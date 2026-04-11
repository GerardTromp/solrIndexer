#!/usr/bin/env python3
"""
sources/pst/extract.py — pull source for archived Outlook PST files.

Wraps the `readpst` CLI (from Ubuntu's pst-utils package) to extract
each PST under FSEARCH_PST_INPUT_DIR into per-message .eml files under
FSEARCH_PST_OUTPUT (the source root in sources.yaml). Generates a
.manifest.json so fs_indexer can enrich each doc with the original
sender, subject, sent date, etc.

Designed to run as a sources.yaml hook command. Exit codes:
  0 → success (or no PSTs to extract — not an error)
  1 → at least one PST failed but not all
  2 → fatal: bad config, output dir unwritable, all PSTs failed

See DESIGN.md in this directory for the rationale behind every choice.

Environment variables:
  FSEARCH_PST_INPUT_DIR     directory to scan for *.pst (required)
  FSEARCH_PST_OUTPUT        output root, also the fsearch source root (required)
  FSEARCH_PST_STATE         state file path (default: <output>/.extract_state.json)
  FSEARCH_PST_READPST       readpst binary (default: /usr/bin/readpst)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("FSEARCH_PST_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("pst-extract")

MANIFEST_FILENAME = ".manifest.json"
STATE_FILENAME = ".extract_state.json"


# ── Config ──────────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error(f"Missing required env var: {name}")
        sys.exit(2)
    return val


def _load_config() -> dict:
    input_dir = Path(_require_env("FSEARCH_PST_INPUT_DIR")).resolve()
    output_root = Path(_require_env("FSEARCH_PST_OUTPUT")).resolve()
    state_path = Path(os.environ.get(
        "FSEARCH_PST_STATE", output_root / STATE_FILENAME))
    readpst = os.environ.get("FSEARCH_PST_READPST", "/usr/bin/readpst")

    if not input_dir.is_dir():
        log.error(f"Input dir does not exist or is not a directory: {input_dir}")
        sys.exit(2)
    if not shutil.which(readpst):
        log.error(f"readpst not found at {readpst}; install pst-utils")
        sys.exit(2)

    output_root.mkdir(parents=True, exist_ok=True)
    if not os.access(output_root, os.W_OK):
        log.error(f"Output dir not writable: {output_root}")
        sys.exit(2)

    return {
        "input_dir":   input_dir,
        "output_root": output_root,
        "state_path":  state_path,
        "readpst":     readpst,
    }


# ── State (incremental tracking) ────────────────────────────────────────────

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning(f"State file unreadable, starting fresh: {e}")
        return {}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _pst_signature(pst_path: Path) -> tuple[int, float]:
    s = pst_path.stat()
    return (s.st_size, s.st_mtime)


def _has_changed(pst_path: Path, state: dict) -> bool:
    """True if we've never seen this PST or its size/mtime changed."""
    key = str(pst_path)
    if key not in state:
        return True
    sig = _pst_signature(pst_path)
    prev = tuple(state[key].get("signature", (-1, -1.0)))
    return sig != prev


# ── Extraction ──────────────────────────────────────────────────────────────

# A PST stem may contain spaces or shell metacharacters; sanitize for the
# output subdirectory name. Keep alnum + a few safe punctuation chars.
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")


def _safe_dirname(stem: str) -> str:
    return _SAFE_STEM.sub("_", stem) or "pst"


def _extract_pst(pst_path: Path, dest_dir: Path, readpst: str) -> bool:
    """
    Run readpst on a single PST. Returns True on success.

    Layout produced (with -S -te -e):
        dest_dir/
          archive_name/
            Inbox/
              <messages>.eml
            Sent Items/
              <messages>.eml
            ...
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        readpst,
        "-S",          # separate files (one per message)
        "-D",          # include deleted items
        "-te",         # email items only (no contacts/journal)
        "-q",          # quiet — errors still go to stderr
        "-o", str(dest_dir),
        str(pst_path),
    ]
    log.info(f"Extracting {pst_path.name} -> {dest_dir}")
    log.debug("cmd: %s", " ".join(cmd))
    start = time.time()
    try:
        # readpst writes its output via dest_dir/<pst_basename>/...,
        # but the manpage notes that the working dir changes after the
        # PST is opened, so an absolute -o is required (which we do).
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        log.error(f"readpst timeout on {pst_path}")
        return False
    except FileNotFoundError as e:
        log.error(f"readpst command not found: {e}")
        return False

    elapsed = time.time() - start
    if result.returncode != 0:
        log.error(f"readpst failed for {pst_path} ({elapsed:.1f}s) "
                  f"exit={result.returncode}")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-20:]:
                log.error(f"  readpst> {line}")
        return False

    n_files = sum(1 for _ in dest_dir.rglob("*.eml"))
    log.info(f"Extracted {pst_path.name}: {n_files} messages in {elapsed:.1f}s")
    return True


# ── Manifest generation ────────────────────────────────────────────────────

_HEADER_PARSER = BytesHeaderParser()


def _eml_to_entry(eml_path: Path, source_root: Path,
                  pst_basename: str) -> dict | None:
    """
    Build a manifest entry for one .eml file by parsing only its
    headers (fast — avoids decoding bodies/attachments). Returns the
    entry dict or None if the file isn't parseable as an email.
    """
    try:
        with eml_path.open("rb") as f:
            msg = _HEADER_PARSER.parse(f)
    except OSError:
        return None

    # ── Source timestamp from Date header ─────────────────────────────────
    source_ts = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt is not None:
                # Strip tzinfo→UTC for Solr pdate format
                if dt.tzinfo is not None:
                    dt = dt.astimezone(tz=None).replace(tzinfo=None)
                source_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            pass

    # ── Folder = parent directory name relative to PST output root ───────
    try:
        rel = eml_path.resolve().relative_to(source_root.resolve())
    except (ValueError, OSError):
        return None
    folder = "/".join(rel.parts[1:-1]) if len(rel.parts) > 2 else ""

    # ── Header summary ────────────────────────────────────────────────────
    def _hdr(name: str) -> str:
        v = msg.get(name)
        return str(v).strip() if v else ""

    metadata = {
        "from":       _hdr("From"),
        "to":         _hdr("To"),
        "cc":         _hdr("Cc"),
        "subject":    _hdr("Subject"),
        "message_id": _hdr("Message-ID"),
        "pst":        pst_basename,
        "folder":     folder,
    }
    # Drop empty fields to keep the JSON small
    metadata = {k: v for k, v in metadata.items() if v}

    entry: dict = {"metadata": metadata}
    if source_ts:
        entry["source_timestamp"] = source_ts
    return entry


def _build_manifest(output_root: Path,
                    pst_subdirs: list[Path]) -> dict:
    """
    Walk all .eml files under each pst subdirectory and assemble the
    manifest dict. Always rebuilt from scratch — see DESIGN.md.
    """
    entries: dict[str, dict] = {}
    total = 0
    for sub in pst_subdirs:
        if not sub.exists():
            continue
        # The first level under sub is readpst's per-PST root (it uses
        # the input file's basename), so its name is what goes into the
        # 'pst' metadata field.
        for child in sub.iterdir():
            if not child.is_dir():
                continue
            pst_basename = child.name
            for eml in child.rglob("*.eml"):
                entry = _eml_to_entry(eml, output_root, pst_basename)
                if entry is None:
                    continue
                try:
                    rel = eml.resolve().relative_to(output_root.resolve())
                except (ValueError, OSError):
                    continue
                entries[rel.as_posix()] = entry
                total += 1

    return {
        "version":      1,
        "source_name":  "pst-archive",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries":      entries,
    }


def _write_manifest(output_root: Path, manifest: dict) -> None:
    target = output_root / MANIFEST_FILENAME
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(target)
    log.info(f"Wrote manifest: {target} ({len(manifest['entries'])} entries)")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    cfg = _load_config()
    state = _load_state(cfg["state_path"])

    # Discover PSTs (case-insensitive *.pst, non-recursive — Outlook
    # archives are typically siblings, not nested).
    psts = sorted(p for p in cfg["input_dir"].iterdir()
                  if p.is_file() and p.suffix.lower() == ".pst")
    if not psts:
        log.warning(f"No .pst files found in {cfg['input_dir']}")
        # Still write/refresh the manifest so the source root has one
        # even when empty (avoids stale manifest from a previous run).
        _write_manifest(cfg["output_root"], _build_manifest(cfg["output_root"], []))
        return 0

    log.info(f"Found {len(psts)} PST(s) under {cfg['input_dir']}")

    n_extracted = 0
    n_skipped_unchanged = 0
    n_failed = 0
    pst_subdirs: list[Path] = []

    for pst in psts:
        dest_subdir = cfg["output_root"] / _safe_dirname(pst.stem)
        pst_subdirs.append(dest_subdir)

        if not _has_changed(pst, state) and dest_subdir.exists():
            log.info(f"Unchanged: {pst.name} (skipping extraction)")
            n_skipped_unchanged += 1
            continue

        ok = _extract_pst(pst, dest_subdir, cfg["readpst"])
        if ok:
            state[str(pst)] = {
                "signature":    list(_pst_signature(pst)),
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
            }
            n_extracted += 1
        else:
            n_failed += 1

    # Persist state regardless — partial progress is better than none
    _save_state(cfg["state_path"], state)

    # Always rebuild the manifest from scratch so it stays in sync
    # with whatever's actually on disk.
    manifest = _build_manifest(cfg["output_root"], pst_subdirs)
    try:
        _write_manifest(cfg["output_root"], manifest)
    except OSError as e:
        log.error(f"Manifest write failed: {e}")
        return 2

    log.info(
        f"Done: extracted={n_extracted} unchanged={n_skipped_unchanged} "
        f"failed={n_failed} manifest_entries={len(manifest['entries'])}"
    )

    if n_failed and n_extracted == 0 and n_skipped_unchanged == 0:
        return 2   # everything failed
    if n_failed:
        return 1   # partial failure
    return 0


if __name__ == "__main__":
    sys.exit(main())

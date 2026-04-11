"""
fs_sources.py — source configuration and hook execution for fs_indexer.

Reads a sources.yaml file describing one or more indexable sources. Each
source has a name, a kind, a root directory, optional excludes, and an
optional pre-index hook. The hook is a shell command run before walking
the root; it's intended for "pull" sources that fetch data into the root
(PST extraction, Gmail sync, etc.).

This module deliberately knows nothing about Solr or fs_indexer's
internals — it just parses config, exposes a small dataclass, and runs
hooks with lock/timeout protection. fs_indexer.main() is responsible for
looping over the returned sources and calling the existing indexer
machinery for each.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None   # type: ignore

# Default location — picked to match the /opt/fsearch install layout.
# Users can override via FSEARCH_SOURCES env var or the --sources CLI flag.
DEFAULT_SOURCES_FILE = Path(os.environ.get(
    "FSEARCH_SOURCES", "/opt/fsearch/sources.yaml"))


ON_FAILURE_MODES = {"skip", "abort", "continue-stale"}


@dataclass
class Hook:
    command: str
    timeout: int = 3600              # seconds
    lockfile: Path | None = None     # per-source lock to prevent overlap
    on_failure: str = "skip"         # skip | abort | continue-stale


@dataclass
class Source:
    name: str                        # unique human-readable identifier
    kind: str                        # coarse type: fs | pst | imap | msg | ...
    roots: list[Path]                # directories fs_indexer will walk
    excludes: list[str] = field(default_factory=list)
    hook: Hook | None = None

    @property
    def root(self) -> Path:
        """First root — convenience for logging when only one is expected."""
        return self.roots[0] if self.roots else Path(".")


def load_sources(path: Path = DEFAULT_SOURCES_FILE) -> list[Source]:
    """
    Parse and validate a sources.yaml file. Returns [] if the file is
    missing (the caller decides what to do — typically fall back to
    legacy env/CLI behavior). Raises ValueError on malformed entries.
    """
    if not path.exists():
        return []
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required for sources.yaml support; "
            "install with: pip install pyyaml")

    with path.open() as f:
        data = yaml.safe_load(f) or {}

    raw = data.get("sources", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'sources' must be a list")

    out: list[Source] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: source #{i} is not a mapping: {entry!r}")

        name = entry.get("name")
        if not name:
            raise ValueError(
                f"{path}: source #{i} missing required 'name': {entry!r}")

        kind = entry.get("kind", "fs")
        excludes = entry.get("excludes") or []
        if not isinstance(excludes, list):
            raise ValueError(f"{path}: source '{name}' excludes must be a list")

        hook: Hook | None = None
        if "hook" in entry and entry["hook"]:
            h = entry["hook"]
            if not isinstance(h, dict) or "command" not in h:
                raise ValueError(
                    f"{path}: source '{name}' hook must be a mapping with a 'command' key")
            on_fail = h.get("on_failure", "skip")
            if on_fail not in ON_FAILURE_MODES:
                raise ValueError(
                    f"{path}: source '{name}' hook.on_failure must be one of "
                    f"{sorted(ON_FAILURE_MODES)}")
            hook = Hook(
                command=str(h["command"]),
                timeout=int(h.get("timeout", 3600)),
                lockfile=Path(h["lockfile"]) if h.get("lockfile") else None,
                on_failure=on_fail,
            )

        # A single YAML entry describes one source with one root. If a
        # future format wants multi-root entries (e.g., for legacy bundles),
        # accept `roots:` as a list fallback, but prefer `root:` for clarity.
        root_val = entry.get("root")
        if root_val is not None:
            roots_list = [Path(root_val)]
        else:
            raw_roots = entry.get("roots", [])
            if not isinstance(raw_roots, list) or not raw_roots:
                raise ValueError(
                    f"{path}: source '{name}' must specify 'root' or 'roots'")
            roots_list = [Path(r) for r in raw_roots]

        out.append(Source(
            name=str(name),
            kind=str(kind),
            roots=roots_list,
            excludes=[str(e) for e in excludes],
            hook=hook,
        ))

    # Sanity: duplicate names are almost always a config bug
    seen: set[str] = set()
    for s in out:
        if s.name in seen:
            raise ValueError(f"{path}: duplicate source name '{s.name}'")
        seen.add(s.name)

    return out


def acquire_source_lock(lockfile: Path) -> bool:
    """
    PID-based per-source lockfile. Returns True if the lock was acquired,
    False if another live process still holds it. Stale locks (dead PID)
    are silently taken over.
    """
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    if lockfile.exists():
        try:
            pid = int(lockfile.read_text().strip())
            os.kill(pid, 0)   # signal 0: existence probe
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass               # stale — fall through and overwrite
    lockfile.write_text(str(os.getpid()))
    return True


def release_source_lock(lockfile: Path) -> None:
    try:
        if lockfile.exists():
            pid = int(lockfile.read_text().strip())
            if pid == os.getpid():
                lockfile.unlink()
    except (ValueError, OSError):
        pass


def run_hook(source: Source, log) -> bool:
    """
    Execute the source's pre-index hook with timeout and optional
    lockfile. Returns True on success (or no hook); False on failure.
    Caller inspects source.hook.on_failure to decide next step.
    """
    hook = source.hook
    if hook is None:
        return True

    if hook.lockfile and not acquire_source_lock(hook.lockfile):
        log.warning(
            f"Source '{source.name}': lockfile {hook.lockfile} held by another "
            f"process — skipping hook this run")
        return False

    try:
        log.info(
            f"Source '{source.name}': running hook (timeout {hook.timeout}s)")
        log.debug(f"Hook command: {hook.command}")
        start = time.time()
        result = subprocess.run(
            hook.command,
            shell=True,
            timeout=hook.timeout,
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - start

        # Surface any hook output at the right log level so it's
        # discoverable in /mnt/wd1/solr/logs/indexer.log.
        if result.stdout:
            for line in result.stdout.splitlines()[-20:]:
                log.debug(f"  hook> {line}")

        if result.returncode == 0:
            log.info(f"Source '{source.name}': hook OK ({elapsed:.1f}s)")
            return True

        log.error(
            f"Source '{source.name}': hook failed with exit {result.returncode} "
            f"({elapsed:.1f}s)")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-10:]:
                log.error(f"  hook> {line}")
        return False

    except subprocess.TimeoutExpired:
        log.error(
            f"Source '{source.name}': hook exceeded timeout of {hook.timeout}s")
        return False
    except FileNotFoundError as e:
        log.error(f"Source '{source.name}': hook command not found: {e}")
        return False
    except Exception as e:
        log.error(f"Source '{source.name}': hook error: {e}")
        return False
    finally:
        if hook.lockfile:
            release_source_lock(hook.lockfile)


# ── Manifest reader ──────────────────────────────────────────────────────────

MANIFEST_FILENAME = ".manifest.json"


class Manifest:
    """
    In-memory view of a source's sibling `.manifest.json`.

    Expected format (version 1):

        {
          "version": 1,
          "source_name": "gmail",
          "generated_at": "2026-04-11T08:30:00Z",
          "entries": {
            "inbox/2024/2024-03-15_subject-abc.eml": {
              "source_timestamp": "2024-03-15T09:14:22Z",
              "metadata": {
                "from": "alice@example.com",
                "to": ["bob@example.com"],
                "subject": "Subject ABC",
                "message_id": "<xyz@mail>"
              }
            },
            ...
          }
        }

    Entry keys are paths **relative to the source root** — this keeps the
    manifest valid across remounts (e.g., /mnt/wd1 -> /mnt/data).

    The loader is lenient: missing files, malformed JSON, and unknown
    version numbers all degrade gracefully to "no manifest data", so a
    broken manifest never prevents indexing.
    """

    def __init__(self, source_root: Path, entries: dict[str, dict]):
        self.source_root = source_root
        self._entries = entries

    def lookup(self, path: Path) -> dict | None:
        """
        Return the manifest entry for `path`, or None if not listed.
        Path is matched by its position relative to the source root.
        """
        try:
            rel = path.resolve().relative_to(self.source_root.resolve())
        except (ValueError, OSError):
            return None
        # Manifest keys are posix-style by convention; match both the
        # as_posix form and the raw string form for author convenience.
        rel_str = rel.as_posix()
        return self._entries.get(rel_str) or self._entries.get(str(rel))

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


def load_manifest(source_root: Path, log=None) -> Manifest | None:
    """
    Read `<source_root>/.manifest.json` if present. Returns None if
    there's no manifest (the common case for plain fs sources), a
    populated Manifest on success, or an empty Manifest on load/parse
    errors so the caller can distinguish "intentionally absent" (None)
    from "tried to load but couldn't" (empty).
    """
    mf_path = source_root / MANIFEST_FILENAME
    if not mf_path.exists():
        return None

    try:
        with mf_path.open() as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        if log is not None:
            log.warning(f"Manifest load failed at {mf_path}: {e}")
        return Manifest(source_root, {})

    if not isinstance(data, dict):
        if log is not None:
            log.warning(f"Manifest at {mf_path} is not a JSON object")
        return Manifest(source_root, {})

    version = data.get("version", 1)
    if version != 1:
        if log is not None:
            log.warning(f"Manifest {mf_path} uses unsupported version {version}")
        return Manifest(source_root, {})

    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        if log is not None:
            log.warning(f"Manifest at {mf_path}: 'entries' must be a mapping")
        return Manifest(source_root, {})

    if log is not None:
        log.info(f"Loaded manifest from {mf_path} ({len(entries)} entries)")
    return Manifest(source_root, entries)

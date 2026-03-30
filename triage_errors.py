#!/usr/bin/env python3
"""
triage_errors.py — Classify Tika failures as retryable or permanent.

Reads the error log, re-probes each file against Tika to capture the actual
exception, and writes two output files:
  retryable.log   — files that failed due to transient issues (feed to --retry-errors)
  permanent.log   — files with unrecoverable errors (encrypted, corrupt, etc.)

Usage:
    python triage_errors.py [--error-log /mnt/wd1/solr/logs/index_errors.log]

After running:
    # Copy retryable files into the error log so --retry-errors picks them up
    cp retryable.log /mnt/wd1/solr/logs/index_errors.log
    python fs_indexer.py --retry-errors
"""

import os, sys, argparse, datetime
from pathlib import Path

import requests

TIKA_URL = os.environ.get("TIKA_URL", "http://localhost:9998/tika")
ERROR_LOG = Path("/mnt/wd1/solr/logs/index_errors.log")

TIKA_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt":  "application/vnd.oasis.opendocument.text",
    ".ods":  "application/vnd.oasis.opendocument.spreadsheet",
    ".odp":  "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".rtf":  "application/rtf",
}

# Substrings in Tika error responses that indicate permanent failure
PERMANENT_MARKERS = [
    "EncryptedDocumentException",
    "password",
    "Missing root object",
    "Expected.*but found",
    "invalid cross reference",
    "Unexpected EOF",
    "document is really a",      # wrong format masquerading as another
    "bomb",                       # zip bomb protection
    "OldWordFileFormatException", # ancient .doc format
]


def read_error_log(logfile: Path) -> list[tuple[str, str, str]]:
    """Return list of (timestamp, reason, filepath) from the error log."""
    entries = []
    with open(logfile) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                entries.append((parts[0], parts[1], parts[2]))
    return entries


def probe_tika(filepath: str) -> tuple[str, str]:
    """
    Send the file to Tika. Returns (status, detail):
      ("ok", "")              — extraction succeeded
      ("permanent", "reason") — unrecoverable error
      ("retryable", "reason") — transient / unknown error
      ("missing", "")         — file no longer exists
    """
    path = Path(filepath)
    if not path.exists():
        return "missing", "file no longer exists"

    ext = path.suffix.lower()
    if ext not in TIKA_MIME:
        # Not a Tika-handled file — original error was something else
        return "retryable", "non-tika file type"

    content_type = TIKA_MIME[ext]
    try:
        with open(path, "rb") as f:
            data = f.read(10 * 1024 * 1024)  # 10MB cap

        resp = requests.put(
            TIKA_URL, data=data,
            headers={"Accept": "text/plain", "Content-Type": content_type},
            timeout=30)

        if resp.ok:
            return "ok", ""

        detail = resp.text.strip().split("\n")[0][:300] if resp.text else ""

        for marker in PERMANENT_MARKERS:
            if marker.lower() in detail.lower():
                return "permanent", detail

        # Unknown 422 or other error — assume retryable
        return "retryable", f"HTTP {resp.status_code} | {detail}"

    except requests.exceptions.ConnectionError:
        return "retryable", "tika connection refused"
    except requests.exceptions.Timeout:
        return "retryable", "tika timeout"
    except Exception as e:
        return "retryable", str(e)


def main():
    ap = argparse.ArgumentParser(description="Triage Tika failures into retryable vs permanent")
    ap.add_argument("--error-log", type=Path, default=ERROR_LOG)
    ap.add_argument("--output-dir", type=Path, default=Path("."),
                    help="Directory for retryable.log and permanent.log")
    args = ap.parse_args()

    if not args.error_log.exists():
        print(f"No error log at {args.error_log}")
        sys.exit(0)

    entries = read_error_log(args.error_log)
    print(f"Read {len(entries)} entries from {args.error_log}")

    # Check Tika is up
    try:
        r = requests.get(TIKA_URL.rsplit("/", 1)[0], timeout=5)
        if not r.ok:
            raise Exception()
    except Exception:
        print("ERROR: Tika is not responding — start it first")
        sys.exit(1)

    retryable_f = open(args.output_dir / "retryable.log", "w")
    permanent_f = open(args.output_dir / "permanent.log", "w")

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    retryable_f.write(f"# Triage run {ts}\n")
    permanent_f.write(f"# Triage run {ts}\n")

    counts = {"ok": 0, "retryable": 0, "permanent": 0, "missing": 0}

    for i, (orig_ts, orig_reason, filepath) in enumerate(entries):
        status, detail = probe_tika(filepath)
        counts[status] += 1

        if status == "ok":
            # Extraction now works — write to retryable so it gets re-indexed
            retryable_f.write(f"{orig_ts}\t{orig_reason}\t{filepath}\n")
        elif status == "retryable":
            retryable_f.write(f"{orig_ts}\t{detail or orig_reason}\t{filepath}\n")
        elif status == "permanent":
            permanent_f.write(f"{orig_ts}\t{detail}\t{filepath}\n")
        # missing: skip entirely

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(entries)}  ok={counts['ok']} retry={counts['retryable']} "
                  f"perm={counts['permanent']} missing={counts['missing']}")

    retryable_f.close()
    permanent_f.close()

    print(f"\nDone. {len(entries)} files triaged:")
    print(f"  OK (now works):  {counts['ok']}")
    print(f"  Retryable:       {counts['retryable']}")
    print(f"  Permanent:       {counts['permanent']}")
    print(f"  Missing:         {counts['missing']}")
    print(f"\nOutput:")
    print(f"  {args.output_dir / 'retryable.log'}")
    print(f"  {args.output_dir / 'permanent.log'}")
    print(f"\nTo retry recoverable files:")
    print(f"  cp retryable.log {ERROR_LOG}")
    print(f"  python fs_indexer.py --retry-errors")


if __name__ == "__main__":
    main()

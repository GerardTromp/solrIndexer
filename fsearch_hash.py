"""
fsearch_hash.py — content hash helper for the indexer.

Adapted from /mnt/d/GT/Professional/CHPC/bin/multiDigest.py. That script
computes five hashes in one pass for large-file integrity verification.
For fsearch we only need *one* stable key per file for dedup, so this
module exposes a single SHA-256-over-content helper tuned for fast
sequential reads on ext4/NVMe.

Key differences from the CHPC version:
  - Only SHA-256 (dedup doesn't benefit from the five-hash ensemble).
  - Memory-aware chunk sizing via psutil (portable), not `free -t -m`.
  - Chunk size rounded to a page-size multiple for aligned reads.
  - Fixed the cascade bug from the original: largest bucket wins.
  - Large-file guard: files above a threshold are skipped unless the
    caller opts in (mirrors the existing --large-files flag semantics).
"""

import hashlib
import os
from pathlib import Path

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# Hard ceiling above which we refuse to hash unless explicitly asked.
# Matches the existing MAX_TEXT_SIZE semantics — a 500MB VM image has
# little dedup value and hashing it slows every index pass.
DEFAULT_MAX_HASH_SIZE = 500 * 1024 * 1024   # 500 MB

try:
    _PAGE_SIZE = os.sysconf("SC_PAGESIZE")
except (ValueError, AttributeError, OSError):
    _PAGE_SIZE = 4096


def _available_mb() -> int:
    """Available memory in MB, or a conservative default if unknown."""
    if _HAS_PSUTIL:
        try:
            return psutil.virtual_memory().available // (1024 * 1024)
        except Exception:
            pass
    # Fallback: parse /proc/meminfo (Linux-only).
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError):
        pass
    return 500   # safe middle-of-the-road assumption


def compute_chunk_size() -> int:
    """
    Return a read-chunk size in bytes, scaled to available memory and
    aligned to a page-size multiple for efficient sequential reads.

    Adapted from multiDigest.get_chunksize() — same thresholds, same
    intent, but:
      - Uses proper elif cascade (the original fell through to the
        smallest matching value instead of the largest).
      - Returns bytes aligned to page size instead of naked KB.
    """
    mem_avail = _available_mb()

    if mem_avail > 1000:
        read_mult_kb = 1024        # 1 MB chunks — sweet spot for NVMe
    elif mem_avail > 500:
        read_mult_kb = 512
    elif mem_avail > 200:
        read_mult_kb = 256
    elif mem_avail > 100:
        read_mult_kb = 128
    elif mem_avail > 50:
        read_mult_kb = 64
    else:
        read_mult_kb = 32

    raw = read_mult_kb * 1024
    # Round up to a page-size multiple (no-op on 4K-page systems since
    # all values above are already multiples of 4096).
    return ((raw + _PAGE_SIZE - 1) // _PAGE_SIZE) * _PAGE_SIZE


# Compute once at import; negligible cost, and avoids re-reading meminfo
# on every file. Good enough — the indexer is a short-lived process.
CHUNK_SIZE = compute_chunk_size()


def sha256_file(path: Path, max_size: int = DEFAULT_MAX_HASH_SIZE,
                large_files: bool = False) -> str | None:
    """
    Return the hex SHA-256 of `path`'s contents, or None if hashing is
    skipped (too large without opt-in, unreadable, etc.).

    `max_size`:      threshold above which a file is skipped by default
    `large_files`:   if True, hash regardless of size

    Returns None on any filesystem error. Errors are not raised — the
    caller (indexer) treats the hash as optional enrichment and a missing
    hash must not prevent indexing the doc.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None

    if size == 0:
        # Empty files all share the same SHA-256 — compute once, return.
        # (Alternative: return None. But the empty-file hash is a legit
        # dedup key: it clusters all empty files together, which is
        # exactly what a user looking for "find duplicates" wants.)
        return hashlib.sha256(b"").hexdigest()

    if size > max_size and not large_files:
        return None

    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, PermissionError):
        return None

    return h.hexdigest()

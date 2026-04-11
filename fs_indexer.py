#!/usr/bin/env python3
"""
fs_indexer.py  — Incremental filesystem → Solr indexer for WSL2
Deps: pip install pysolr tika requests click rich
"""

import gc
import multiprocessing
import os, sys, stat, signal, mimetypes, datetime, time, json, logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Generator

import click
import pysolr
import requests

from fsearch_hash import sha256_file
from rich.progress import Progress, SpinnerColumn, TextColumn, MofNCompleteColumn
from rich.logging import RichHandler
from rich.console import Console

logging.basicConfig(handlers=[RichHandler(markup=True)], level=logging.INFO,
                    format="%(message)s", datefmt="[%X]")
log = logging.getLogger("fs_indexer")
console = Console(stderr=True)

# Add file handler alongside RichHandler
ERROR_LOG = Path("/mnt/wd1/solr/logs/index_errors.log")
CORRUPT_LOG = Path("/mnt/wd1/solr/logs/corrupt_files.log")
LOG_FILE = Path("/mnt/wd1/solr/logs/indexer.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(file_handler)

SOLR_URL     = os.environ.get("SOLR_URL", "http://localhost:8983/solr/filesystem")
TIKA_URL     = os.environ.get("TIKA_URL", "http://localhost:9998/tika")
# /rmeta/text returns extracted text AND detected metadata (Content-Type,
# Content-Language, etc.) in a single call — strictly better than /tika for
# our use case, zero extra cost. Derived from TIKA_URL by rewriting the path.
_tika_base = TIKA_URL.rsplit("/", 1)[0] if TIKA_URL.count("/") > 2 else TIKA_URL
TIKA_RMETA_URL = os.environ.get("TIKA_RMETA_URL", f"{_tika_base}/rmeta/text")
BATCH_SIZE   = 300
STATE_FILE   = Path.home() / ".solr" / "indexer_state.json"
CONTENT_PREVIEW  = 1024               # 1KB stored preview for GUI recognition
MAX_CONTENT      = 10 * 1024 * 1024   # 10MB read cap (applied after size check)
MAX_TEXT_SIZE    = 20 * 1024 * 1024   # 20MB — skip content extraction above this by default
LARGE_FILE_LIMIT = 500 * 1024 * 1024  # 500MB — hard cap even with --large-files
LARGE_TIKA_TIMEOUT = 120              # seconds — Tika timeout for large files

# Find cache settings
FIND_CACHE     = Path(os.environ.get("FSEARCH_FIND_CACHE", "/mnt/wd1/solr/find_cache.txt"))
FIND_CACHE_MAX = int(os.environ.get("FSEARCH_FIND_CACHE_MAX_HOURS", "12"))  # hours

# Lockfile — prevents concurrent runs (cron vs manual)
LOCK_FILE = Path(os.environ.get("FSEARCH_LOCK", "/mnt/wd1/solr/indexer.lock"))

# Graceful shutdown flag — checked between files in the index loop
_shutdown_requested = False

def _handle_signal(signum, frame):
    """Signal handler for the worker (child) process."""
    global _shutdown_requested
    name = signal.Signals(signum).name
    if _shutdown_requested:
        log.warning(f"Received {name} again — forcing immediate exit")
        release_lock()
        os._exit(1)
    log.warning(f"Received {name} — finishing current file then stopping "
                f"(send again to force-quit)")
    _shutdown_requested = True


def acquire_lock() -> bool:
    """
    Create a lockfile containing our PID.
    Returns True if lock acquired, False if another instance is running.
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # Check if that PID is still alive
            os.kill(pid, 0)
            # Process exists — lock is held
            log.error(f"Another indexer is running (PID {pid}, lockfile {LOCK_FILE})")
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            # PID is stale (process gone) or lockfile is malformed — take over
            log.warning(f"Stale lockfile found (PID gone) — taking over")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile if it's ours."""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            if pid == os.getpid():
                LOCK_FILE.unlink()
    except (ValueError, OSError):
        pass


def stop_running_indexer() -> bool:
    """Send SIGTERM to a running indexer. Returns True if a process was signalled."""
    if not LOCK_FILE.exists():
        log.info("No running indexer found (no lockfile)")
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log.info(f"Sent SIGTERM to indexer PID {pid} — it will stop after the current batch")
        # Wait briefly for it to exit
        for _ in range(30):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                log.info(f"Indexer PID {pid} has exited")
                LOCK_FILE.unlink(missing_ok=True)
                return True
        log.warning(f"Indexer PID {pid} still running after 30s — may need manual kill")
        return True
    except ProcessLookupError:
        log.info(f"Indexer PID in lockfile is not running — removing stale lock")
        LOCK_FILE.unlink(missing_ok=True)
        return False
    except (ValueError, OSError) as e:
        log.error(f"Could not stop indexer: {e}")
        return False

TEXT_EXTS = {
    ".txt",".md",".rst",".org",
    ".py",".r",".R",".jl",".pl",".perl",
    ".sh",".bash",".zsh",".fish",
    ".c",".cpp",".h",".hpp",".java",".js",".ts",".go",".rs",".scala",
    ".yaml",".yml",".toml",".json",".xml",".csv",".tsv",".ndjson",
    ".html",".htm",".css",".sql",
    ".nf",".snakemake",         # nextflow / snakemake
    ".fasta",".fa",".fna",".faa",".ffn",
    ".fastq",".fq",
    ".vcf",".bcf",".gff",".gff3",".gtf",".bed",".bedgraph",
    ".sam",                      # BAM is binary, skip
    ".log",".out",".err",
    ".conf",".cfg",".ini",".env",
    ".tex",".bib",
    ".ipynb",                    # notebook JSON — content-searchable
}

TIKA_EXTS = {".pdf",".docx",".xlsx",".pptx",".odt",".ods",".odp",".epub",".rtf"}

SKIP_DIRS = {
    ".git",".svn",".hg",
    "__pycache__",".pytest_cache",".mypy_cache",
    "node_modules",".cargo",
    ".solr",".cache",".thumbnails",
    "snap","proc","sys","dev",   # Linux pseudo-fs
}

# ── Device (filesystem) detection ────────────────────────────────────────────

def group_roots_by_device(roots: list[Path]) -> dict[int, list[Path]]:
    """
    Partition root paths by filesystem device ID (st_dev).
    Roots on the same physical device share an st_dev value.
    """
    groups: dict[int, list[Path]] = defaultdict(list)
    for root in roots:
        try:
            dev = root.stat().st_dev
            groups[dev].append(root)
        except OSError as e:
            log.warning(f"Cannot stat root {root}: {e} — skipping")
    return dict(groups)


def _device_cache_path(dev_id: int) -> Path:
    """Per-device find cache path."""
    return FIND_CACHE.parent / f"{FIND_CACHE.stem}_dev{dev_id}{FIND_CACHE.suffix}"


# ── Permanent skip list ──────────────────────────────────────────────────────
# Lives alongside indexer state (~/.solr/), not in logs.
# Files here will never have content extraction retried.

SKIP_CONTENT_FILE = STATE_FILE.parent / "skip_content.tsv"

# Error substrings that indicate permanent, unrecoverable extraction failure
PERMANENT_REASONS = [
    "HTTP 422",                     # Tika actively rejected the file
    "EncryptedDocumentException",
    "InvalidPasswordException",
    "password",
    "Missing root object",
    "invalid cross reference",
    "Unexpected EOF",
    "NotOfficeXmlFileException",
    "not a valid OOXML",
    "OldWordFileFormatException",
    "bomb",                         # zip bomb
    "document is really a",         # format mismatch
    "No valid entries or contents",
    "file no longer exists",
]

_skip_content_set: set[str] | None = None   # lazy-loaded


def _is_permanent_failure(reason: str) -> bool:
    """True if the error reason indicates the file will never be extractable."""
    reason_lower = reason.lower()
    return any(marker.lower() in reason_lower for marker in PERMANENT_REASONS)


def load_skip_content() -> set[str]:
    """Load the permanent skip set from disk. Cached after first call."""
    global _skip_content_set
    if _skip_content_set is not None:
        return _skip_content_set
    _skip_content_set = set()
    if SKIP_CONTENT_FILE.exists():
        with open(SKIP_CONTENT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                _skip_content_set.add(parts[0])
    if _skip_content_set:
        log.info(f"Loaded {len(_skip_content_set):,} permanently skipped files")
    return _skip_content_set


def add_to_skip_content(filepath: str, reason: str):
    """Append a file to the permanent skip list and the corrupt files log."""
    skip = load_skip_content()
    if filepath in skip:
        return
    skip.add(filepath)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SKIP_CONTENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SKIP_CONTENT_FILE, "a") as f:
        f.write(f"{filepath}\t{ts}\t{reason}\n")
    # Also log to corrupt_files.log for review / cleanup
    with open(CORRUPT_LOG, "a") as f:
        f.write(f"{ts}\t{reason}\t{filepath}\n")


def should_skip_content(filepath: str) -> bool:
    """True if this file is in the permanent skip list."""
    return filepath in load_skip_content()


# ── Log problem files  ──────────────────────────────────────────────────────

def log_error(filepath: str, reason: str):
    # If this is a permanent failure, add to skip list instead of error log
    if _is_permanent_failure(reason):
        add_to_skip_content(filepath, reason)
        return
    with open(ERROR_LOG, "a") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts}\t{reason}\t{filepath}\n")

# ── State management ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "indexed_count": 0}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Tika health & auto-restart ───────────────────────────────────────────────

TIKA_JAR = os.environ.get("TIKA_JAR", str(Path.home() / "opt" / "tika-server.jar"))
TIKA_LOG = Path(os.environ.get("TIKA_LOG", "/mnt/wd1/solr/logs/tika.log"))
TIKA_LOG4J = os.environ.get("TIKA_LOG4J", "/opt/fsearch/setup/tika-log4j2.xml")
TIKA_PORT = int(TIKA_URL.rsplit(":", 1)[-1].split("/")[0]) if ":" in TIKA_URL else 9998

_tika_consecutive_failures = 0
_TIKA_FAILURE_THRESHOLD = 5       # consecutive failures before declaring Tika dead
_tika_alive = True                # flipped to False when threshold reached
_tika_restarts = 0
_TIKA_MAX_RESTARTS = 3            # max auto-restarts per indexer run

def check_tika_alive() -> bool:
    """Quick liveness probe — GET on Tika root."""
    try:
        base = TIKA_URL.rsplit("/", 1)[0] if "/" in TIKA_URL.split("//", 1)[-1] else TIKA_URL
        resp = requests.get(base, timeout=5)
        return resp.ok
    except Exception:
        return False


def _restart_tika() -> bool:
    """Attempt to restart Tika server. Returns True if successful."""
    global _tika_restarts
    import subprocess

    if _tika_restarts >= _TIKA_MAX_RESTARTS:
        log.error(f"Tika restart limit reached ({_TIKA_MAX_RESTARTS}) — not restarting again. "
                  f"Binary content will not be extracted for the rest of this run.")
        return False

    if not Path(TIKA_JAR).exists():
        log.error(f"Tika JAR not found at {TIKA_JAR} — cannot restart")
        return False

    _tika_restarts += 1
    log.warning(f"Attempting Tika restart ({_tika_restarts}/{_TIKA_MAX_RESTARTS})...")

    # Kill any zombie Tika process
    try:
        subprocess.run(["pkill", "-f", "tika-server"], capture_output=True, timeout=5)
        time.sleep(2)
    except Exception:
        pass

    # Rotate log on restart
    if TIKA_LOG.exists() and TIKA_LOG.stat().st_size > 0:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated = TIKA_LOG.with_name(f"{TIKA_LOG.stem}_{ts}{TIKA_LOG.suffix}")
        try:
            TIKA_LOG.rename(rotated)
            log.info(f"Rotated Tika log → {rotated}")
        except OSError:
            pass

    # Start Tika
    TIKA_LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["java"]
    if Path(TIKA_LOG4J).exists():
        cmd.append(f"-Dlog4j2.configurationFile={TIKA_LOG4J}")
    cmd += ["-jar", TIKA_JAR, "--port", str(TIKA_PORT)]
    with open(TIKA_LOG, "a") as tlog:
        subprocess.Popen(
            cmd,
            stdout=tlog, stderr=tlog,
            start_new_session=True,
        )

    # Wait for readiness
    for _ in range(20):
        time.sleep(2)
        if check_tika_alive():
            log.info("Tika restarted successfully")
            return True

    log.error("Tika failed to start after restart attempt")
    return False


def _tika_failure(path: Path, reason: str) -> str:
    """Track consecutive Tika failures; auto-restart once threshold is hit."""
    global _tika_consecutive_failures, _tika_alive
    _tika_consecutive_failures += 1

    if _tika_consecutive_failures >= _TIKA_FAILURE_THRESHOLD and _tika_alive:
        alive = check_tika_alive()
        if not alive:
            log.error(
                f"Tika appears DOWN ({_tika_consecutive_failures} consecutive failures, "
                f"liveness probe failed)")
            # Attempt auto-restart
            if _restart_tika():
                _tika_consecutive_failures = 0
                _tika_alive = True
                log.info("Tika recovered — will retry binary files from error log on next run")
            else:
                _tika_alive = False
        else:
            log.warning(
                f"Tika liveness OK but {_tika_consecutive_failures} extraction failures — "
                f"may be overloaded or processing corrupt files")
            # Reset counter to give it another chance (parser errors, not dead)
            _tika_consecutive_failures = 0
    elif _tika_consecutive_failures <= _TIKA_FAILURE_THRESHOLD:
        log.debug(f"Tika failed for {path}: {reason}")

    # Log the file to error log so --retry-errors picks it up
    log_error(str(path), f"tika: {reason}")
    return ""


def _tika_success() -> None:
    """Reset failure counter on any successful extraction."""
    global _tika_consecutive_failures, _tika_alive
    if _tika_consecutive_failures > 0:
        if not _tika_alive:
            log.info("Tika is responding again — resuming binary content extraction")
        _tika_consecutive_failures = 0
        _tika_alive = True


# ── MIME type mapping for Tika ───────────────────────────────────────────────

_TIKA_MIME = {
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

# ── Content extraction ───────────────────────────────────────────────────────

def _parse_rmeta_response(payload) -> tuple[str, dict]:
    """
    Tika /rmeta/text returns a JSON array — one object per embedded document
    (compound files like .msg, .zip yield multiple). The first element is
    the top-level container; its metadata is what we want.

    Returns (content_text, metadata_subset) where metadata_subset contains
    only the fields we care about in fsearch:
      - language (normalized lowercase, no region suffix)
      - mimetype_detected (Content-Type with any charset parameter stripped)
    """
    if not isinstance(payload, list) or not payload:
        return "", {}
    top = payload[0] if isinstance(payload[0], dict) else {}

    text = top.get("X-TIKA:content", "") or ""

    meta: dict[str, str] = {}

    ct = top.get("Content-Type", "")
    if isinstance(ct, list):
        ct = ct[0] if ct else ""
    if ct:
        # Strip parameters like '; charset=UTF-8' and normalize lowercase
        meta["mimetype_detected"] = ct.split(";", 1)[0].strip().lower()

    # Tika reports language under several possible keys depending on version
    # and file type — check them in order of preference.
    lang = (top.get("language")
            or top.get("Content-Language")
            or top.get("dc:language")
            or "")
    if isinstance(lang, list):
        lang = lang[0] if lang else ""
    if lang:
        # "en-US" → "en" — fsearch only cares about language, not region
        meta["language"] = lang.split("-", 1)[0].strip().lower()

    return text, meta


def extract_via_tika(path: Path, large: bool = False) -> tuple[str, dict]:
    """
    Extract text + metadata via Tika's /rmeta/text endpoint.

    Returns (content_text, metadata_dict). On any failure the text is empty
    and the metadata dict is empty; callers must treat both fields as
    optional. A Tika failure never raises.
    """
    if not _tika_alive or _shutdown_requested:
        return "", {}
    timeout = LARGE_TIKA_TIMEOUT if large else 15
    ext = path.suffix.lower()
    content_type = _TIKA_MIME.get(ext, "application/octet-stream")
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CONTENT)
        if _shutdown_requested:
            return "", {}
        resp = requests.put(
            TIKA_RMETA_URL, data=data,
            headers={"Accept": "application/json",
                     "Content-Type": content_type},
            timeout=timeout)
        if resp.ok:
            _tika_success()
            try:
                return _parse_rmeta_response(resp.json())
            except ValueError as e:
                # Malformed JSON from Tika — treat as a parse failure but
                # don't mark Tika dead; the next file may be fine.
                log.debug(f"Tika rmeta parse error for {path}: {e}")
                return "", {}
        # Capture Tika's error detail (Java exception) from the response body
        detail = resp.text.strip().split("\n")[0][:200] if resp.text else ""
        reason = f"HTTP {resp.status_code}"
        if detail:
            reason += f" | {detail}"
        _tika_failure(path, reason)
        return "", {}
    except requests.exceptions.Timeout:
        log.warning(f"Tika timeout ({timeout}s) for {path} — skipping content")
        _tika_failure(path, "timeout")
        return "", {}
    except requests.exceptions.ConnectionError:
        _tika_failure(path, "connection refused")
        return "", {}
    except Exception as e:
        _tika_failure(path, str(e))
        return "", {}


def extract_content(path: Path, large_files: bool = False) -> tuple[str, dict]:
    """
    Returns (content_text, metadata_dict). The metadata dict may contain
    'language' and 'mimetype_detected' for Tika-processed files; it is
    always empty for plain-text files since we don't run detection on them.
    """
    if should_skip_content(str(path)):
        return "", {}
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            if sz > LARGE_FILE_LIMIT:
                log.info(f"Skipping content (exceeds hard cap {sz/1024/1024:.0f}MB): {path}")
                return "", {}
            if sz > MAX_TEXT_SIZE:
                if not large_files:
                    log.debug(f"Skipping content (>{MAX_TEXT_SIZE//1024//1024}MB, use --large-files): {path}")
                    return "", {}
                log.info(f"Large file content extraction ({sz/1024/1024:.0f}MB): {path}")
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            return raw.decode("utf-8", errors="replace"), {}
        elif ext in TIKA_EXTS:
            sz = path.stat().st_size
            if sz > LARGE_FILE_LIMIT:
                return "", {}
            if sz > MAX_TEXT_SIZE and not large_files:
                return "", {}
            return extract_via_tika(path, large=sz > MAX_TEXT_SIZE)
    except Exception as e:
        log.debug(f"Content extraction error {path}: {e}")
    return "", {}


# ── Document builder ─────────────────────────────────────────────────────────

def ts_to_solr(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

def file_to_doc(path: Path, large_files: bool = False) -> dict | None:
    try:
        if path.name.startswith("~$"):   # Office lock/temp files — always garbage
            return None
        s = path.stat()
        if stat.S_ISLNK(s.st_mode):   # skip symlinks to avoid loops
            return None
        ext = path.suffix.lower()
        mime, _ = mimetypes.guess_type(str(path))
        content, extract_meta = extract_content(path, large_files=large_files)
        sha = sha256_file(path, large_files=large_files)
        doc = {
            "id":              str(path),
            "filepath":        str(path),
            "filename":        path.name,
            "extension":       ext.lstrip(".") if ext else "",
            "directory":       str(path.parent),
            "size_bytes":      s.st_size,
            "mtime":           ts_to_solr(s.st_mtime),
            "mimetype":        mime or "application/octet-stream",
            "content":         content,
            "content_preview": content[:CONTENT_PREVIEW] if content else "",
            "owner":           str(s.st_uid),
        }
        if sha:
            doc["content_sha256"] = sha
        if extract_meta.get("language"):
            doc["language"] = extract_meta["language"]
        if extract_meta.get("mimetype_detected"):
            doc["mimetype_detected"] = extract_meta["mimetype_detected"]
        return doc
    except (PermissionError, FileNotFoundError, OSError) as e:
        log.debug(f"Skipping {path}: {e}")
        return None

# ── Crawlers ─────────────────────────────────────────────────────────────────

def crawl_full(roots: list[Path], exclude: set[Path]) -> Generator[Path, None, None]:
    for root in roots:
        for dirpath, dirs, files in os.walk(str(root), followlinks=False):
            dp = Path(dirpath)
            dirs[:] = sorted([
                d for d in dirs
                if d not in SKIP_DIRS
                and not d.startswith(".")
                and (dp / d).resolve() not in exclude
            ])
            for f in files:
                yield dp / f

def crawl_incremental(roots: list[Path], since_ts: float,
                       exclude: set[Path]) -> Generator[Path, None, None]:
    """
    Use find -newer for efficiency — avoids stat-ing every file in Python.
    Falls back to full crawl if find is unavailable.
    """
    import subprocess, tempfile

    # Write a reference timestamp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ts") as tf:
        tf_path = tf.name
    os.utime(tf_path, (since_ts, since_ts))

    try:
        for root in roots:
            exclude_args = []
            for ex in exclude:
                exclude_args += ["-path", str(ex), "-prune", "-o"]

            cmd = ["find", str(root)] + exclude_args + \
                  ["-newer", tf_path, "-type", "f", "-print0"]

            result = subprocess.run(cmd, capture_output=True)
            for fp in result.stdout.split(b"\0"):
                if fp:
                    yield Path(fp.decode("utf-8", errors="replace"))
    finally:
        os.unlink(tf_path)

# ── Find cache ───────────────────────────────────────────────────────────────

FIND_CACHE_CHECKPOINT = FIND_CACHE.with_suffix(".checkpoint")

def find_cache_valid(cache: Path = FIND_CACHE) -> bool:
    """True if cache exists and is younger than FIND_CACHE_MAX hours."""
    if not cache.exists():
        return False
    age_hours = (time.time() - cache.stat().st_mtime) / 3600
    if age_hours > FIND_CACHE_MAX:
        log.info(f"Find cache expired ({age_hours:.1f}h old, max {FIND_CACHE_MAX}h) — will rescan")
        return False
    log.info(f"Find cache valid ({age_hours:.1f}h old) — resuming from {cache}")
    return True

def write_find_cache(roots: list[Path], since_ts: float | None,
                     exclude: set[Path], cache: Path = FIND_CACHE):
    """
    Run find and write results to cache file, one path per line.
    Uses find -newer for incremental, plain os.walk for full.
    """
    import subprocess
    import tempfile

    log.info(f"Running filesystem scan → {cache}")
    cache.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file first — avoid partial cache on interruption
    tmp = cache.with_suffix(".tmp")

    with open(tmp, "w") as out:
        out.write(f"# fsearch find cache\n")
        out.write(f"# written: {datetime.datetime.utcnow().isoformat()}\n")
        out.write(f"# mode: {'incremental' if since_ts else 'full'}\n")

        if since_ts:
            # Incremental — use find -newer reference file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ts") as tf:
                tf_path = tf.name
            os.utime(tf_path, (since_ts, since_ts))
            try:
                for root in roots:
                    if _shutdown_requested:
                        break
                    exclude_args = []
                    for ex in exclude:
                        exclude_args += ["-path", str(ex), "-prune", "-o"]
                    cmd = ["find", str(root)] + exclude_args + \
                          ["-newer", tf_path, "-type", "f", "-print0"]
                    result = subprocess.run(cmd, capture_output=True)
                    for fp in result.stdout.split(b"\0"):
                        if fp:
                            out.write(fp.decode("utf-8", errors="replace") + "\n")
            finally:
                os.unlink(tf_path)
        else:
            # Full crawl — os.walk for SKIP_DIRS handling
            for root in roots:
                if _shutdown_requested:
                    break
                for dirpath, dirs, files in os.walk(str(root), followlinks=False):
                    if _shutdown_requested:
                        break
                    dp = Path(dirpath)
                    dirs[:] = sorted([
                        d for d in dirs
                        if d not in SKIP_DIRS
                        and not d.startswith(".")
                        and (dp / d).resolve() not in exclude
                    ])
                    for f in files:
                        out.write(str(dp / f) + "\n")

    if _shutdown_requested:
        tmp.unlink(missing_ok=True)
        log.info("Shutdown — discarding incomplete find cache")
        return

    tmp.rename(cache)   # atomic replace
    count = sum(1 for l in open(cache) if not l.startswith("#"))
    log.info(f"Find cache written: {count} files → {cache}")


def read_find_cache(cache: Path = FIND_CACHE) -> Generator[Path, None, None]:
    """Yield paths from cache. Skips comment lines."""
    with open(cache) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            yield Path(line)


def _checkpoint_path(cache: Path) -> Path:
    return cache.with_suffix(".checkpoint")

def read_checkpoint(cache: Path = FIND_CACHE) -> int:
    """Return number of files already processed in current cache."""
    cp = _checkpoint_path(cache)
    if cp.exists():
        try:
            return int(cp.read_text().strip())
        except ValueError:
            pass
    return 0

def write_checkpoint(n: int, cache: Path = FIND_CACHE):
    _checkpoint_path(cache).write_text(str(n))

def clear_checkpoint(cache: Path = FIND_CACHE):
    cp = _checkpoint_path(cache)
    if cp.exists():
        cp.unlink()


def crawl_from_cache(cache: Path = FIND_CACHE) -> Generator[Path, None, None]:
    """Yield paths from cache, skipping already-processed files."""
    skip = read_checkpoint(cache)
    if skip > 0:
        log.info(f"Resuming from checkpoint: skipping first {skip} files")
    count = 0
    for path in read_find_cache(cache):
        if count < skip:
            count += 1
            continue
        yield path
        count += 1

# ── Skip-if-unchanged: avoid re-indexing files Solr already has ──────────────

def fetch_indexed_meta(solr: pysolr.Solr) -> dict[str, tuple[int, str, bool]]:
    """
    Bulk-fetch (size_bytes, mtime, has_sha256) for every indexed document.
    Returns {filepath: (size_bytes, mtime_solr_str, has_sha256)}.
    Used to skip re-indexing unchanged files. A file is treated as
    "unchanged" only if size+mtime match AND the doc already has a
    content_sha256 — this lets a normal --full run backfill hashes into
    previously-indexed docs that lack them, without touching docs that
    already carry one.
    """
    log.info("Loading indexed file metadata from Solr for change detection...")
    meta: dict[str, tuple[int, str, bool]] = {}
    cursor = "*"
    while True:
        if _shutdown_requested:
            log.info("Shutdown — aborting metadata fetch, will skip change detection")
            return {}
        results = solr.search("*:*",
                              fl="id,size_bytes,mtime,content_sha256",
                              rows=5000,
                              sort="id asc",
                              cursorMark=cursor)
        for r in results:
            meta[r["id"]] = (r.get("size_bytes", -1),
                             r.get("mtime", ""),
                             bool(r.get("content_sha256")))
        new_cursor = results.nextCursorMark
        if new_cursor == cursor:
            break
        cursor = new_cursor
    with_hash = sum(1 for v in meta.values() if v[2])
    log.info(f"Loaded metadata for {len(meta):,} indexed files "
             f"({with_hash:,} already hashed, {len(meta) - with_hash:,} need backfill)")
    return meta


def _file_unchanged(path: Path, indexed_meta: dict[str, tuple[int, str, bool]]) -> bool:
    """
    True if the file's current size and mtime match what Solr already has
    AND the indexed doc already has a content_sha256. The hash check lets
    a --full (or any) run backfill hashes into older docs that lack one.
    """
    key = str(path)
    if key not in indexed_meta:
        return False
    try:
        s = path.stat()
        idx_size, idx_mtime, has_hash = indexed_meta[key]
        return (s.st_size == idx_size
                and ts_to_solr(s.st_mtime) == idx_mtime
                and has_hash)
    except OSError:
        return False


# ── Delete pass: remove Solr docs for files that no longer exist ─────────────

def _build_existing_set(roots: list[Path], exclude: set[Path]) -> set[str]:
    """
    Fast filesystem scan that only collects paths (no stat, no content).
    Returns a set of filepath strings for existence checking.
    Uses os.scandir recursively — much faster than individual Path.exists()
    calls because it reads directories sequentially (cache-friendly I/O).
    """
    import subprocess

    log.info("Building filesystem snapshot for purge pass...")
    existing: set[str] = set()

    for root in roots:
        if _shutdown_requested:
            break
        # Use find -type f for speed — one process per root, sequential dir reads
        exclude_args = []
        for ex in exclude:
            exclude_args += ["-path", str(ex), "-prune", "-o"]
        for skip in SKIP_DIRS:
            exclude_args += ["-name", skip, "-prune", "-o"]

        cmd = ["find", str(root)] + exclude_args + ["-type", "f", "-print0"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            for fp in result.stdout.split(b"\0"):
                if fp:
                    existing.add(fp.decode("utf-8", errors="replace"))
        except subprocess.TimeoutExpired:
            log.warning(f"Find timed out for {root} — falling back to partial snapshot")
        except Exception as e:
            log.warning(f"Find failed for {root}: {e}")

    log.info(f"Filesystem snapshot: {len(existing):,} files found")
    return existing


def purge_deleted(solr: pysolr.Solr, roots: list[Path] = None,
                  exclude: set[Path] = None, batch_size: int = 50000):
    """
    Delete Solr docs for files that no longer exist on disk.

    If roots/exclude are provided, builds a fast filesystem snapshot first
    and uses set membership for O(1) lookups (instead of one stat per doc).
    Falls back to per-file Path.exists() if roots are not available.
    """
    # Build snapshot if we have roots
    if roots and not _shutdown_requested:
        existing = _build_existing_set(roots, exclude or set())
        use_snapshot = True
    else:
        existing = None
        use_snapshot = False

    log.info("Running delete pass%s...",
             f" ({len(existing):,} files in snapshot)" if use_snapshot else "")
    cursor = "*"
    deleted = 0

    while True:
        if _shutdown_requested:
            log.info(f"Shutdown — aborting delete pass (purged {deleted} so far)")
            break
        results = solr.search("*:*",
                              fl="id",
                              rows=batch_size,
                              sort="id asc",
                              cursorMark=cursor)
        if use_snapshot:
            to_delete = [r["id"] for r in results if r["id"] not in existing]
        else:
            to_delete = [r["id"] for r in results if not Path(r["id"]).exists()]

        if to_delete:
            solr.delete(id=to_delete)
            deleted += len(to_delete)
            log.info(f"  Purged {deleted} deleted files so far...")
        new_cursor = results.nextCursorMark
        if new_cursor == cursor:
            break
        cursor = new_cursor
    solr.commit()
    log.info(f"Delete pass complete. Total purged: {deleted}")

# ── Batch add with error recovery ────────────────────────────────────────────

def safe_add(solr: pysolr.Solr, batch: list[dict], dry_run: bool) -> tuple[int, int]:
    """
    Try to add batch as a whole. On failure, retry one doc at a time
    so a single problematic file doesn't lose the whole batch.
    Returns (ok_count, fail_count).
    Pops docs during retry to free memory as each is processed.
    """
    if dry_run or not batch:
        n = len(batch)
        batch.clear()
        return n, 0
    try:
        solr.add(batch, commitWithin=10000)
        n = len(batch)
        batch.clear()
        return n, 0
    except Exception as e:
        log.warning(f"Batch POST failed ({len(batch)} docs): {e} — retrying individually")
        ok = failed = 0
        while batch:
            if _shutdown_requested:
                log.info(f"Shutdown — abandoning {len(batch)} remaining docs in batch")
                batch.clear()
                break
            doc = batch.pop(0)
            try:
                solr.add([doc], commitWithin=10000)
                ok += 1
            except Exception as e2:
                fp = doc.get("filepath", "?")
                log.error(f"Failed to index {fp}: {e2}")
                log_error(fp, str(e2))
                failed += 1
            del doc   # release content memory immediately
        # Force collection of serialized payloads and stale connection buffers
        gc.collect()
        log.info(f"Batch retry complete: {ok} succeeded, {failed} failed")
        return ok, failed

# ── Error log retry ──────────────────────────────────────────────────────────

def rotate_error_log() -> Path | None:
    """
    Move current error log to a timestamped temp path.
    Returns the rotated path, or None if no error log exists.
    """
    if not ERROR_LOG.exists():
        log.info("No error log found — nothing to retry")
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rotated = ERROR_LOG.with_name(f"{ERROR_LOG.stem}_{ts}.tmp")
    ERROR_LOG.rename(rotated)
    log.info(f"Rotated error log to {rotated} ({sum(1 for _ in open(rotated))} entries)")
    return rotated

def read_error_log(rotated: Path) -> list[str]:
    """Read filepaths from a rotated error log. Skips malformed lines."""
    paths = []
    with open(rotated) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                paths.append(parts[2])   # filepath is third field
            else:
                log.warning(f"Malformed error log line: {line!r}")
    return paths

def cleanup_rotated_log(rotated: Path, had_errors: bool):
    """
    Delete the rotated log if retry was clean.
    If new errors occurred, append the rotated log to the new error log
    so nothing is lost, then delete the rotated copy.
    """
    if had_errors:
        log.info(f"New errors occurred — merging {rotated.name} into {ERROR_LOG.name}")
        with open(ERROR_LOG, "a") as out, open(rotated) as src:
            out.write(f"# --- Carried over from {rotated.name} ---\n")
            out.write(src.read())
    rotated.unlink()
    log.info(f"Rotated log {rotated.name} cleaned up")

def run_retry(solr_url: str, large_files: bool, dry_run: bool):
    """
    Re-index files listed in the error log.
    Triages each entry: permanent failures go to skip list,
    transient failures are retried, and only still-failing transient
    errors cycle back to the error log.
    """
    rotated = rotate_error_log()
    if rotated is None:
        return

    # Read full entries (with reasons) for triage
    entries = []
    with open(rotated) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                entries.append((parts[0], parts[1], parts[2]))  # ts, reason, filepath

    log.info(f"Triaging {len(entries)} error log entries...")

    # First pass: separate permanent from retryable
    retryable = []
    permanent_count = 0
    missing_count = 0

    for ts, reason, filepath in entries:
        if _is_permanent_failure(reason):
            add_to_skip_content(filepath, reason)
            permanent_count += 1
        elif not Path(filepath).exists():
            add_to_skip_content(filepath, "file no longer exists")
            missing_count += 1
        else:
            retryable.append(filepath)

    if permanent_count or missing_count:
        log.info(f"Triaged: {permanent_count} permanent failures → skip list, "
                 f"{missing_count} missing files → skip list, "
                 f"{len(retryable)} to retry")

    if not retryable:
        log.info("No retryable files — nothing to do")
        rotated.unlink()
        return

    # Second pass: retry the transient failures
    log.info(f"Retrying {len(retryable)} previously failed files...")

    solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
    batch, ok_total, fail_total = [], 0, 0

    for filepath in retryable:
        doc = file_to_doc(Path(filepath), large_files=large_files)
        if doc:
            batch.append(doc)
        if len(batch) >= BATCH_SIZE:
            ok_n, fail_n = safe_add(solr, batch, dry_run)
            ok_total += ok_n
            fail_total += fail_n

    # Final partial batch
    ok_n, fail_n = safe_add(solr, batch, dry_run)
    ok_total += ok_n
    fail_total += fail_n

    if not dry_run:
        solr.commit()

    log.info(f"Retry complete: {ok_total} succeeded, {fail_total} still failing")
    # Only keep the rotated log data if transient failures remain
    cleanup_rotated_log(rotated, had_errors=fail_total > 0)

# ── Core index routine ────────────────────────────────────────────────────────

def _index_device_group(dev_id: int, cache: Path, solr_url: str,
                        dry_run: bool, large_files: bool,
                        indexed_meta: dict[str, tuple[int, str, bool]] | None = None,
                        ) -> tuple[int, int, int, int]:
    """
    Index all files from a single device's find cache.
    Returns (ok_total, skipped, fail_total, unchanged).
    Each device group gets its own Solr connection and checkpoint.
    """
    solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
    batch, total, skipped, ok_total, fail_total, unchanged = [], 0, 0, 0, 0, 0
    dev_label = f"dev:{dev_id}"

    for path in crawl_from_cache(cache):
        if indexed_meta and _file_unchanged(path, indexed_meta):
            unchanged += 1
            continue

        doc = file_to_doc(path, large_files=large_files)
        if doc:
            batch.append(doc)
            total += 1
        else:
            skipped += 1

        if len(batch) >= BATCH_SIZE:
            ok_n, fail_n = safe_add(solr, batch, dry_run)
            ok_total += ok_n
            fail_total += fail_n
            if not dry_run:
                write_checkpoint(total + skipped + unchanged, cache)

        if _shutdown_requested:
            log.info(f"[{dev_label}] Shutdown requested — stopping after current batch")
            break

    # Final partial batch
    ok_n, fail_n = safe_add(solr, batch, dry_run)
    ok_total += ok_n
    fail_total += fail_n

    if not dry_run:
        solr.commit()

    log.info(f"[{dev_label}] Done: indexed={ok_total} unchanged={unchanged} "
             f"skipped={skipped} errors={fail_total}")
    return ok_total, skipped, fail_total, unchanged


def run_index(roots, exclude_paths, incremental: bool, no_purge: bool,
              solr_url: str, dry_run: bool, large_files: bool,
              rebuild: bool = False):
    state = load_state()

    if rebuild and not dry_run:
        log.info("[bold red]REBUILD MODE[/bold red] — deleting all documents from Solr")
        solr_rebuild = pysolr.Solr(solr_url, always_commit=False, timeout=120)
        solr_rebuild.delete(q="*:*")
        solr_rebuild.commit()
        log.info("Solr index cleared — starting fresh full crawl")
        # Reset state so we do a full crawl
        state = {"last_run": None, "indexed_count": 0}
        save_state(state)
        incremental = False

    since_ts = None
    if incremental:
        last = state.get("last_run")
        if last:
            since_ts = datetime.datetime.fromisoformat(last).timestamp()
            log.info(f"Incremental mode: indexing files newer than {last}")
        else:
            log.info("No previous run found — falling back to full index")

    exclude = {Path(e).resolve() for e in exclude_paths}
    roots_p = [Path(r) for r in roots]

    # ── Detect devices ────────────────────────────────────────────────────────
    dev_groups = group_roots_by_device(roots_p)
    n_devices = len(dev_groups)

    if n_devices == 0:
        log.error("No valid root paths — nothing to index")
        return

    for dev_id, dev_roots in dev_groups.items():
        root_list = ", ".join(str(r) for r in dev_roots)
        log.info(f"Device {dev_id}: {root_list}")

    if n_devices > 1:
        log.info(f"Detected {n_devices} distinct filesystems — will parallelise find and index")

    # ── Find phase (parallel per device) ──────────────────────────────────────
    # Map each device to its cache path
    if n_devices == 1:
        # Single device — use the default cache path for backward compat
        dev_caches = {next(iter(dev_groups)): FIND_CACHE}
    else:
        dev_caches = {dev_id: _device_cache_path(dev_id) for dev_id in dev_groups}

    caches_to_build = {}
    for dev_id, cache in dev_caches.items():
        if find_cache_valid(cache):
            log.info(f"Device {dev_id}: using existing find cache")
        else:
            clear_checkpoint(cache)
            caches_to_build[dev_id] = cache

    if caches_to_build:
        if len(caches_to_build) == 1:
            dev_id = next(iter(caches_to_build))
            write_find_cache(dev_groups[dev_id], since_ts, exclude,
                             cache=caches_to_build[dev_id])
        else:
            log.info(f"Running {len(caches_to_build)} find scans in parallel...")
            with ThreadPoolExecutor(max_workers=len(caches_to_build)) as pool:
                futures = {
                    pool.submit(write_find_cache, dev_groups[dev_id], since_ts,
                                exclude, cache): dev_id
                    for dev_id, cache in caches_to_build.items()
                }
                for fut in as_completed(futures):
                    dev_id = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        log.error(f"Find scan failed for device {dev_id}: {e}")

    # ── Save state at start so interruptions leave a checkpoint ───────────────
    run_start = datetime.datetime.utcnow()
    if not dry_run:
        state["last_run"] = run_start.isoformat()
        save_state(state)

    # ── Fetch existing metadata for change detection ────────────────────────────
    if rebuild:
        indexed_meta = {}   # nothing in Solr to compare against
    else:
        solr_meta = pysolr.Solr(solr_url, always_commit=False, timeout=120)
        indexed_meta = fetch_indexed_meta(solr_meta)

    # ── Index phase (parallel per device) ─────────────────────────────────────
    ok_total = fail_total = skipped_total = unchanged_total = 0

    if n_devices == 1:
        # Single device — run inline with progress bar (preserves existing UX)
        dev_id = next(iter(dev_caches))
        cache = dev_caches[dev_id]
        solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
        batch, total, skipped, unchanged = [], 0, 0, 0

        with Progress(SpinnerColumn(),
                      TextColumn("[progress.description]{task.description}"),
                      MofNCompleteColumn(),
                      console=console) as prog:
            task = prog.add_task("Indexing files...", total=None)
            for path in crawl_from_cache(cache):
                if indexed_meta and _file_unchanged(path, indexed_meta):
                    unchanged += 1
                    continue

                doc = file_to_doc(path, large_files=large_files)
                if doc:
                    batch.append(doc)
                    total += 1
                else:
                    skipped += 1

                if len(batch) >= BATCH_SIZE:
                    ok_n, fail_n = safe_add(solr, batch, dry_run)
                    ok_total += ok_n
                    fail_total += fail_n
                    if not dry_run:
                        write_checkpoint(total + skipped + unchanged, cache)

                prog.update(task, completed=total,
                            description=f"[cyan]Indexed[/cyan] {path.name[:50]}")

                if _shutdown_requested:
                    log.info("Shutdown requested — stopping after current batch")
                    break

        ok_n, fail_n = safe_add(solr, batch, dry_run)
        ok_total += ok_n
        fail_total += fail_n
        skipped_total = skipped
        unchanged_total = unchanged

        if not dry_run:
            solr.commit()
    else:
        # Multiple devices — parallel workers, each with its own Solr connection
        log.info(f"Starting {n_devices} parallel index workers...")
        with ThreadPoolExecutor(max_workers=n_devices) as pool:
            futures = {
                pool.submit(_index_device_group, dev_id, dev_caches[dev_id],
                            solr_url, dry_run, large_files,
                            indexed_meta): dev_id
                for dev_id in dev_caches
            }
            for fut in as_completed(futures):
                dev_id = futures[fut]
                try:
                    ok, skip, fail, unch = fut.result()
                    ok_total += ok
                    skipped_total += skip
                    fail_total += fail
                    unchanged_total += unch
                except Exception as e:
                    log.error(f"Index worker for device {dev_id} failed: {e}")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if not dry_run:
        solr_cleanup = pysolr.Solr(solr_url, always_commit=False, timeout=120)
        if not no_purge:
            purge_deleted(solr_cleanup, roots=roots_p, exclude=exclude)
        for cache in dev_caches.values():
            clear_checkpoint(cache)
            cache.unlink(missing_ok=True)

    state["indexed_count"] = state.get("indexed_count", 0) + ok_total
    if not dry_run:
        save_state(state)

    log.info(
        f"[green]Done.[/green] Indexed: {ok_total}, Unchanged: {unchanged_total}, "
        f"Skipped: {skipped_total}, Errors: {fail_total}"
    )

# ── CLI ───────────────────────────────────────────────────────────────────────

def _worker(roots, exclude, full, rebuild, no_purge, purge_only, dry_run,
            solr_url, large_files, retry_errors):
    """
    Worker function — runs in a child process.
    Installs signal handlers and does all the actual indexing work.
    """
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        if purge_only:
            if not roots:
                raise click.UsageError("ROOTS required for --purge-only")
            roots_p = [Path(r) for r in roots]
            exclude_set = {Path(e).resolve() for e in exclude}
            solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
            purge_deleted(solr, roots=roots_p, exclude=exclude_set)
            return

        if retry_errors:
            run_retry(solr_url=solr_url, large_files=large_files, dry_run=dry_run)
            if not roots:
                return

        if roots:
            run_index(
                roots, exclude,
                incremental=not full and not rebuild,
                no_purge=no_purge,
                solr_url=solr_url,
                dry_run=dry_run,
                large_files=large_files,
                rebuild=rebuild,
            )
        elif not retry_errors:
            raise click.UsageError("ROOTS required unless --retry-errors or --stop is used")
    finally:
        release_lock()


def _run_in_child(roots, exclude, full, rebuild, no_purge, purge_only, dry_run,
                  solr_url, large_files, retry_errors):
    """
    Fork a child process to do the work. The parent supervises and handles
    SIGTERM by killing the child — this works even when the child is blocked
    in a C-level call (HTTP request, subprocess, etc.).
    """
    child = multiprocessing.Process(
        target=_worker,
        args=(roots, exclude, full, rebuild, no_purge, purge_only, dry_run,
              solr_url, large_files, retry_errors),
        daemon=False,
    )
    child.start()

    # Write the CHILD's PID to the lockfile — this is the PID that --stop targets
    LOCK_FILE.write_text(str(child.pid))
    log.info(f"Worker started (PID {child.pid})")

    # Parent signal handler: forward to child, then escalate
    def _parent_signal(signum, frame):
        name = signal.Signals(signum).name
        if child.is_alive():
            log.info(f"Received {name} — sending to worker PID {child.pid}")
            os.kill(child.pid, signum)
            # Give child a few seconds to finish gracefully
            child.join(timeout=5)
            if child.is_alive():
                log.warning(f"Worker did not stop — sending SIGKILL")
                child.kill()
                child.join(timeout=5)
        release_lock()
        sys.exit(1)

    signal.signal(signal.SIGTERM, _parent_signal)
    signal.signal(signal.SIGINT, _parent_signal)

    # Wait for child to finish
    child.join()

    # Clean up lock if child exited without releasing (crash, kill, etc.)
    if LOCK_FILE.exists():
        try:
            lock_pid = int(LOCK_FILE.read_text().strip())
            if lock_pid == child.pid:
                LOCK_FILE.unlink()
        except (ValueError, OSError):
            pass

    sys.exit(child.exitcode or 0)


@click.command()
@click.argument("roots", nargs=-1, required=False)
@click.option("-x","--exclude", multiple=True, help="Paths to exclude")
@click.option("--full",     is_flag=True, help="Force full re-index (ignore last_run)")
@click.option("--rebuild",  is_flag=True, help="Delete all Solr documents and re-index from scratch")
@click.option("--no-purge", is_flag=True, help="Skip the deleted-files purge pass")
@click.option("--purge-only", is_flag=True, help="Run only the delete pass (no indexing)")
@click.option("--dry-run",  is_flag=True, help="Parse and extract but don't write to Solr")
@click.option("--solr-url", default=SOLR_URL, show_default=True)
@click.option("--large-files", is_flag=True, default=False,
              help=f"Extract content from files >{MAX_TEXT_SIZE//1024//1024}MB "
                   f"(slower, longer Tika timeout of {LARGE_TIKA_TIMEOUT}s)")
@click.option("--retry-errors", is_flag=True, default=False,
              help="Re-index files from the error log, then clear it")
@click.option("--stop", is_flag=True, default=False,
              help="Stop a running indexer gracefully (sends SIGTERM)")
@click.option("--status", is_flag=True, default=False,
              help="Check if an indexer is currently running")
def main(roots, exclude, full, rebuild, no_purge, purge_only, dry_run, solr_url, large_files, retry_errors, stop, status):
    """Crawl ROOT paths and index (or incrementally update) Solr."""
    if stop:
        stop_running_indexer()
        return

    if status:
        if LOCK_FILE.exists():
            try:
                pid = int(LOCK_FILE.read_text().strip())
                os.kill(pid, 0)
                log.info(f"Indexer is running (PID {pid})")
            except (ProcessLookupError, ValueError):
                log.info("No indexer running (stale lockfile)")
        else:
            log.info("No indexer running")
        return

    if not acquire_lock():
        sys.exit(1)

    _run_in_child(roots, exclude, full, rebuild, no_purge, purge_only, dry_run,
                  solr_url, large_files, retry_errors)


if __name__ == "__main__":
    main()

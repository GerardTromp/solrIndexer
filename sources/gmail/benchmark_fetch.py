#!/usr/bin/env python3
"""
benchmark_fetch.py — measure Gmail API fetch performance in three modes.

Permanent regression-test backstop for sync.py's fetch path. See Phase
5.1.6 in .claude/context/09-source-abstraction-plan.md for the full
rationale. This tool is NOT a one-off — it lives in the repo and should
be re-run whenever fetch logic changes (auth library update, Google API
shift, concurrency mode re-tuning, quota policy change) to catch
regressions against a known baseline.

The three modes:

  serial    One messages.get at a time. Matches the current Phase 5.1
            sync.py implementation. Baseline for comparison.

  threads   ThreadPoolExecutor with N=5. Each worker builds its OWN
            service object on first use and caches it in thread-local
            storage. httplib2 (which googleapiclient uses under the
            hood) is not thread-safe when a single http object is
            shared — concurrent access triggers glibc heap
            corruption. Thread-local services are the correct
            pattern. Expected 3-5x speedup if Google doesn't rate-
            limit, zero speedup if they do.

  batch     googleapiclient.http.BatchHttpRequest with 50 sub-requests
            per HTTP call. Gmail recommends <=50. Individual sub-
            requests still count against quota, but the network round-
            trip cost amortizes. Expected 5-20x speedup if the API
            honors the batch semantics as documented.

Environment:
    FSEARCH_GMAIL_OUTPUT       required — used to locate state DB
    FSEARCH_GMAIL_CREDENTIALS  OAuth client JSON (default: ~/.config/fsearch/gmail_credentials.json)
    FSEARCH_GMAIL_TOKEN        cached refresh token (default: ~/.config/fsearch/gmail_token.json)

Usage:
    ./benchmark_fetch.py                  # default: 200 messages
    ./benchmark_fetch.py --n 500
    ./benchmark_fetch.py --modes serial,threads
    ./benchmark_fetch.py --threads 10     # override worker count

Output: stderr for progress, stdout for a pipe-friendly results table.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import BatchHttpRequest
    _DEPS_OK = True
except ImportError as _e:
    _DEPS_OK = False
    _DEPS_ERR = str(_e)

logging.basicConfig(
    level=os.environ.get("FSEARCH_GMAIL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("gmail-benchmark")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_DB_FILENAME = ".gmail_state.sqlite"
DEFAULT_N = 200
DEFAULT_THREADS = 5
DEFAULT_BATCH = 50

# Response fields we need. Matches what sync.py actually consumes:
# raw (the message body for base64 decode), internalDate (for the
# path layout), labelIds + threadId (for manifest metadata). Everything
# else is discarded. This is the same fields mask that sync.py would
# use after Phase 5.1.6 lands.
FETCH_FIELDS = "raw,internalDate,labelIds,threadId"


# ── Config & auth ───────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not _DEPS_OK:
        log.error(f"Google API libraries missing: {_DEPS_ERR}\n"
                  f"  pip install google-api-python-client google-auth-oauthlib")
        sys.exit(2)

    out = os.environ.get("FSEARCH_GMAIL_OUTPUT")
    if not out:
        log.error("Missing required env var: FSEARCH_GMAIL_OUTPUT")
        sys.exit(2)

    output_root = Path(out).resolve()
    state_db = output_root / STATE_DB_FILENAME
    if not state_db.exists():
        log.error(f"State DB not found at {state_db}. Run sync.py at "
                  f"least once before benchmarking.")
        sys.exit(2)

    creds_dir = Path.home() / ".config" / "fsearch"
    creds_path = Path(os.environ.get(
        "FSEARCH_GMAIL_CREDENTIALS", creds_dir / "gmail_credentials.json"))
    token_path = Path(os.environ.get(
        "FSEARCH_GMAIL_TOKEN", creds_dir / "gmail_token.json"))

    return {
        "state_db":   state_db,
        "creds_path": creds_path,
        "token_path": token_path,
    }


def _get_credentials(cfg: dict) -> "Credentials":
    if not cfg["token_path"].exists():
        log.error(f"No token at {cfg['token_path']}. Run 'sync.py --auth' first.")
        sys.exit(2)
    creds = Credentials.from_authorized_user_file(str(cfg["token_path"]), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Don't save the refreshed token — benchmark shouldn't mutate
        # production auth state. Next sync.py run will refresh again.
    return creds


# ── ID selection ────────────────────────────────────────────────────────────

def _select_ids(state_db: Path, n: int) -> list[str]:
    """
    Pull N Gmail message IDs at random from the state DB.

    An earlier version picked the top-N by internal_ms descending, but
    that produced a pathologically failure-prone sample: the newest
    rows in the DB are dominated by "migrated" entries whose internal
    timestamps all collapse to the same midnight-UTC value (derived
    from filename date prefixes), plus a handful of genuinely fresh
    fetches from whatever testing happened to populate the DB last.
    A concentrated window like that disproportionately includes
    recently-deleted messages (spam cleanup, "unsubscribe"-style
    pruning, etc.), so up to 45% of selected IDs can 404 on fetch.

    Random sampling across the whole table gives a much stabler
    baseline — deleted messages are diluted into the ~26k-entry
    population and failures converge to the long-term drift rate
    (a few percent).

    Uses Python's random.sample so the result is reproducible with
    --seed. (SQLite's ORDER BY RANDOM() is non-deterministic, which
    would break regression-test comparability.)
    """
    conn = sqlite3.connect(str(state_db))
    try:
        rows = conn.execute("SELECT msg_id FROM fetched").fetchall()
    finally:
        conn.close()
    all_ids = [row[0] for row in rows]
    if len(all_ids) <= n:
        log.warning(
            f"State DB has only {len(all_ids)} rows — benchmark will use all of them")
        return all_ids
    return random.sample(all_ids, n)


# ── Mode implementations ────────────────────────────────────────────────────

class FetchResult:
    """Accumulator for per-mode results."""
    def __init__(self, mode: str):
        self.mode = mode
        self.ok = 0
        self.failed = 0
        self.rate_limited = 0
        self.bytes_decoded = 0
        self.per_request_ms: list[float] = []
        self.total_ms = 0.0

    def summary(self) -> str:
        if not self.per_request_ms:
            return f"{self.mode:10s}  no successful fetches"
        sorted_ms = sorted(self.per_request_ms)
        p50 = sorted_ms[len(sorted_ms) // 2]
        p95 = sorted_ms[int(len(sorted_ms) * 0.95)]
        p99 = sorted_ms[min(int(len(sorted_ms) * 0.99), len(sorted_ms) - 1)]
        throughput = self.ok / (self.total_ms / 1000) if self.total_ms > 0 else 0
        return (
            f"{self.mode:10s}  "
            f"ok={self.ok:4d}  "
            f"failed={self.failed:3d}  "
            f"429s={self.rate_limited:3d}  "
            f"total={self.total_ms/1000:6.2f}s  "
            f"rate={throughput:6.1f}/s  "
            f"p50={p50:5.0f}ms  "
            f"p95={p95:5.0f}ms  "
            f"p99={p99:5.0f}ms  "
            f"bytes={self.bytes_decoded/1024/1024:6.2f}MB"
        )


def _is_rate_limit(e: HttpError) -> bool:
    """Return True if the HttpError looks like rate-limiting."""
    if e.resp.status == 429:
        return True
    if e.resp.status == 403:
        msg = str(e).lower()
        return ("ratelimitexceeded" in msg or
                "userratelimitexceeded" in msg)
    return False


def _decode_raw(raw_str: str) -> int:
    """Decode base64url and return the byte count (discards content)."""
    if not raw_str:
        return 0
    return len(base64.urlsafe_b64decode(raw_str.encode("ascii")))


def mode_serial(service, ids: list[str]) -> FetchResult:
    """Baseline: one messages.get at a time, sequential."""
    r = FetchResult("serial")
    start = time.monotonic()
    for mid in ids:
        t0 = time.monotonic()
        try:
            msg = service.users().messages().get(
                userId="me", id=mid, format="raw",
                fields=FETCH_FIELDS).execute()
        except HttpError as e:
            if _is_rate_limit(e):
                r.rate_limited += 1
            r.failed += 1
            continue
        t1 = time.monotonic()
        r.per_request_ms.append((t1 - t0) * 1000)
        r.bytes_decoded += _decode_raw(msg.get("raw", ""))
        r.ok += 1
    r.total_ms = (time.monotonic() - start) * 1000
    return r


def _fetch_one_threadlocal(tls, service_factory, mid: str
                           ) -> tuple[str, dict | None, HttpError | None, float]:
    """
    Worker function for ThreadPoolExecutor. Builds a per-thread
    googleapiclient service object on first call and caches it in
    thread-local storage. httplib2 is not safe for concurrent access
    via a shared service.
    """
    svc = getattr(tls, "service", None)
    if svc is None:
        svc = service_factory()
        tls.service = svc
    t0 = time.monotonic()
    try:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="raw",
            fields=FETCH_FIELDS).execute()
        return mid, msg, None, (time.monotonic() - t0) * 1000
    except HttpError as e:
        return mid, None, e, (time.monotonic() - t0) * 1000


def mode_threads(service_factory, ids: list[str], workers: int) -> FetchResult:
    """
    ThreadPoolExecutor with N workers, each using its own service
    object. Takes a factory (zero-arg callable returning a service)
    rather than a service, so this function can construct them
    lazily per thread.
    """
    r = FetchResult(f"threads-{workers}")
    tls = threading.local()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_fetch_one_threadlocal, tls, service_factory, mid)
            for mid in ids
        ]
        for fut in as_completed(futures):
            _mid, msg, err, elapsed_ms = fut.result()
            if err is not None:
                if _is_rate_limit(err):
                    r.rate_limited += 1
                r.failed += 1
                continue
            r.per_request_ms.append(elapsed_ms)
            r.bytes_decoded += _decode_raw(msg.get("raw", ""))
            r.ok += 1
    r.total_ms = (time.monotonic() - start) * 1000
    return r


def mode_batch(service, ids: list[str], batch_size: int) -> FetchResult:
    """BatchHttpRequest with configurable sub-request count."""
    r = FetchResult(f"batch-{batch_size}")
    start = time.monotonic()

    # We cannot measure per-request latency meaningfully in batch mode
    # because every sub-request resolves when the whole batch HTTP call
    # returns. Record the per-batch time and attribute it evenly.
    def _callback(request_id, response, exception):
        if exception is not None:
            if isinstance(exception, HttpError) and _is_rate_limit(exception):
                r.rate_limited += 1
            r.failed += 1
            return
        r.bytes_decoded += _decode_raw(response.get("raw", ""))
        r.ok += 1

    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        batch_start = time.monotonic()
        batch = service.new_batch_http_request(callback=_callback)
        for mid in chunk:
            batch.add(service.users().messages().get(
                userId="me", id=mid, format="raw",
                fields=FETCH_FIELDS))
        try:
            batch.execute()
        except HttpError as e:
            # Whole batch failed before any sub-request ran. Every item
            # in the chunk is a failure.
            log.warning(f"Batch of {len(chunk)} failed entirely: {e}")
            if _is_rate_limit(e):
                r.rate_limited += len(chunk)
            r.failed += len(chunk)
            continue
        batch_elapsed = (time.monotonic() - batch_start) * 1000
        # Attribute batch-level latency evenly across sub-requests for
        # the percentile stats to be comparable to serial mode.
        per = batch_elapsed / max(len(chunk), 1)
        for _ in chunk:
            r.per_request_ms.append(per)
    r.total_ms = (time.monotonic() - start) * 1000
    return r


# ── Main ───────────────────────────────────────────────────────────────────

def _parse_argv() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure Gmail API fetch performance in three modes")
    p.add_argument("--n", type=int, default=DEFAULT_N,
                   help=f"Number of messages to fetch [{DEFAULT_N}]")
    p.add_argument("--modes", default="serial,threads,batch",
                   help="Comma-separated modes to run [serial,threads,batch]")
    p.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                   help=f"Worker count for the threads mode [{DEFAULT_THREADS}]")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                   help=f"Sub-request count for the batch mode [{DEFAULT_BATCH}]")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for ID ordering (for reproducibility)")
    return p.parse_args()


def main() -> int:
    args = _parse_argv()
    cfg = _load_config()
    creds = _get_credentials(cfg)

    # Each mode gets its own service object so there's no lingering
    # HTTP connection state between modes.
    def _mk_service():
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # Seed BEFORE selecting IDs so random.sample in _select_ids is
    # reproducible. Without this the seed only affects the per-mode
    # shuffles below, not the initial selection.
    if args.seed is not None:
        random.seed(args.seed)

    ids = _select_ids(cfg["state_db"], args.n)
    if not ids:
        log.error("No message IDs available in state DB")
        return 2

    log.info(f"Benchmark: {len(ids)} messages, modes={args.modes}")

    modes_to_run = [m.strip() for m in args.modes.split(",") if m.strip()]
    results: list[FetchResult] = []

    for mode_name in modes_to_run:
        # Shuffle per run so no mode benefits from Gmail's server-side
        # cache warming from an earlier mode's fetches of the same IDs
        # in order. Using the same ID set keeps the comparison fair.
        shuffled = ids.copy()
        random.shuffle(shuffled)

        log.info(f"  running mode={mode_name}...")
        service = _mk_service()
        t_mode_start = time.monotonic()
        try:
            if mode_name == "serial":
                r = mode_serial(service, shuffled)
            elif mode_name == "threads":
                r = mode_threads(_mk_service, shuffled, args.threads)
            elif mode_name == "batch":
                r = mode_batch(service, shuffled, args.batch)
            else:
                log.warning(f"    unknown mode: {mode_name}")
                continue
        except Exception as e:
            log.error(f"    mode={mode_name} crashed: {e}")
            continue
        mode_elapsed = time.monotonic() - t_mode_start
        log.info(
            f"    mode={mode_name} done in {mode_elapsed:.1f}s "
            f"(ok={r.ok} failed={r.failed} 429s={r.rate_limited})")
        results.append(r)

    # ── Results table to stdout ─────────────────────────────────────────────
    print()
    print(f"Gmail fetch benchmark — {len(ids)} messages, "
          f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 120)
    for r in results:
        print(r.summary())
    print()

    # ── Decision rules (matches the plan doc) ───────────────────────────────
    baseline = next((r for r in results if r.mode == "serial"), None)
    if baseline and baseline.total_ms > 0:
        baseline_s = baseline.total_ms / 1000
        print(f"Decision rules (vs serial baseline = {baseline_s:.1f}s):")
        print(f"  - batch-*   >5x faster, zero 429s  =>  ship batch")
        print(f"  - threads-* >3x faster, zero 429s  =>  ship threads")
        print(f"  - otherwise                         =>  keep serial")
        print()
        for r in results:
            if r.mode == "serial" or r.total_ms == 0:
                continue
            ratio = baseline_s / (r.total_ms / 1000)
            limits_free = r.rate_limited == 0
            threshold = 5 if r.mode.startswith("batch") else 3
            meets = ratio >= threshold and limits_free
            status = "MEETS" if meets else "below"
            print(
                f"  {r.mode:10s}  {ratio:4.1f}x faster  "
                f"rate_limited={r.rate_limited:3d}  "
                f"threshold={threshold}x  {status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

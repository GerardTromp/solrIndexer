# solrIndexer Development Chat — 2026-03-28 to 2026-03-30

## Session Overview

Iterative development of a filesystem indexer (Apache Solr + Tika) with search CLI, web GUI, and operational tooling. Started from an existing single-threaded indexer and search CLI.

---

## 1. Parallel Device Detection and Indexing

**Request:** Determine which root directories are on different filesystems and execute find/index processes in parallel per device.

**Solution:**

- Added `group_roots_by_device()` using `os.stat().st_dev` to partition roots by filesystem device ID
- Per-device find caches (`find_cache_dev{id}.txt`)
- Parameterized all cache functions to accept a cache path
- `ThreadPoolExecutor` for parallel find scans and parallel index workers
- Single-device path preserves existing Rich progress bar UX

**Key functions:**

- `group_roots_by_device()` — partitions roots by st_dev
- `_device_cache_path()` — per-device cache filenames
- `_index_device_group()` — worker function for one device group

---

## 2. Change Detection (Skip Unchanged Files)

**Problem:** Re-indexing files that were previously indexed — wasteful when size and mtime haven't changed.

**Solution:**

- `fetch_indexed_meta()` — bulk fetch `{filepath: (size_bytes, mtime)}` from Solr using cursor-mark pagination
- `_file_unchanged()` — compare file's current stat against Solr's stored values
- Both single-device and multi-device paths skip unchanged files
- Checkpoint math updated to include unchanged count

**Result:** Full re-indexes now skip all unchanged files. Summary reports "Unchanged" count.

---

## 3. Web GUI (fsearch_web.py + static/search.html)

**Request:** HTML form with advanced search query interface, draggable rows for Boolean organization, content preview on click.

**Files created:**

- `fsearch_web.py` — Flask backend with `/api/search` and `/api/content` endpoints
- `static/search.html` — Single-file UI (HTML + CSS + JS, no build step)

**Features:**

- Dynamic search rows: join operator (AND/OR/NOT) + field selector + value input
- Drag-and-drop reorder via handle
- "NOT→end" button moves all NOT rows to bottom
- Field types: text, name, ext, path, dir, content, size, since, before, raw
- Content preview: click result row to expand stored preview
- Highlight snippets with `<mark>` tags from Solr
- Keyboard shortcuts: Ctrl+Enter search, Ctrl+Shift+A add row, ? toggle help
- Copy query / CLI command buttons for passing queries to fsearch CLI

---

## 4. Content Preview Field

**Problem:** `content` field was `stored=false` — searchable but not retrievable for GUI preview.

**Solution (revised):** Instead of storing full content:

- Added `content_preview` field: `text_general`, `stored:true`, `indexed:true`
- Populated at index time with first 1KB (`CONTENT_PREVIEW = 1024`)
- Main `content` field stays `stored:false, indexed:true`
- Preview returned inline with search results (no separate API call)
- Changed from `string` to `text_general` to enable filtering/existence queries

**Schema change:**

```bash
curl -s -X POST "http://localhost:8983/solr/filesystem/schema" \
  -H 'Content-Type: application/json' \
  -d '{"add-field": {"name":"content_preview", "type":"text_general", "stored":true, "indexed":true}}'
```

---

## 5. Tika Health Monitoring and Auto-Restart

**Problem:** Tika died mid-run (corrupt PDF crashed PDFBox), indexer silently recorded empty content for all subsequent files.

**Root cause:** `java.io.IOException: Missing root object specification in trailer` in PDFBox, cascading to thread pool exhaustion.

**Solution:**

- Consecutive failure tracking (`_TIKA_FAILURE_THRESHOLD = 5`)
- Liveness probe (`check_tika_alive()`)
- Auto-restart: `_restart_tika()` — kills zombie, rotates log, starts fresh JVM
- Max 3 restarts per indexer run
- Skip HTTP calls entirely when Tika declared dead
- Recovery detection resets counters

**Additional fixes:**

- Correct MIME types sent to Tika (`_TIKA_MIME` dict) instead of `application/octet-stream`
- Tika error detail captured from response body for error log
- Failed files logged via `log_error()` for `--retry-errors`

---

## 6. Log Rotation

**In `run_index.sh`:**

- `rotate_log()` function: size-based rotation (10MB default), keeps 5 copies, gzip in background
- Tika log rotated with timestamp on each Tika restart
- Applied to both `tika.log` and `indexer.log`

---

## 7. SIGTERM / Shutdown Fix

**Problem:** Indexer was non-responsive to SIGTERM — signal acknowledged in output but process continued running. Python's signal handler only runs between bytecode instructions; blocking C-level calls (requests.put, subprocess.run, socket reads) don't yield.

**Solution — parent/child process model:**

```
Parent (supervisor)
  ├── acquires lock
  ├── forks child, writes child PID to lockfile
  ├── waits on child.join()
  ├── on SIGTERM: forwards to child, waits 5s, then SIGKILL
  └── cleans up lockfile

Child (worker)
  ├── installs signal handlers
  ├── does all indexing/purge/retry work
  └── best-effort graceful shutdown via _shutdown_requested flag
```

**Additional shutdown checks added to all blocking paths:**

- `extract_via_tika()` — early return before HTTP call
- `safe_add()` — check between individual retries
- `purge_deleted()` — check each page
- `fetch_indexed_meta()` — check each page, return {} on abort
- `write_find_cache()` — check per root and per directory in os.walk
- Incomplete find cache discarded on shutdown

**Second SIGTERM = force exit:** `os._exit(1)` with lock cleanup.

---

## 8. --rebuild Flag

**Problem:** After Tika crash, 800K+ docs had correct size/mtime but empty content. Change detection would skip them all as "unchanged" on --full.

**Solution:** `--rebuild` flag:

- Deletes all documents from Solr (`delete q=*:*`)
- Resets indexer state (clears `last_run`)
- Skips change detection (empty index, nothing to compare)
- Forces full crawl

```bash
python fs_indexer.py --rebuild /home/$USER /mnt/wd1/GT /mnt/d/GT
```

---

## 9. Error Triage (triage_errors.py)

**Problem:** Large error log with mix of retryable (Tika was down) and permanent (encrypted/corrupt) failures.

**Solution:** Standalone script `triage_errors.py`:

- Reads error log
- Re-probes each file against Tika to capture actual Java exception
- Classifies using `PERMANENT_MARKERS` (EncryptedDocumentException, Missing root object, etc.)
- Outputs `retryable.log` and `permanent.log` in same tab-separated format
- Retryable log can be copied to error log for `--retry-errors`

```bash
python triage_errors.py
cp retryable.log /mnt/wd1/solr/logs/index_errors.log
python fs_indexer.py --retry-errors
```

---

## 10. Tika Heap Management

**Problem:** Large PDFs exhaust Tika's JVM heap (default 512m), causing OOM and cascading failures. But 3 parallel indexers with large heaps would overload the system.

**Solution in `run_index.sh`:**

- `--tika-heap` flag: `run_index.sh --tika-heap 4g`
- `get_running_tika_heap()` — reads `-Xmx` from `/proc/PID/cmdline`
- `heap_to_bytes()` — converts heap specs for comparison
- If requested > running: kills and restarts Tika with larger heap
- After indexing: tears down override instance, restarts at default 512m
- `start_tika()` and `stop_tika()` helper functions

```bash
# Normal daily cron
run_index.sh

# Retry large PDFs with extra heap
pkill -f tika-server  # if needed
run_index.sh --tika-heap 4g
```

---

## 11. Fast Purge Pass

**Problem:** `purge_deleted()` called `Path.exists()` on 800K+ files individually — extremely slow random I/O.

**Solution:**

- `_build_existing_set()` — runs `find -type f -print0` per root, builds `set[str]`
- Purge loop uses `r["id"] not in existing` — O(1) set lookup, zero disk I/O
- `find` reads directories sequentially (cache-friendly) vs random stat calls
- Falls back to per-file `Path.exists()` if roots not provided
- `--purge-only` CLI flag for standalone purge without indexing

```bash
python fs_indexer.py --purge-only /home/$USER /mnt/wd1/GT /mnt/d/GT
```

---

## 12. CLI Query from GUI

**Feature:** Copy generated Solr query from web GUI to use with `fsearch` CLI.

- `fsearch -q 'QUERY'` accepts raw Solr/Lucene query passthrough
- GUI shows generated query in status bar after each search
- Two copy buttons: raw query string, or full `fsearch` CLI command
- `copyCli()` escapes single quotes for shell and includes limit/sort options

---

## 13. Git Repository

Created git repo with `.gitattributes` (LF line endings) and pushed to:
https://github.com/GerardTromp/solrIndexer

---

## Files Modified/Created

| File                    | Description                                                                                                |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `fs_indexer.py`         | Main indexer — parallel devices, change detection, Tika health, parent/child shutdown, rebuild, purge-only |
| `fsearch.py`            | CLI search tool (unchanged)                                                                                |
| `fsearch_web.py`        | Flask web GUI backend                                                                                      |
| `static/search.html`    | Web GUI frontend                                                                                           |
| `triage_errors.py`      | Error log triage tool                                                                                      |
| `run_index.sh`          | Cron wrapper — log rotation, Tika heap management                                                          |
| `setup/setup_schema.sh` | Schema with content_preview field                                                                          |
| `.gitattributes`        | LF line endings                                                                                            |
| `.gitignore`            | Exclude logs, caches, build artifacts                                                                      |

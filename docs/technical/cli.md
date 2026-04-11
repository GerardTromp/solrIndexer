# CLI tools

## `fsearch` — search tool

The CLI search frontend. Takes Boolean filters and prints matching
documents as a Rich table, CSV, TXT, or JSON.

### Basic queries

```bash
# Full-text
fsearch "BRCA1"

# Filename glob (repeatable = OR)
fsearch --name "*.vcf" --name "*.vcf.gz"

# Extension (comma-separated within, repeatable = OR)
fsearch --ext py,r,R --ext sh

# Path substring (auto-wildcarded)
fsearch --path NLM_CDE --ext py

# Content regex
fsearch --content "/p[._]?adj\s*<\s*0\.05/" --ext py,r

# Size + date
fsearch --size ">10MB" --before 2023-06-01
fsearch --since 2024-01-01 --name "*.vcf"
```

### Boolean combinations

```bash
# AND across clause groups (default)
fsearch --path NLM_CDE --ext py --content pandas

# OR across clause groups
fsearch --ext py --or --ext r --path data

# NOT (negation)
fsearch --ext py --not-name "test_*"
fsearch --content pandas --not-path site-packages

# Show the generated Solr query without running it
fsearch --show-query --name "*.py" --path work
```

### Raw Solr query (expert)

```bash
fsearch -q 'content:GATK AND filename:*hg38* AND size_bytes:[1000000 TO *]'
fsearch -q 'language:en AND mimetype_detected:application/pdf' -l 100
```

### Output modes

```bash
# Default: Rich table with highlight snippets
fsearch "pandas"

# Paths only (one per line, suitable for piping)
fsearch -Q "pandas" | xargs grep "DataFrame"

# JSON to stdout
fsearch --json --ext py --limit 200

# Export to file — format inferred from extension
fsearch --ext py --export python_files.csv
fsearch -Q "pandas" --export paths.txt
fsearch --content BRCA1 --export hits.json --limit 5000

# Override format regardless of extension
fsearch --ext log --export /tmp/out --format txt
```

CSV exports carry all columns:
`filepath, filename, extension, size_bytes, mtime, directory,
content_sha256, language, mimetype_detected`.

### Full flag reference

```text
usage: fsearch [OPTIONS] [TEXT]

Positional:
  text                      Free text / regex (/pattern/) / Boolean query

Repeatable match filters (multiple = OR within field):
  -n, --name GLOB           Filename glob
  -e, --ext LIST            Extensions (comma-sep within, repeatable)
  -d, --dir DIR             Directory (exact, or trailing / for prefix)
  -p, --path GLOB           Full filepath (glob/*auto*, /regex/)
  -c, --content TEXT        File content (supports /regex/)

Negation filters (always AND NOT):
  -N, --not-name GLOB
  -E, --not-ext LIST
  -D, --not-dir DIR
  -P, --not-path GLOB
  -C, --not-content TEXT

Boolean control:
  --or                      Join clause groups with OR instead of AND

Size / date:
  -s, --size RANGE          e.g. >10MB, <1GB, >=500KB
      --since YYYY-MM-DD    Modified after
      --before YYYY-MM-DD   Modified before

Raw / output:
  -q, --query RAW           Raw Solr/Lucene query string (expert)
  -l, --limit N             Max results [50]
      --sort SORT           score desc | mtime desc | size_bytes asc | ...
      --highlight / --no-highlight
  -Q, --quiet               Print paths only
      --json                JSON output
  -o, --export FILE         Export to FILE (format inferred from extension)
      --format {csv,txt,json}   Explicit export format
      --show-query          Print the generated Solr query (debug)
      --solr-url URL        Override Solr endpoint
```

---

## `fs_indexer.py` — indexer

The crawler and Solr writer. Normally runs via `run_index.sh` under
cron, but also callable directly for on-demand indexing and testing.

### Common invocations

```bash
# Index every source in sources.yaml (typical nightly run)
fs_indexer.py

# Index one source only
fs_indexer.py --source gmail

# Print the resolved source list and exit
fs_indexer.py --list-sources

# Use a specific config file
fs_indexer.py --sources /etc/fsearch/custom.yaml

# Force a full re-index (ignores last_run)
fs_indexer.py --full

# Rebuild from scratch — DELETES all Solr docs first
fs_indexer.py --rebuild

# Dry-run: walk and extract but don't write to Solr
fs_indexer.py --dry-run --source filesystem

# Legacy mode: positional roots = one "legacy-fs" source
fs_indexer.py /home/gerard /mnt/wd1/GT

# Retry files from the error log
fs_indexer.py --retry-errors

# Stop a running indexer gracefully
fs_indexer.py --stop

# Check if an indexer is currently running
fs_indexer.py --status
```

### Full flag reference

```text
usage: fs_indexer.py [OPTIONS] [ROOTS]...

Options:
  -x, --exclude TEXT        Paths to exclude
      --full                Force full re-index (ignore last_run)
      --rebuild             Delete all Solr documents and re-index
      --no-purge            Skip the deleted-files purge pass
      --purge-only          Run only the delete pass
      --dry-run             Extract but don't write to Solr
      --solr-url URL        [default: http://localhost:8983/solr/filesystem]
      --large-files         Extract content from files >20MB
      --retry-errors        Re-index files from the error log, then clear it
      --stop                Stop a running indexer gracefully (SIGTERM)
      --status              Check if an indexer is currently running
      --sources PATH        Path to sources.yaml [/opt/fsearch/sources.yaml]
      --source NAME         Run only the named source from sources.yaml
      --list-sources        Parse sources.yaml and print the list
```

### Lifecycle and state

- **One indexer at a time** — guarded by
  `/mnt/wd1/solr/indexer.lock`, a PID file. Stale locks (dead PID)
  are taken over automatically.
- **Graceful shutdown** — `--stop` or SIGTERM triggers a flag that
  the inner loops check between files. The current batch finishes,
  then the indexer commits and exits cleanly. Escalates to SIGKILL
  after ~5 seconds if the worker is blocked in C code (Tika HTTP
  call).
- **Checkpoints** — the find cache is checkpointed every 300 files
  so an interrupted run resumes from near the last commit.

---

## `fsearch_web.py` — web GUI

Flask backend for the web search UI. Normally run as a long-lived
service via systemd or `nohup` (example in `run_index.sh` comments).

```bash
fsearch_web.py                       # default port 8080, 127.0.0.1
fsearch_web.py --port 9090           # alternate port
fsearch_web.py --host 0.0.0.0        # listen on all interfaces
fsearch_web.py --solr-url http://other:8983/solr/filesystem
```

Once running, visit `http://127.0.0.1:8080/` to use the search UI.
See [HTTP API](api.md) for the endpoints and payloads.

---

## Source helpers

### `sources/pst/extract.py`

Pull source for archived PST files. Configured via env vars, typically
set in `run_index.sh` or a cron file.

```bash
FSEARCH_PST_INPUT_DIR="/mnt/c/Users/gerard/Documents/Outlook Files" \
FSEARCH_PST_OUTPUT=/mnt/wd1/sources/pst \
/opt/fsearch/sources/pst/extract.py
```

See the [PST wiring vignette](../vignettes/pst-wiring.md).

### `sources/gmail/sync.py`

Pull source for a Gmail mailbox. Requires one-time GCP OAuth setup.

```bash
# First-time authorization
FSEARCH_GMAIL_OUTPUT=/mnt/wd1/sources/gmail \
/opt/fsearch/sources/gmail/sync.py --auth

# Subsequent headless runs
FSEARCH_GMAIL_OUTPUT=/mnt/wd1/sources/gmail \
/opt/fsearch/sources/gmail/sync.py
```

See the [Gmail wiring vignette](../vignettes/gmail-wiring.md).

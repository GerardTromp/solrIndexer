# Export & duplicate detection

The CLI and web GUI share two features worth walking through: **bulk
export** of filtered results to a file, and **duplicate detection**
via the content hash field.

## Export

Both `fsearch` (CLI) and the web GUI can dump a filtered result set
to CSV, TXT, or JSON.

### From the CLI

```bash
# CSV (all columns) — format inferred from extension
fsearch --ext py --path NLM_CDE -o python_files.csv

# TXT (filepath only, one per line)
fsearch --name "*.vcf" --limit 100000 -o vcfs.txt

# JSON (same as --json but to a file)
fsearch --content BRCA1 -o hits.json --limit 5000

# Explicit format (overrides extension)
fsearch --ext log -o /tmp/out --format txt
```

Defaults and limits:

- The CLI default `--limit` is 50. **Raise it for exports** or you'll
  only get 50 rows. `--limit 100000` is a reasonable ceiling.
- CSV includes all columns: `filepath, filename, extension, size_bytes,
  mtime, directory, content_sha256, language, mimetype_detected`.
- TXT is the fastest format and the most useful for piping into
  other tools (`xargs grep ...`, rsync file-list, etc.).

### From the web GUI

The toolbar has **Export CSV** and **Export TXT** buttons alongside
the **Search** button. A separate **Export max** input controls how
many rows get exported (default 10,000, hard cap 100,000 server-side).

!!! tip
    Set **Export max** higher than the on-screen `Limit`. The display
    limit keeps the GUI responsive for browsing; the export limit is
    independent so you can preview 50 rows on screen while exporting
    the full 10k set.

Click either button, the browser downloads a file named
`fsearch_YYYYMMDD_HHMMSS.{ext}`. Nothing is cached server-side, so
repeated clicks produce fresh snapshots.

### Over the HTTP API

```bash
curl -X POST http://127.0.0.1:8080/api/export \
  -H "Content-Type: application/json" \
  -d '{
    "rows": [{"field": "ext", "value": "py", "join": "AND", "negate": false}],
    "format": "csv",
    "limit": 10000
  }' \
  -o python_files.csv
```

See [HTTP API](../technical/api.md) for full request/response details.

---

## Duplicate detection

Every Solr doc carries a `content_sha256` field — a SHA-256 over the
file's raw bytes (streamed, memory-aware chunking). Two files with
the same hash have bit-identical contents regardless of path, name,
mtime, or size.

### Finding duplicates of one file

**From the web GUI**: click the **⧉** icon on any result row. The
query gets replaced with a raw `content_sha256:"<hash>"` clause and
re-runs, showing every doc that shares the hash in the standard
result table.

**From the CLI**:

```bash
# First, find a file's hash
fsearch -q 'filename:main.py' --limit 1 --json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["content_sha256"])'
# e8e493a1674976d5cbc4ae84baeb5f732dc846d8c6dea0e479003ba94a9fc3f4

# Look up everything with that hash
fsearch -q 'content_sha256:"e8e493a1674976d5cbc4ae84baeb5f732dc846d8c6dea0e479003ba94a9fc3f4"'
```

**From the HTTP API**:

```bash
curl -X POST http://127.0.0.1:8080/api/duplicates \
  -H "Content-Type: application/json" \
  -d '{"hash": "e8e493a1674976d5cbc4ae84baeb5f732dc846d8c6dea0e479003ba94a9fc3f4"}'
```

### Enumerating all duplicate groups

Useful for finding systemic waste — e.g., "which files have 5+
copies scattered across my filesystem?"

```bash
curl -X POST http://127.0.0.1:8080/api/duplicates \
  -H "Content-Type: application/json" \
  -d '{"min_count": 5, "limit": 50}' \
  | python3 -m json.tool
```

Response shape:

```json
{
  "total_groups": 23,
  "groups": [
    {"hash": "e8e4...", "count": 17},
    {"hash": "a1b2...", "count": 12},
    ...
  ]
}
```

Then drill into one group with a follow-up Mode-1 call:

```bash
curl -X POST http://127.0.0.1:8080/api/duplicates \
  -H "Content-Type: application/json" \
  -d '{"hash": "e8e4..."}' \
  | python3 -m json.tool
```

### Cross-source deduplication

The hash is source-agnostic — a PDF attached to an email (from the
Gmail or PST source) has the same hash as the same PDF sitting on
your filesystem. Finding cross-source duplicates just works:

```bash
# All files sharing a hash, grouped by source
fsearch -q 'content_sha256:"<hash>"' --limit 100
```

Useful for questions like "I have this document attached to three
emails and sitting in two project folders — which copies can I
safely delete?"

### Hash backfill on existing docs

If you installed fsearch before Phase 0.2 (the hashing phase), your
docs have no `content_sha256`. The hash-aware incremental check
(`_file_unchanged` requires hash presence) means a plain
`fs_indexer.py --full` run will naturally backfill hashes into every
doc that lacks one.

**Cost**: backfilled docs also re-run Tika extraction because the
content field isn't `stored=true`. On a large corpus this can take
hours on the first backfill pass. Options:

- **Let it happen organically** via nightly cron — new/changed
  files pick up hashes immediately; unchanged files backfill as
  they naturally get touched
- **Force a full backfill** with `fs_indexer.py --full` (or
  `fs_indexer.py --full --source <name>` to scope to one source)
- **Backfill specific paths** by using positional roots: `fs_indexer.py
  --full /mnt/d/GT/small_subdir`

The indexer's startup log reports the current state:

```
INFO Loaded metadata for 623,564 indexed files (412,301 already hashed, 211,263 need backfill)
```

So you can always see how much work remains.

### Limitations of hash-based dedup

- **File system boundary**: a file that's hashed differently by two
  tools (e.g., one strips UTF-8 BOM, one doesn't) will show as
  distinct. Hashing is over raw bytes.
- **Near-duplicates**: two PDFs that render identically but have
  different metadata timestamps in their binary header hash
  differently. This is content-level dedup, not "visual similarity".
- **Hash skipped for very large files**: by default, files over 500MB
  skip hashing (they also typically skip content extraction). Set
  `--large-files` to force hashing of larger files.
- **Empty files all cluster together**: the canonical empty-SHA-256
  matches every zero-byte file. This is intentional — it's a real
  "all my empty files" dedup group — but can be surprising the first
  time you see hundreds of them in one result set.

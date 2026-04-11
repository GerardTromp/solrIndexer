# Architecture

fsearch is three cooperating processes sitting on top of a shared Solr
core. Each process has one job and they communicate only through Solr
or through the filesystem.

## Component diagram

```
                      ┌───────────────────┐
                      │   sources.yaml    │
                      └─────────┬─────────┘
                                │ reads
                                ▼
┌─────────┐    pull hook   ┌──────────────┐   crawl    ┌──────┐
│ Source  ├───────────────▶│ fs_indexer.py├───────────▶│ Tika │
│ scripts │ writes files   │  per-source  │  content   │ (9998)│
└─────────┘                │     loop     │◀──────────┘
                           └──────┬───────┘
                                  │ batch add
                                  ▼
                           ┌──────────────┐
                           │    Solr      │  ──▶  fsearch CLI
                           │ filesystem   │  ──▶  fsearch_web.py
                           │    core      │       (Flask + static/)
                           └──────────────┘
```

## Runtime pieces

**Apache Solr 10** — the search index. One core, `filesystem`. Runs
as a local service on `http://localhost:8983`. Data lives on the
dedicated ext4 data disk at `/mnt/wd1/solr/data`.

**Apache Tika 3** — content extractor. Runs as a standalone HTTP
server on `http://localhost:9998`. The indexer uses the `/rmeta/text`
endpoint, which returns both extracted text AND detected metadata
(MIME type, language) in a single call.

**`fs_indexer.py`** — the crawler. Reads [`sources.yaml`](../technical/configuration.md)
to decide what to walk, runs each source's optional pre-index hook,
then walks the root and batches docs into Solr. One source at a time,
so log output is readable and Tika isn't thrashed.

**`fsearch_web.py`** — Flask app backing the web GUI. Translates
row-based queries from the UI into Solr Lucene queries via the
shared clause-builder logic. Serves [`static/search.html`](../technical/cli.md)
at `/`.

**`fsearch.py`** — CLI search tool. Same clause-builder as the web
side, but outputs a Rich table (or CSV/TXT/JSON via `--export`).

## Data flow for a single file

1. A source drops a file into its root directory (plain filesystem
   source = no drop, just stat existing files; pull source = hook
   script extracts content; push source = external tool writes
   files on its own schedule).
2. `fs_indexer.py` walks the source root, device-grouped for
   multi-disk parallelism.
3. For each file, the indexer checks the "unchanged" cache
   (size+mtime+hash-present match Solr's record). If unchanged,
   skip. Otherwise proceed.
4. Content extraction: `fsearch_hash.sha256_file()` computes a
   SHA-256 (single streaming pass, chunk size tuned to available
   memory). For text files, raw `open()`+`read()`. For binary
   files, Tika via `/rmeta/text`.
5. Tika returns `(content_text, metadata_dict)` where metadata
   includes `mimetype_detected` and `language`.
6. The indexer also looks up the file in the source's manifest
   (if one exists) for `source_timestamp` and `source_metadata`
   enrichment.
7. A fully-enriched doc is added to a batch and committed to Solr
   when the batch reaches 300.
8. The purge pass at the end scopes its delete cursor to the
   current source (via `source_name` field) so one source can't
   clobber another's docs.

## Why the source abstraction exists

Without it, `fs_indexer.py` would hardcode which roots to walk, and
every new data source (email, archives, other systems) would either
require editing the indexer OR invent a parallel importer that
bypasses the deduplication, hashing, and incremental logic.

The abstraction uses the simplest possible contract: **a source is a
directory**. The indexer walks it like any other filesystem root. Pull
sources implement a pre-index hook that fills the directory before the
walk; push sources run on their own schedule and the indexer just sees
files appear. A sidecar `.manifest.json` provides per-file enrichment
without requiring the indexer to understand source-specific formats.

See [Source abstraction](sources.md) for the contract in detail.

## Where state lives

| State | Location | Scope |
|---|---|---|
| Solr index | `/mnt/wd1/solr/data` | Durable, survives restarts |
| Indexer run state | `~/.solr/indexer_state.json` | Per-source `last_run` timestamps |
| Find cache | `/mnt/wd1/solr/find_cache_src-<name>_dev<N>.txt` | Per-source, per-device file list |
| Source lockfiles | `<source_root>/.lock` (configured per source) | Prevents hook re-entry |
| Permanent skip list | `~/.solr/skip_content.tsv` | Files Tika has given up on |
| Error log | `/mnt/wd1/solr/logs/index_errors.log` | Retryable failures |
| Per-source sync state | Source-specific (PST: `.extract_state.json`, Gmail: `.gmail_state.json`) | Owned by each source script |

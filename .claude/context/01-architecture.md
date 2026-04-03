# System Architecture

## High-Level Architecture

```
Filesystem (WSL2 + Windows mounts)
    │
    ▼
fs_indexer.py (incremental crawler)
    │  find cache, mtime diff, parallel threads
    ▼
Apache Tika 3.0 (content extraction)
    │  PDF, DOCX, XLSX, plain text, etc.
    ▼
Apache Solr 10.0 (SolrCloud, ZooKeeper embedded)
    │  schema: filepath, filename, ext, size, mtime, content, content_preview
    │
    ├──► fsearch.py      — CLI search (rich tables, Boolean queries)
    └──► fsearch_web.py  — Flask web GUI (query builder, highlights)
```

## Architectural Style

Pipeline architecture: batch crawler → content extraction → search index → query interfaces.

## Core Components

### 1. Indexer (`fs_indexer.py`)
- Incremental crawl using mtime comparison against Solr
- Parallel Tika extraction via ThreadPoolExecutor
- Batch Solr updates (300 docs/batch)
- Find cache for fast re-crawling
- Error triage: retryable vs permanent failures
- Graceful shutdown on SIGTERM/SIGINT

### 2. CLI Search (`fsearch.py`)
- Click/argparse-based CLI
- Boolean query building (AND, OR, NOT)
- Regex support in text/content/path fields
- Rich table output with highlights
- Size/date range filters

### 3. Web GUI (`fsearch_web.py`)
- Flask backend, single-page HTML frontend
- Dynamic query builder with drag-and-drop rows
- Solr highlighting (content + filename)
- Content preview toggle per result
- Keyboard shortcuts (Ctrl+Enter, ?)

### 4. Cron Wrapper (`run_index.sh`)
- Ensures Tika is running before indexing
- Manages Tika heap size (default 512m, configurable)
- Retry previously failed files
- Purge orphaned docs (deleted files)
- Daily at 2am via crontab

### 5. Error Triage (`triage_errors.py`)
- Re-probes failed files against Tika
- Classifies as retryable (transient) or permanent (corrupt/encrypted)
- Permanent skip list prevents repeated failures

## Technology Stack

- **Runtime**: Python 3, Bash, Java 21
- **Search**: Apache Solr 10.0 (SolrCloud mode)
- **Content extraction**: Apache Tika 3.0
- **Web**: Flask, vanilla JS
- **CLI output**: Rich library
- **Platform**: WSL2 Ubuntu on Windows 11

## Data Flow

```
find/cache → filter (size, ext, mtime) → read file
    → Tika extraction (HTTP PUT) → extract text + metadata
    → build Solr doc (filepath, filename, ext, size, mtime, content, content_preview)
    → batch POST to Solr (300/batch, commit every batch)
```

---
*Last Updated: 2026-04-03*

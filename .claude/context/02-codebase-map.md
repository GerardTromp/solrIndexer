# Codebase Map

## Directory Structure

```
solrIndexer/
├── CLAUDE.md                 # Project brief (this repo)
├── VERSION                   # Semantic version (0.0.4)
├── IndexerCode.md            # Original design doc / chat transcript
├── fs_indexer.py             # Main indexer (1200+ lines)
├── fsearch.py                # CLI search tool (330 lines)
├── fsearch_web.py            # Flask web GUI (250 lines)
├── triage_errors.py          # Error classification (180 lines)
├── run_index.sh              # Cron wrapper (165 lines)
├── install.sh                # One-shot setup script
├── static/
│   └── search.html           # Web UI (single page, 695 lines)
├── setup/
│   ├── install.sh            # Duplicate of root install.sh
│   └── setup_schema.sh       # Solr schema API calls
├── docs/
│   └── development_chat_*.md # Development session transcripts
└── .claude/                  # Checkpoint system
    ├── checkpoints/
    ├── context/
    ├── sessions/
    └── memory-bank/
```

## Entry Points

- **Indexer**: `python3 fs_indexer.py /path/to/index [--full|--retry-errors]`
- **CLI search**: `fsearch "query"` (symlinked from `/usr/local/bin/fsearch`)
- **Web GUI**: `python3 fsearch_web.py --host 0.0.0.0 [--port 8080]`
- **Cron**: `run_index.sh` (daily at 2am)

## Module Dependencies

```
run_index.sh
    └── fs_indexer.py
        ├── pysolr (Solr client)
        ├── requests (Tika HTTP)
        ├── click (CLI)
        └── rich (output)

fsearch.py
    ├── pysolr
    └── rich

fsearch_web.py
    ├── flask
    ├── pysolr
    └── static/search.html (served via send_from_directory)
```

## Critical Paths

### Indexing Flow
`run_index.sh` → `fs_indexer.py` → Tika → Solr

### Search Flow (CLI)
User → `fsearch.py` → Solr → Rich table output

### Search Flow (Web)
Browser → `fsearch_web.py /api/search` → Solr → JSON → `search.html` render

## Deployment Location

Scripts deployed to `/opt/fsearch/` in WSL via `install.sh`:
- `fs_indexer.py`, `fsearch.py`, `fsearch_web.py`, `run_index.sh`
- `static/search.html`
- Symlink: `/usr/local/bin/fsearch` → `/opt/fsearch/fsearch.py`

---
*Last Updated: 2026-04-03*

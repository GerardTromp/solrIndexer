# fsearch — Solr Filesystem Search (v0.0.4)

> **Checkpoint system**: See `.claude/context/` for detailed project knowledge.

## Project Summary

Solr-backed filesystem search system running on WSL2 Ubuntu. Indexes local
filesystems via Apache Tika for content extraction, provides CLI search
(`fsearch`) and a web GUI (`fsearch_web.py`) with Boolean query building.
Currently indexing 623K+ documents.

## Environment

- **Runtime**: WSL2 Ubuntu, Python 3, Java 21
- **Solr 10** at `http://localhost:8983/solr/filesystem`
- **Tika 3** at `http://localhost:9998/tika`
- **Data disk**: `/mnt/wd1` (ext4, Solr data + logs)
- **Scripts**: deployed to `/opt/fsearch/` in WSL
- **Cron**: daily 2am indexing via `run_index.sh`

## Architecture

```
Filesystem ──► fs_indexer.py ──► Tika (content) ──► Solr
                                                      │
                                            ┌─────────┤
                                            ▼         ▼
                                      fsearch.py   fsearch_web.py
                                       (CLI)        (Flask GUI)
```

## Key Files

| File | Purpose |
|---|---|
| `fs_indexer.py` | Incremental crawler + Solr indexer (parallel, Tika) |
| `fsearch.py` | CLI search tool (rich output) |
| `fsearch_web.py` | Flask web GUI with query builder |
| `static/search.html` | Single-page search UI (dark theme) |
| `run_index.sh` | Cron wrapper (Tika lifecycle, retry, purge) |
| `triage_errors.py` | Classify Tika failures as retryable/permanent |
| `install.sh` | One-shot WSL setup (Solr, Tika, schema, cron) |

## Primary Principles

- **Code must be separate from data**
  - No real datasets or personal paths in source (only in config/env vars)
  - Data paths are always parameters or environment variables

- **No personal or server-specific information in committed code**

## Checkpoint System

This project uses a structured checkpoint system for context preservation.

### Location
All checkpoint and context files are in `.claude/`

### When to Create Checkpoints
- **Incremental**: End of each session (>30 min work)
- **Full**: End of week, after major milestones, before breaks

### Recovery from Interruption
1. Read `.claude/context/08-progress.md` for latest state
2. Read most recent checkpoint in `.claude/checkpoints/`
3. Check git status for uncommitted work

### Checkpoint Files
- **checkpoints/**: Full and incremental snapshots
- **context/**: Persistent project knowledge (01-08)
- **sessions/**: Individual session logs
- **memory-bank/**: Lessons learned

See `.claude/README.md` for directory layout.

# Design Patterns & Conventions

## Architectural Patterns

### Pipeline Pattern
Indexing follows a strict pipeline: discover → filter → extract → transform → load.
Each stage is independent and can be retried.

### Batch Processing
Solr updates are batched (300 docs/batch) to balance throughput and memory.
`pysolr.Solr.add()` with `commit=True` per batch.

### Graceful Shutdown
`fs_indexer.py` traps SIGTERM/SIGINT, sets a flag, and drains the current
batch before exiting. Ensures partial progress is committed.

### Error Segregation
Errors are triaged into retryable (transient Tika failures, timeouts) and
permanent (corrupt files, encrypted PDFs). Permanent failures are added to
a skip list to avoid repeated processing.

## Code Conventions

- **Python style**: PEP 8, minimal classes, script-oriented
- **Imports**: stdlib first, then third-party, grouped
- **CLI**: Click for indexer, argparse for fsearch
- **Output**: Rich library for terminal formatting
- **Logging**: Python `logging` + Rich handler + file handler
- **Config**: Environment variables with sensible defaults
- **Path handling**: `pathlib.Path` throughout

## Testing Patterns

- No formal test suite yet
- Manual testing via CLI and web GUI
- Error triage script serves as a diagnostic tool

## Web Frontend Patterns

- Single HTML file with embedded CSS and JS (no build step)
- Dark theme (Tokyo Night color scheme)
- Vanilla JS, no framework
- Drag-and-drop query rows
- Keyboard shortcuts for power users

---
*Last Updated: 2026-04-03*

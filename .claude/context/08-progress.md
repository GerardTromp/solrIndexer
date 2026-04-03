# Current Implementation Progress

**Last Updated**: 2026-04-03
**Current Version**: 0.0.4

## Status Summary

Core system is functional and in daily use. 623K+ documents indexed.
Daily incremental indexing runs via cron at 2am.

## Recently Completed

- **2026-04-02**: Deployed web server to `/opt/fsearch/` in WSL, updated `install.sh` to include web files
- **2026-04-02**: Fixed `fsearch_web.py` static file path resolution (`HERE = Path(__file__).resolve().parent`)
- **2026-03-31**: v0.0.4 — Skip Office lock files (`~$*`), fix permanent failure detection
- **2026-03-30**: v0.0.3 — Permanent skip list, error triage, corrupt files log
- **2026-03-30**: v0.0.2 — Fix CLI, cron PATH, mount check, increase purge batch
- **2026-03-30**: v0.0.1 — Tika resilience, fast purge, shutdown fix, log suppression
- **2026-03-25**: v0.0.0 — Initial commit: indexer, CLI, web GUI, install script

## Active Development

None currently — system is stable and in maintenance mode.

## Backlog

### High Priority
- [ ] Investigate WSL→Windows port forwarding reliability (wslrelay)

### Medium Priority
- [ ] Add unit tests for query building (fsearch.py, fsearch_web.py)
- [ ] Systemd service files for Solr/Tika (instead of bashrc auto-start)
- [ ] Incremental indexing for file renames/moves (detect by content hash)

### Low Priority / Nice to Have
- [ ] Real-time file watching (inotifywait) for near-instant indexing
- [ ] Search result pagination in web GUI
- [ ] Export search results to CSV
- [ ] Authentication for web GUI if exposed beyond localhost

## Technical Debt

| Item | Impact | Effort | Priority |
|---|---|---|---|
| No test suite | Risk of regressions | Medium | Medium |
| Duplicate install.sh (root + setup/) | Confusion | Small | Low |
| IndexerCode.md is a 50K-token chat dump | Noise in repo | Small | Low |

---
*Last Updated: 2026-04-03*

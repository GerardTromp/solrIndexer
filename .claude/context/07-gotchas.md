# Known Issues, Gotchas & Workarounds

## Critical Gotchas (Must Know)

### WSL port forwarding breaks after Windows Update
- **Symptom**: `wslrelay.exe` listens on `[::1]:8983` but returns empty replies
- **Cause**: Windows Update resets WSL networking state
- **Workaround**: Run the Flask web server inside WSL where it can reach Solr on localhost directly. Don't rely on wslrelay for Solr access from Windows.
- **When it matters**: After any Windows reboot/update

### Flask 404 when launched from wrong directory
- **Symptom**: `GET /` returns 404
- **Cause**: `os.path.dirname(__file__)` returns empty string when __file__ is relative
- **Workaround**: Fixed by using `Path(__file__).resolve().parent` (the `HERE` variable). Always launch from project dir or use absolute path.
- **When it matters**: Running `fsearch_web.py` from a different working directory

### Office lock files (~$*.docx)
- **Symptom**: Tika extraction fails on Office temp/lock files
- **Cause**: Lock files are partial binary stubs, not real documents
- **Workaround**: v0.0.4 skips files matching `~$*` pattern
- **Status**: Fixed

## Environment-Specific Issues

### WSL2
- Solr binds to IPv6 `[::1]` by default; Windows apps expecting IPv4 `127.0.0.1` cannot connect
- `/mnt/d` access is slower than native ext4 on `/mnt/wd1`
- Solr data should live on ext4 (`/mnt/wd1`) not NTFS for performance

### Tika
- Default 512MB heap insufficient for some large PDFs
- Use `run_index.sh --tika-heap 4g` for temporary boost
- Tika server can become unresponsive under heavy load; indexer has timeout + retry logic

## Common Pitfalls

1. **Running indexer while Tika is down**: Indexer will log errors for all content extraction; use `run_index.sh` which ensures Tika is up
2. **Concurrent indexer runs**: Lock file prevents this, but check if stale lock exists at `/mnt/wd1/solr/indexer.lock`
3. **Deploying to /opt/fsearch**: Remember to copy `static/search.html` too, not just `.py` files

---
*Last Updated: 2026-04-03*

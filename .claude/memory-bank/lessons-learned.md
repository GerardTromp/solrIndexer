# Lessons Learned

## WSL Networking
- **Lesson**: WSL port forwarding (wslrelay) is unreliable after Windows updates. Run services that need Solr inside WSL rather than relying on the relay from Windows.
- **Date**: 2026-04-02

## Static File Serving
- **Lesson**: Always use `Path(__file__).resolve().parent` for locating static assets relative to a script. `os.path.dirname(__file__)` fails when __file__ is a relative path and the CWD differs from the script location.
- **Date**: 2026-04-02

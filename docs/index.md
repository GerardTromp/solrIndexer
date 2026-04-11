# fsearch

Solr-backed filesystem search for WSL2 Ubuntu. Indexes local filesystems,
emails (PST archives, Gmail, Outlook desktop), and any other content a
user-provided "source" can drop into a directory, then exposes it through
a CLI (`fsearch`) and a web GUI (`fsearch_web.py`).

!!! note "Project principle"
    **Code is separate from data.** No real datasets or personal paths live
    in source. Every data path is a parameter, config entry, or environment
    variable.

## What it does

- Walks configured source directories, hands each file to
  [Apache Tika](https://tika.apache.org/) for content extraction,
  computes a SHA-256 over file bytes, and indexes the result in
  [Apache Solr](https://solr.apache.org/).
- Supports Boolean query building via a web GUI with a row-based
  filter editor, query export to CSV/TXT/JSON, and row actions
  (open in VS Code, copy path, find duplicates).
- Enriches each Solr doc with detected language, Tika-detected MIME,
  content hash, and source tagging (`source_name`, `source_kind`,
  `source_timestamp`, `source_metadata`).
- Pluggable source abstraction: add a new source by writing a small
  script that deposits files in a directory and an optional
  `.manifest.json` — no fsearch code changes needed.

## Where to start

=== "Just installing"
    Read [First install](vignettes/first-install.md) for the one-shot
    WSL setup.

=== "Adding a source"
    Pick the vignette for the source kind you're wiring up:

    - [PST archives](vignettes/pst-wiring.md) — local Outlook PST files
    - [Gmail](vignettes/gmail-wiring.md) — via OAuth2 refresh token
    - [Outlook desktop (COM)](vignettes/outlook-wiring.md) — Windows-side COM automation

=== "Understanding the system"
    [Architecture](overview/architecture.md) for the high-level picture,
    [Source abstraction](overview/sources.md) for how pluggable sources
    work, and [Solr schema](technical/schema.md) for what's actually
    stored per document.

=== "Daily use"
    [CLI tools](technical/cli.md) and
    [Export & duplicate detection](vignettes/export-and-dedup.md).

## Build these docs locally

```bash
pip install mkdocs
cd /path/to/solrIndexer
mkdocs serve      # live-reload on http://127.0.0.1:8000
mkdocs build      # static site in ./site/
```

The theme is the built-in `readthedocs` one — no extra packages needed.

## Project layout

| Path | Purpose |
|---|---|
| `fs_indexer.py` | Incremental crawler + Solr indexer, per-source loop |
| `fs_sources.py` | Source config loader, hook runner, manifest reader |
| `fsearch_hash.py` | SHA-256 content hasher |
| `fsearch.py` | CLI search tool |
| `fsearch_web.py` | Flask web GUI backend |
| `static/search.html` | Single-page search UI |
| `sources/pst/extract.py` | PST pull source (readpst wrapper) |
| `sources/gmail/sync.py` | Gmail pull source (OAuth2) |
| `sources.yaml.example` | Example sources config |
| `run_index.sh` | Cron wrapper |
| `install.sh` | One-shot WSL setup |
| `deploy.sh` | Sync edits to `/opt/fsearch/` |

# Configuration

fsearch reads configuration from three places, in order of precedence:

1. **CLI flags** (highest — override everything)
2. **`sources.yaml`** config file
3. **Environment variables** (fallback)

## `sources.yaml`

Default location: `/opt/fsearch/sources.yaml`. Override with
`--sources PATH` or `FSEARCH_SOURCES` env var.

### Minimal example

```yaml
sources:
  - name: filesystem
    kind: fs
    root: /home/gerard

  - name: wd1-gt
    kind: fs
    root: /mnt/wd1/GT
    excludes:
      - node_modules
      - .git
      - .venv
```

### Full-featured example

```yaml
sources:
  - name: filesystem
    kind: fs
    root: /home/gerard
    excludes:
      - node_modules
      - .git

  # Pull source with hook (PST archives)
  - name: pst-archive
    kind: pst
    root: /mnt/wd1/sources/pst
    hook:
      command: /opt/fsearch/sources/pst/extract.py
      timeout: 3600
      lockfile: /mnt/wd1/sources/pst/.lock
      on_failure: skip

  # Pull source with hook (Gmail)
  - name: gmail
    kind: imap
    root: /mnt/wd1/sources/gmail
    hook:
      command: /opt/fsearch/sources/gmail/sync.py
      timeout: 1800
      lockfile: /mnt/wd1/sources/gmail/.lock
      on_failure: skip

  # Push source — no hook, external tool writes here
  - name: outlook-work
    kind: msg
    root: /mnt/c/Users/gerard/OutlookExport
```

### Field reference

| Field | Type | Required | Purpose |
|---|---|:---:|---|
| `name` | string | ✓ | Unique identifier; tagged in Solr as `source_name` |
| `kind` | string |   | Coarse type (default: `fs`); tagged as `source_kind` |
| `root` | path | ✓ | Directory to walk |
| `roots` | list of paths |   | Alternative for multi-root bundles (legacy) |
| `excludes` | list |   | Path substrings to skip during walk |
| `hook` | mapping |   | Optional pre-index hook (see below) |

### Hook fields

| Field | Type | Required | Default | Purpose |
|---|---|:---:|---|---|
| `command` | string | ✓ | — | Shell command run before walk |
| `timeout` | int |   | `3600` | Wall-clock timeout (seconds) |
| `lockfile` | path |   | none | PID-based lock to prevent overlap |
| `on_failure` | string |   | `skip` | `skip` / `abort` / `continue-stale` |

### `on_failure` modes

**`skip`** — log the error, move on to the next source. Best for
nightly cron jobs where one flaky source shouldn't block the rest.

**`abort`** — log and exit the whole indexer with nonzero status.
Use when the source is load-bearing (e.g., a legal-archival source
whose absence would mean incomplete search results for a reason that
matters).

**`continue-stale`** — log the failure, walk the root anyway using
whatever data is already there from the previous successful run. Best
for push sources where an occasional sync failure upstream shouldn't
prevent searching older data.

---

## Environment variables

### Runtime locations

| Variable | Default | Purpose |
|---|---|---|
| `SOLR_URL` | `http://localhost:8983/solr/filesystem` | Solr endpoint |
| `TIKA_URL` | `http://localhost:9998/tika` | Tika endpoint (legacy) |
| `TIKA_RMETA_URL` | derived from `TIKA_URL` | Tika `/rmeta/text` endpoint |
| `FSEARCH_SOURCES` | `/opt/fsearch/sources.yaml` | Config file path |
| `FSEARCH_LOCK` | `/mnt/wd1/solr/indexer.lock` | Global indexer lockfile |
| `FSEARCH_FIND_CACHE` | `/mnt/wd1/solr/find_cache.txt` | Base find cache path |
| `FSEARCH_FIND_CACHE_MAX_HOURS` | `12` | Cache expiry |
| `INDEX_ROOTS` | (unset) | Whitespace-separated roots (fallback when sources.yaml is absent) |

### PST source

| Variable | Default | Purpose |
|---|---|---|
| `FSEARCH_PST_INPUT_DIR` | (required) | Directory to scan for `*.pst` files |
| `FSEARCH_PST_OUTPUT` | (required) | Output root (same as source `root` in sources.yaml) |
| `FSEARCH_PST_STATE` | `<output>/.extract_state.json` | Incremental state |
| `FSEARCH_PST_READPST` | `/usr/bin/readpst` | `readpst` binary |
| `FSEARCH_PST_LOG_LEVEL` | `INFO` | Log verbosity |

### Gmail source

| Variable | Default | Purpose |
|---|---|---|
| `FSEARCH_GMAIL_OUTPUT` | (required) | Output root |
| `FSEARCH_GMAIL_CREDENTIALS` | `~/.config/fsearch/gmail_credentials.json` | OAuth client JSON from GCP |
| `FSEARCH_GMAIL_TOKEN` | `~/.config/fsearch/gmail_token.json` | Cached refresh token |
| `FSEARCH_GMAIL_STATE` | `<output>/.gmail_state.json` | History cursor state |
| `FSEARCH_GMAIL_LOG_LEVEL` | `INFO` | Log verbosity |

---

## Setting env vars for cron runs

The cleanest place to set source env vars is inside `run_index.sh`,
the cron wrapper. Example:

```bash
#!/bin/bash
# /opt/fsearch/run_index.sh — cron wrapper (excerpt)

export PATH=/usr/local/bin:/usr/bin:/bin
export SOLR_URL="http://localhost:8983/solr/filesystem"

# PST source configuration
export FSEARCH_PST_INPUT_DIR="/mnt/c/Users/gerard/Documents/Outlook Files"
export FSEARCH_PST_OUTPUT="/mnt/wd1/sources/pst"

# Gmail source configuration
export FSEARCH_GMAIL_OUTPUT="/mnt/wd1/sources/gmail"

# ... existing Tika/Solr start-up checks ...

python3 /opt/fsearch/fs_indexer.py
```

That way env vars are hermetic to the indexer run and cron doesn't
need to know anything source-specific.

## Configuration gotchas

**The source `root` path must equal `FSEARCH_*_OUTPUT`.**  
The source declaration points at the directory that will contain the
files to index; the pull hook's env var tells the hook where to write
them. They must point at the same directory, otherwise the hook
writes files somewhere the indexer never looks.

**`.manifest.json` sidecar is never indexed as a doc.**  
`file_to_doc()` skips files named exactly `.manifest.json` the same
way it skips `~$*` Office lock files. The manifest reader consumes
them for enrichment, but they don't become searchable results.

**Paths inside a manifest are RELATIVE to the source root.**  
This is so the manifest survives remounts (e.g., `/mnt/wd1` → `/mnt/data`).
Manifests that use absolute paths won't match and will silently apply no
enrichment.

**Per-source lockfiles are advisory, not mandatory.**  
A hook without a `lockfile` will happily run concurrently with itself
if you somehow trigger two indexer runs at the same time. The
indexer's own global lockfile (`FSEARCH_LOCK`) normally prevents
this, but if you're running sources manually, declare a `lockfile`
in their hook config.

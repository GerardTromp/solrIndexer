# Source abstraction

A **source** is a named directory that fsearch walks and tags with a
kind. Sources are defined in
[`sources.yaml`](../technical/configuration.md) and processed
sequentially by `fs_indexer.py`, each in its own child process.

## The contract

At its minimum, a source is just:

```yaml
- name: unique-name
  kind: fs
  root: /path/to/directory
```

The indexer walks `root`, tags every doc with
`source_name=unique-name` and `source_kind=fs`, and commits them to
Solr. That's the whole contract for a **plain filesystem source**.

## Pull sources (hook + root)

A pull source extends the contract with a pre-index **hook**: a shell
command the indexer runs before walking the root. The hook is
responsible for filling the root directory with files to index.

```yaml
- name: pst-archive
  kind: pst
  root: /mnt/wd1/sources/pst
  hook:
    command: /opt/fsearch/sources/pst/extract.py
    timeout: 3600              # seconds
    lockfile: /mnt/wd1/sources/pst/.lock
    on_failure: skip           # skip | abort | continue-stale
```

Semantics:

- The hook runs once per indexer invocation, before the walk
- A per-source PID lockfile prevents overlapping hook runs (e.g.,
  a 2-hour extraction still running when the next cron ticks)
- Stale locks (dead PID) are taken over automatically
- Timeout is wall-clock; hook killed on exceed
- Hook stdout is captured and logged at debug; stderr at error
- `on_failure` modes:
    - **`skip`** — log the error, move to the next source
    - **`abort`** — stop the entire indexer run with nonzero exit
    - **`continue-stale`** — walk the root anyway using whatever
      the previous successful run left behind

## Push sources (root only, no hook)

A push source is structurally identical to a plain `fs` source but
lives outside the indexer's control. Some external process (a
Windows-side Task Scheduler job, a cron on a different machine, a
user manually dropping files) writes files into the root on its own
schedule. fsearch just sees them on the next walk.

```yaml
- name: outlook-work
  kind: msg
  root: /mnt/c/Users/gerard/OutlookExport
  # No hook — files arrive here via the Windows-side exporter
```

This is how the
[Outlook COM exporter](../vignettes/outlook-wiring.md) wires in
without requiring any Windows-specific code in the indexer itself.

## Manifest enrichment

Any source can drop a `.manifest.json` file at its root to attach
per-file metadata to Solr docs. The format:

```json
{
  "version": 1,
  "source_name": "gmail",
  "generated_at": "2026-04-11T08:30:00Z",
  "entries": {
    "2024/03/2024-03-15_abcd1234.eml": {
      "source_timestamp": "2024-03-15T09:14:22Z",
      "metadata": {
        "from": "alice@example.com",
        "to": "bob@example.com",
        "subject": "Re: project",
        "message_id": "<...>",
        "labels": ["INBOX", "IMPORTANT"]
      }
    }
  }
}
```

Keys in `entries` are **paths relative to the source root** — so
manifests survive filesystem remounts. The indexer loads the manifest
once at the start of each source's walk and looks up every file by
its relative path.

What gets applied to the Solr doc:

- `source_timestamp` — stored as a Solr `pdate` (Phase 1 schema). Lets
  you sort by email send-date independently of filesystem mtime.
- `metadata` — serialized to a JSON string and stored in the
  `source_metadata` field (stored, NOT indexed). fsearch retrieves
  it for display but doesn't search inside it.

A broken manifest never prevents indexing: missing file → no
enrichment; malformed JSON → warning logged, empty manifest used.
Docs lacking a manifest entry are indexed normally with filesystem
metadata only.

## Back-compat

Three precedence levels when resolving what to index:

1. **Positional CLI roots** override everything —
   `fs_indexer.py /a /b` wraps both into one `legacy-fs` source
   with multi-root (matches pre-Phase-2 semantics: one indexer run
   across multiple devices in parallel).
2. **`sources.yaml`** is read from
   `/opt/fsearch/sources.yaml` by default, overridable with
   `--sources PATH`. `--source NAME` runs only the named entry.
3. **`INDEX_ROOTS` env var** (whitespace-separated) as a fallback
   if `sources.yaml` is absent or empty.

## Per-source state isolation

Each source gets its own:

- **`last_run` timestamp** under `state["sources"][name]["last_run"]`
  in `~/.solr/indexer_state.json`, so incremental mode doesn't
  cross-pollute.
- **Find cache file** named `find_cache_src-<name>_dev<N>.txt`, so
  two sources on the same filesystem don't clobber each other's
  file list.
- **Purge scope**: the cursor scan that finds deleted files is
  scoped to `source_name:<name>`, so source A's purge can't delete
  source B's docs because they happen to be outside A's root.

# Wiring up PST archives

A walkthrough for getting archived Outlook `.pst` files into
fsearch. This assumes you've finished [First install](first-install.md)
and have at least one plain filesystem source working.

## What you'll end up with

- A nightly job that extracts each PST file in a Windows archive
  directory into per-message `.eml` files
- A `.manifest.json` sidecar recording the sender, subject, and
  sent-date of each message
- Every email showing up in fsearch tagged
  `source_name=pst-archive`, sortable by `source_timestamp` (the
  real sent-date, not the filesystem mtime of the extracted .eml)
- Searches like `fsearch -q 'source_kind:pst AND content:"BRCA1"'`
  that only look at PST-archived messages

## Prerequisites

- `pst-utils` installed (`apt install pst-utils`). `install.sh` does
  this automatically; verify with `which readpst`.
- A directory containing one or more `.pst` files, readable from WSL.
  For a typical Windows Outlook install that's something like
  `/mnt/c/Users/<you>/Documents/Outlook Files/`.
- Write access to the intended output directory, typically on the
  data disk (`/mnt/wd1/sources/pst`).

## Step 1 — Create the output directory

```bash
sudo mkdir -p /mnt/wd1/sources/pst
sudo chown "$USER:$USER" /mnt/wd1/sources/pst
```

The source root IS the output root. The indexer walks this directory,
and the extraction hook writes into it.

## Step 2 — Test the extractor manually

Before wiring anything into cron, run the extractor by hand to make
sure it works on your PST files:

```bash
FSEARCH_PST_INPUT_DIR="/mnt/c/Users/gerard/Documents/Outlook Files" \
FSEARCH_PST_OUTPUT=/mnt/wd1/sources/pst \
/opt/fsearch/sources/pst/extract.py
```

You should see something like:

```
14:20:11 INFO Found 3 PST(s) under /mnt/c/Users/gerard/Documents/Outlook Files
14:20:11 INFO Extracting archive_2024.pst -> /mnt/wd1/sources/pst/archive_2024
14:22:45 INFO Extracted archive_2024.pst: 8421 messages in 154.3s
14:22:45 INFO Extracting archive_2023.pst -> /mnt/wd1/sources/pst/archive_2023
...
14:35:10 INFO Wrote manifest: /mnt/wd1/sources/pst/.manifest.json (24103 entries)
14:35:10 INFO Done: extracted=3 unchanged=0 failed=0 manifest_entries=24103
```

If any PST fails, check the stderr output — `readpst` usually gives
a clear reason (encrypted PST, corrupt file, locked OST file).

## Step 3 — Verify the output

```bash
ls /mnt/wd1/sources/pst/
# archive_2023  archive_2024  archive_2022  .manifest.json  .extract_state.json

head /mnt/wd1/sources/pst/.manifest.json
# {
#   "version": 1,
#   "source_name": "pst-archive",
#   "generated_at": "2026-04-11T18:35:10Z",
#   "entries": {
#     "archive_2024/Inbox/abc123_001.eml": {
#       ...

find /mnt/wd1/sources/pst -name "*.eml" | head -5
```

## Step 4 — Add the source to `sources.yaml`

Edit `/opt/fsearch/sources.yaml` and add:

```yaml
sources:
  - name: pst-archive
    kind: pst
    root: /mnt/wd1/sources/pst
    hook:
      command: /opt/fsearch/sources/pst/extract.py
      timeout: 7200
      lockfile: /mnt/wd1/sources/pst/.lock
      on_failure: skip
```

Verify it parses:

```bash
fs_indexer.py --list-sources
# NAME                 KIND       HOOK  ROOTS
# filesystem           fs         no    /home/gerard
# pst-archive          pst        yes   /mnt/wd1/sources/pst
```

## Step 5 — Set env vars in the cron wrapper

The hook command reads `FSEARCH_PST_INPUT_DIR` and
`FSEARCH_PST_OUTPUT` from its environment, so they need to be set
when the indexer runs the hook. The cleanest place is
`/opt/fsearch/run_index.sh`:

```bash
sudo vi /opt/fsearch/run_index.sh
```

Add near the top (after `export PATH=...`):

```bash
export FSEARCH_PST_INPUT_DIR="/mnt/c/Users/gerard/Documents/Outlook Files"
export FSEARCH_PST_OUTPUT="/mnt/wd1/sources/pst"
```

## Step 6 — First live run through the indexer

Run only the PST source to verify the full wiring end-to-end:

```bash
FSEARCH_PST_INPUT_DIR="/mnt/c/Users/gerard/Documents/Outlook Files" \
FSEARCH_PST_OUTPUT=/mnt/wd1/sources/pst \
fs_indexer.py --source pst-archive
```

Expected flow in the log:

1. `=== Source: pst-archive (pst) → /mnt/wd1/sources/pst ===`
2. `Source 'pst-archive': running hook (timeout 7200s)` — the
   extraction runs (or skips if nothing changed)
3. `Loaded manifest from .../pst-archive/.manifest.json (24103 entries)`
4. Find cache scan + file-by-file indexing begins
5. Each `.eml` gets Tika-extracted and committed with
   `source_name=pst-archive` and `source_timestamp=<sent-date>`

## Step 7 — Verify in Solr

```bash
# Count PST-sourced docs
curl -s 'http://localhost:8983/solr/filesystem/select?q=source_name:pst-archive&rows=0' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'])"

# Spot-check one doc
fsearch -q 'source_kind:pst' -l 1 --json | python3 -m json.tool
```

You should see `source_timestamp`, `source_metadata` (JSON blob with
sender/subject/message_id), and all the usual fields.

## Searching PST content

Once indexed, PST messages are just documents. Some useful patterns:

```bash
# Find PST emails mentioning a project name
fsearch -q 'source_kind:pst AND content:ProjectAlpha'

# Find PST emails from a specific sender
# (source_metadata is a JSON string — substring match)
fsearch -q 'source_kind:pst AND source_metadata:"alice@example.com"'

# Sort by real sent-date
fsearch -q 'source_kind:pst' --sort 'source_timestamp desc' -l 20

# Find duplicates between fs and PST sources (same message attached
# somewhere AND in an email)
# Click the "find duplicates" button in the web GUI on any result row
```

## Ongoing maintenance

- **Incremental extraction** is automatic. The extractor tracks each
  PST by `(size, mtime)` in `.extract_state.json`. Unchanged PSTs
  are skipped; modified PSTs are wiped and re-extracted.
- **Adding a new PST** is a file copy — drop it in the input dir,
  the next run picks it up.
- **Removing a PST** from the input dir does NOT delete its
  extracted messages from the output. Clean that up manually if
  you want:

  ```bash
  rm -rf /mnt/wd1/sources/pst/old_archive_name
  # Next indexer run purges the corresponding Solr docs via the
  # per-source purge pass
  ```

## Troubleshooting

**"readpst not found at /usr/bin/readpst"**  
`pst-utils` isn't installed. `sudo apt install pst-utils`.

**"No .pst files found"**  
Check the input dir spelling (Windows paths under `/mnt/c/...` are
case-sensitive from WSL). Verify readability: `ls -la
"$FSEARCH_PST_INPUT_DIR"`.

**Extraction takes forever**  
Normal on the first run — Outlook PSTs are often several GB each.
Use `--timeout 14400` in the hook config if the default 1-hour
budget is too tight. Subsequent runs are fast because unchanged
PSTs are skipped entirely.

**Messages are indexed but have no `source_timestamp`**  
The `.eml` is missing a parseable `Date` header, which happens for
messages originally sent by broken MUAs. The extractor logs these
at debug level; the docs still index, just without the date
enrichment.

**PST hook runs but no files appear in Solr**  
Check that the hook is actually writing to the same directory the
source's `root:` points at. The most common failure mode is a typo
in either the sources.yaml or the env var pointing them at
different places.

# PST source — design notes

**Created**: 2026-04-11 (Phase 4)
**Status**: implemented as a `readpst`-based wrapper

This document records the choices behind the PST extractor and the
options that were considered but not taken. It's here so a future visit
to this code (or a fresh start on a different system) doesn't have to
re-discover the tradeoffs.

## Library choice

PSTs (and OSTs) are Microsoft's compound document format for Outlook
mailboxes. Three viable readers exist on Linux/WSL:

### Option A — pypff (`libpff-python`) [REJECTED]

Native Python bindings to libpff. Most flexible: per-message access in
Python with no subprocess overhead, easy to plug into the manifest
generator without an intermediate filesystem layout.

**Why rejected**:
- Historically painful install. The PyPI wheel is unmaintained for
  recent CPython versions; users typically need to build from source
  against `libpff-dev`, which is not in Ubuntu universe by default.
- One bad system upgrade and the C extension breaks silently.
- Adds a hard Python-side dependency for what is fundamentally a
  one-shot extraction job.

When to revisit: if we ever need streaming access to large PSTs that
won't fit in `/tmp` once extracted, or if we want to incrementally read
just the new messages from a PST without re-extracting the whole thing.
pypff is the only path that gives us message-level random access.

### Option B — libratom [REJECTED]

A higher-level Python wrapper that uses pypff under the hood and adds
spaCy-based NLP/entity extraction. Built by NARA's RATOM project for
email archival research.

**Why rejected**:
- Bundles ~20 transitive deps (spaCy, ML models, scikit-learn) that
  fsearch will never use.
- Inherits pypff's install fragility.
- Designed for forensic mailbox analysis, not for "hand me a folder of
  .eml files". Wrong shape.

When to revisit: if fsearch ever wants entity extraction or message
classification at index time. That'd be a very different project.

### Option C — `readpst` (libpst CLI) [CHOSEN]

A C tool from Ubuntu's `pst-utils` package. Reads PST/OST files and
emits per-message files in either `.eml` (RFC822) or `.msg` format,
preserving the original folder hierarchy.

**Why chosen**:
- Single `apt install pst-utils` and it's there forever. No Python
  binding fragility, no compiler dance.
- Output is exactly the shape fsearch wants: a tree of `.eml` files
  that the existing crawler + Tika pipeline already handle natively.
  No new parsers, no new MIME handlers.
- Separation of concerns: the messy MAPI traversal is hidden inside a
  battle-tested C tool. Our Python wrapper is just glue.
- Easy to test without writing PSTs by hand: pipe a real PST through
  it, inspect the resulting .eml tree.

**Tradeoffs accepted**:
- Subprocess overhead per PST. For nightly runs over a fixed archive
  this is negligible (PSTs are extracted once and re-extracted only
  when their mtime/size changes — see incremental strategy below).
- Disk doubling: extracted .eml files take roughly the same space as
  the source PST. We deliberately put output on `/mnt/wd1` (the data
  disk), not the system drive, so this is fine.
- No streaming: a 5GB PST gets fully extracted before indexing
  begins. Acceptable for archival PSTs which don't change often.

When to revisit: if a single PST is large enough that extraction time
dominates the cron window, or if disk doubling becomes painful. At that
point, switch to pypff and stream messages directly into the manifest
without an intermediate filesystem layout.

## Input discovery: how the extractor finds PSTs

Three patterns considered:

### Pattern 1 — hardcoded list of paths [REJECTED]

```python
PST_FILES = [
    "/mnt/c/Users/.../archive1.pst",
    "/mnt/c/Users/.../archive2.pst",
]
```

Simple and explicit, but violates the "code must be separate from data"
project principle. Every new PST = code edit + redeploy.

### Pattern 2 — colon-separated env var [REJECTED]

```bash
export FSEARCH_PST_INPUTS="/path/a.pst:/path/b.pst"
```

Better than hardcoded but still a maintenance burden. Adding a PST
means editing shell config and re-sourcing it before the next cron run.
Doesn't survive shell session changes cleanly.

### Pattern 3 — scan a directory, pick up *.pst [CHOSEN]

```bash
export FSEARCH_PST_INPUT_DIR="/mnt/c/Users/.../Outlook Files"
```

The extractor globs `*.pst` (case-insensitive) under the input dir on
every run. Adding a new PST is a file copy; removing one is a delete.
Zero code changes, zero config edits.

**Why chosen**:
- Natural fit for archived PSTs which already live in a single Outlook
  files directory by Windows convention.
- Self-maintaining: rolls forward as the user adds yearly archives.
- Survives WSL restarts and shell session changes.
- Symmetric with how fs sources work: point at a directory, walk it.

**Tradeoffs accepted**:
- No way to *exclude* a single PST from a directory without moving it.
  Acceptable: if you don't want a PST indexed, you probably don't want
  it in your Outlook archives directory either.
- The input dir must be readable from WSL. For Windows-hosted PSTs this
  means the user must be running fsearch from WSL with `/mnt/c` mounted
  (the default). For Linux-native PSTs no special setup needed.

## Incremental extraction

PSTs rarely change once archived, but Outlook does occasionally append
to active PST files. Re-extracting a multi-GB PST every nightly run
would be wasteful.

**Strategy**: track each PST by `(absolute_path, size_bytes, mtime)` in
a small JSON state file at `<output_root>/.extract_state.json`. On each
run:

1. Glob the input dir for `*.pst` (case-insensitive)
2. For each PST, compute its `(size, mtime)` tuple
3. Compare to state file:
   - **Unchanged** (tuple matches): skip extraction entirely
   - **New** (not in state): extract to `<output_root>/<pst_stem>/`
   - **Modified** (tuple differs): wipe `<output_root>/<pst_stem>/`,
     re-extract, update state
4. Walk all `<output_root>/<pst_stem>/` directories (changed AND
   unchanged) to rebuild the manifest from scratch — this is cheap
   (just `email` header parsing) and ensures the manifest never gets
   out of sync with what's on disk.

**Alternatives considered**:
- Tracking individual messages by EntryID and merging into existing
  output dirs incrementally — only viable with pypff. The hash-based
  approach has the same effect end-to-end with simpler logic.
- Skipping the manifest rebuild and only updating entries for changed
  PSTs — saves a few seconds but introduces sync risk if a manual
  intervention (e.g., a deleted .eml) makes the manifest stale.

## Manifest population

For each extracted `.eml`:

1. Open and parse just the headers via `email.parser.BytesHeaderParser`
   (faster than parsing the full body)
2. Extract:
   - `Date` → `source_timestamp` (RFC2822 → ISO8601 conversion)
   - `From`, `To`, `Cc`, `Subject`, `Message-ID` → metadata dict
   - PST source filename (relative to input dir) and folder path
     (from the directory layout `readpst` produces)
3. Append to in-memory manifest dict, keyed by the `.eml` path relative
   to the source root

The manifest is written atomically (`.manifest.json.tmp` then rename)
at the end of the run, so a crash mid-extract leaves the previous
manifest intact.

## Folder hierarchy

`readpst -S` creates output like:

```
output_root/
  archive1/
    Inbox/
      <msgid>.eml
    Sent Items/
      <msgid>.eml
    Deleted Items/
      <msgid>.eml
```

We preserve this layout exactly. The `folder` field in the manifest
metadata records the folder name so users can search "Sent Items"
without renaming files. fsearch's existing path-based search already
handles this naturally — `path:"Sent Items"` works.

## What this source does NOT do

- **Attachment expansion**: attachments inside emails aren't extracted
  as separate files. They're part of the .eml body and Tika handles
  them at index time. If a future use case wants attachments as
  first-class search results, switch readpst to `-m` (also write .msg)
  or use pypff with attachment extraction.
- **Contact / journal export**: `readpst -te` limits output to email
  items. Outlook contacts and journal entries are intentionally
  ignored.
- **Encrypted PSTs**: `readpst` does support password-protected PSTs
  via `-p`, but we don't expose that. If you have one, the extractor
  will fail loudly and you can run readpst manually. Adding password
  support means storing secrets, which is its own design problem.

## Failure modes and observability

- **No PSTs found in input dir**: log warning, exit 0 (not an error)
- **readpst fails on one PST**: log the error, skip that PST (don't
  delete its previous extraction), continue with the rest
- **All PSTs fail**: exit nonzero so the source's `on_failure` policy
  in sources.yaml decides what fsearch does (typically `skip`)
- **Manifest write fails**: rollback (don't replace the existing
  manifest), exit nonzero
- **Output directory unwritable**: hard fail at startup with a clear
  message; no point trying to extract anything

All log output goes to stderr. The fsearch hook runner (`run_hook` in
fs_sources.py) captures it and surfaces failures into the main indexer
log automatically.

## When to throw this away

This extractor is intentionally small (under ~250 lines). If any of the
tradeoffs above start hurting:

- Performance bound by readpst → port to pypff
- Need attachment first-class indexing → pypff
- Need streaming for huge PSTs → pypff
- Need password support → pypff (and a secret store)

In all those cases, the right move is a fresh `extract.py` using pypff,
not patching this one. Keep the readpst version available as a fallback
since it has zero install dependencies once `pst-utils` is on the box.

# Extended Implementation Plan — Source Abstraction & Quick Wins

**Created**: 2026-04-11
**Status**: Active
**Scope**: Next 3 quick wins + 7-step source-abstraction refactor + 3 sources

This plan replaces ad-hoc backlog execution with an ordered, dependency-minimal
sequence. Each phase is independently shippable and leaves the system in a
working state. Deferred backlog items are NOT part of this plan.

---

## Guiding principles

1. **Schema-first, code-second.** Solr schema changes are cheap but irreversible
   mid-flight; land them before code that depends on them.
2. **Back-compat at every step.** Existing `INDEX_ROOTS` env var must keep
   working; existing cron keeps running; existing docs keep resolving.
3. **Ship vertically.** Each phase produces something observable (a new field,
   a new source, a new button) rather than a big-bang refactor.
4. **Sources are files-on-disk.** No plugin API, no Python import contract.
   Pull sources = shell hook + root directory. Push sources = just a directory.
5. **Manifest is optional.** If a source wants to enrich its docs with
   structured metadata, it drops a sibling `.manifest.json`. No manifest =
   filesystem metadata only, same as today.

---

## Phase 0 — Quick wins (independent, ship first)

These are small, valuable, and orthogonal to the source abstraction. Doing
them first gives momentum and validates that the deploy flow works end-to-end.

### 0.1 — "Open in…" actions in web GUI

**Goal**: one-click open a result row in VS Code, file manager, or copy path
to clipboard.

**Files touched**: `static/search.html` only. No server changes.

**Implementation**:
- Add a small actions column (or icon cluster appended to the path cell) with
  three buttons per row:
  - **Copy path** — `navigator.clipboard.writeText(filepath)`, green flash
  - **Open in VS Code** — `location.href = "vscode://file/" + filepath` —
    works on Windows-hosted VS Code when fsearch is opened from a Windows
    browser, because the `vscode://` handler is registered system-wide
  - **Open folder** — `location.href = "file:///" + dirname(filepath)` —
    caveat: modern browsers block `file://` navigation from `http://`
    origins. Fallback: copy the directory path to clipboard and flash a
    toast telling the user to paste it.
- No hotkeys; the existing click-to-expand behavior stays on the row body.
  Buttons `stopPropagation()` so clicking an action doesn't toggle the
  content panel.
- WSL→Windows path translation: fsearch paths are Linux-style
  (`/mnt/d/GT/...`). For `vscode://file/` we need `d:/GT/...`. Add a small
  JS helper `wslToWindows(path)` that rewrites `/mnt/<letter>/...` →
  `<letter>:/...`. If path doesn't match that pattern, leave it alone
  (Linux-native files just won't work from a Windows browser — acceptable).

**Acceptance**: clicking "Open in VS Code" on a `/mnt/d/...` result opens
the file in VS Code. Copy buttons flash green. Row click still expands
content.

**Estimate**: 30–60 min.

---

### 0.2 — Content hash for dedup (`content_sha256`)

**Goal**: compute a stable content hash at index time, store in Solr, enable
duplicate detection across the filesystem.

**Files touched**:
- `setup/setup_schema.sh` — add `content_sha256` field
- `fs_indexer.py` — `file_to_doc()` grows a hash call
- new `fsearch_hash.py` (or module) — adapted from `/bin/multiDigest.py`,
  tuned for fsearch's workload
- `fsearch_web.py` — optional `/api/duplicates` endpoint
- `static/search.html` — optional "find duplicates" link per row

**Implementation**:

**Step 1: schema**
```
{"name":"content_sha256", "type":"string", "stored":true, "indexed":true}
```
Single field, string type, indexed for equality lookup. Not copied into
`_text_`.

**Step 2: hasher module** (adapted from `/bin/multiDigest.py`)
- Keep the complementary-bit-flip protection (valuable even for small files,
  free once implemented).
- **Read chunk**: default 1 MB (`1 << 20`), configurable. Aligned to page
  size × N — on ext4/NVMe this is the sweet spot. Smaller chunks waste
  syscalls; larger chunks don't help once you're past the IO ceiling.
- **Skip large files over a threshold** unless `--large-files` is set.
  Rationale: a 2GB VM image takes ~20s to hash and rarely has dedup value.
  Threshold: same as existing `MAX_TEXT_SIZE` (reuses the large-file flag
  users already understand).
- **Content-hash cache**: keyed by `(filepath, mtime, size)` → sha256.
  Stored next to the existing find-cache, probably as a sqlite db for fast
  random access with 600k+ entries. On re-index, look up cache first; only
  rehash if mtime or size changed.

**Step 3: indexer integration**
- `file_to_doc()` calls the hasher after `extract_content()` (both already
  read the file; consider merging into one pass — stretch goal, not
  required initially).
- Hash failures are non-fatal: log and skip the field, don't skip the doc.

**Step 4: duplicate API (optional in this phase)**
- `POST /api/duplicates` returns groups of docs sharing a hash. Uses Solr
  faceting: `facet.field=content_sha256&facet.mincount=2`.
- Optional GUI button "Find duplicates of this file" that queries
  `content_sha256:"<hash>"` and shows sibling results.

**Acceptance**:
- `curl -s "$SOLR_URL/select?q=*:*&rows=1&fl=content_sha256"` returns a
  64-char hex string for recent docs.
- Re-indexing an unchanged file doesn't recompute the hash (verify via log
  instrumentation or a cache hit counter).
- At least one known duplicate pair is findable via the API.

**Estimate**: 3–4 hours (hasher tuning + cache + integration + testing).

**Open question**: full-index backfill strategy. New files get hashes going
forward, but 623k existing docs have no hash. Options: (a) lazy backfill on
next `--full` run, (b) dedicated `--backfill-hashes` flag that walks Solr
and adds hashes without touching other fields. Recommendation: **(b)**,
because (a) is a silent many-hours-long surprise. Make it explicit and
resumable.

---

### 0.3 — Language & refined MIME fields

**Goal**: facetable `language` and trustworthy `mimetype_detected` fields,
distinct from the current filename-guessed mimetype.

**Files touched**:
- `setup/setup_schema.sh` — add fields
- `fs_indexer.py` — read new Tika response headers
- `static/search.html` — optional: add `language` / `mimetype_detected` to
  the field picker

**Background**: Tika already detects both language and MIME type during
content extraction via the `/rmeta` or `/meta` endpoints. We currently only
use `/tika` (plain text extraction) and throw away the metadata. Switching
to `/rmeta/text` returns both text and metadata in one call — strictly
better, zero cost.

**Implementation**:
- Switch Tika call in `extract_via_tika()` from `PUT /tika` to
  `PUT /rmeta/text`. Response is a JSON array where the first element has
  both `X-TIKA:content` and Tika-detected headers like
  `Content-Language`, `Content-Type`, `dc:language`, etc.
- Parse the JSON, extract:
  - `mimetype_detected` ← `Content-Type` from Tika (strip parameters)
  - `language` ← `Content-Language` or `dc:language`, lowercased ISO-639
- Fall back gracefully: if `/rmeta/text` fails (older Tika, unusual file),
  use current `/tika` path and leave new fields empty.
- Schema: both new fields are `string`, indexed, stored. Single-valued.
- Keep existing `mimetype` field (filename-guessed) as-is for back-compat
  and fast filename-only MIME checks that don't need Tika.

**Acceptance**:
- `curl "$SOLR_URL/select?q=language:en&rows=0"` returns a count.
- `curl "$SOLR_URL/select?q=mimetype_detected:application/pdf&rows=0"`
  returns a count roughly matching the number of PDFs.
- Faceting on `language` shows a handful of detected languages across the
  corpus.

**Estimate**: 2 hours.

**Out of scope for this phase**: GUI faceting UI (that's a deferred backlog
item). We're just adding the data; UI follows later.

---

## Phase 1 — Schema foundations for source abstraction

**Goal**: add the schema fields the source abstraction will rely on, so that
subsequent code changes have stable targets.

**Files touched**: `setup/setup_schema.sh` only.

**New fields**:
| Field | Type | Stored | Indexed | Purpose |
|---|---|---|---|---|
| `source_name` | string | yes | yes | e.g., `"filesystem"`, `"pst-archive"`, `"gmail"`, `"outlook-work"` |
| `source_kind` | string | yes | yes | e.g., `"fs"`, `"pst"`, `"imap"`, `"msg"` — coarser classification |
| `source_timestamp` | pdate | yes | yes | Source-native timestamp (email sent-date, not filesystem mtime) |
| `source_metadata` | string | yes | false | JSON blob, opaque to fsearch, source-specific |

Why `source_metadata` is a string, not a nested doc: Solr nested docs are
painful to query and change the indexing model. We don't need to *search*
inside the metadata — we just need to retrieve it intact for display. String
JSON is the pragmatic choice.

**Back-compat**: all four fields are optional. Existing docs without them
continue to work; new fields default to empty/null on old docs. A one-liner
backfill can set `source_name="filesystem"` and `source_kind="fs"` on all
existing docs via a Solr atomic update (stretch goal, not blocking).

**Acceptance**: schema fields visible via `/schema/fields`. Indexer still
runs. Existing queries return identical results.

**Estimate**: 30 min.

---

## Phase 2 — `sources.yaml` config + source abstraction

**Goal**: introduce a `sources.yaml` config file that `fs_indexer.py` reads
at startup, and refactor the indexer to loop over sources rather than a flat
`roots` list.

**Files touched**:
- new `sources.yaml.example` (template)
- `fs_indexer.py` — config loader + per-source loop + lock/timeout handling
- `CLAUDE.md` — document the new config
- `.claude/context/01-architecture.md` and `03-data-models.md` — update

**Config shape** (`sources.yaml`):
```yaml
sources:
  - name: filesystem
    kind: fs
    root: /home/gerard
    # No hook — this is a plain filesystem crawl
    excludes:
      - node_modules
      - .git

  - name: wd1-gt
    kind: fs
    root: /mnt/wd1/GT

  - name: pst-archive
    kind: pst
    root: /mnt/wd1/sources/pst
    hook:
      command: /opt/fsearch/sources/pst/extract.sh
      timeout: 3600        # seconds
      lockfile: /mnt/wd1/sources/pst/.lock
      on_failure: skip     # skip | abort | continue-stale

  - name: gmail
    kind: imap
    root: /mnt/wd1/sources/gmail
    hook:
      command: /opt/fsearch/sources/gmail/sync.py
      timeout: 900
      lockfile: /mnt/wd1/sources/gmail/.lock

  - name: outlook-work
    kind: msg
    root: /mnt/c/OutlookExport
    # No hook — push source, produced by Windows-side tool
```

**Semantics**:
- Indexer reads `sources.yaml` at startup. If missing, falls back to env
  `INDEX_ROOTS` as a single implicit source named `legacy-fs` (full
  back-compat).
- For each source:
  1. Run `hook.command` if present. Capture exit code, enforce timeout.
     Acquire `lockfile` before running, release after.
  2. If hook succeeds (or no hook), walk `root` like today, tagging every
     doc with `source_name` and `source_kind`.
  3. If hook fails:
     - `on_failure: skip` — log, skip this source, continue with others
     - `on_failure: abort` — log, exit indexer with nonzero
     - `on_failure: continue-stale` — log, still walk the root with
       whatever's there (use case: Outlook COM pushed stuff yesterday,
       today's push failed, but yesterday's data is still worth indexing)
  4. Sources are processed sequentially (not parallel — keeps logs
     readable, avoids Tika thrash). Can revisit later.

**CLI additions**:
- `--sources PATH` — override config file location
- `--source NAME` — run only one named source (useful for testing hooks
  without a full re-index)
- `--list-sources` — print parsed config and exit

**Back-compat**:
- If positional `roots` CLI args are passed, they override the config
  entirely (behaves like today, tagged as `legacy-fs`).
- If `sources.yaml` is missing AND no roots given, fall back to
  `INDEX_ROOTS` env var (current behavior).
- `run_index.sh` cron wrapper needs no changes — config is read
  automatically.

**Lockfile details**:
- Separate from the existing indexer-wide lockfile. Source lockfiles
  prevent a *specific source's hook* from overlapping itself (e.g., PST
  extraction takes 2 hours and the next cron tick fires).
- Stale lock detection: lockfile contains a PID; if process is gone,
  lock is stale and can be taken.

**Acceptance**:
- `python3 fs_indexer.py --list-sources` prints parsed config
- `python3 fs_indexer.py --source filesystem` indexes only the fs source
- Existing `python3 fs_indexer.py /some/path` still works unchanged
- Docs acquire `source_name` and `source_kind` fields

**Estimate**: 4–6 hours. This is the biggest single step; budget extra.

---

## Phase 3 — Manifest reader

**Goal**: if a source produces a `.manifest.json` file alongside its data,
the indexer reads it and merges per-file metadata into Solr docs.

**Files touched**: `fs_indexer.py` — one new helper, called from the crawl
loop.

**Manifest format** (`.manifest.json` at source root or in subdirs):
```json
{
  "version": 1,
  "source_name": "gmail",
  "generated_at": "2026-04-11T08:30:00Z",
  "entries": {
    "inbox/2024/2024-03-15_subject-abc.eml": {
      "source_timestamp": "2024-03-15T09:14:22Z",
      "metadata": {
        "from": "alice@example.com",
        "to": ["bob@example.com"],
        "subject": "Subject ABC",
        "message_id": "<xyz@mail>",
        "labels": ["inbox", "important"]
      }
    },
    "...": {...}
  }
}
```

**Semantics**:
- Keys in `entries` are paths **relative to the source root**.
- During crawl, for each file, look up its relative path in the manifest.
- If found:
  - `source_timestamp` overrides the doc's timestamp field
  - `metadata` is JSON-serialized and stored in `source_metadata`
- If not found in manifest (or no manifest exists), doc is indexed normally
  with filesystem metadata only. No failure.
- Manifest is loaded **once per source**, cached in memory for the
  duration of that source's indexing pass.

**Why relative paths**: source root might move (e.g., you remount
`/mnt/wd1` at `/mnt/data`). Relative paths in the manifest survive this;
absolute paths would require regeneration.

**Edge cases**:
- Manifest references a file that doesn't exist → log debug, skip
- File exists but has no manifest entry → index normally
- Multiple manifest files in subdirs → merge them (deepest wins on
  conflicts). Simpler alternative: one manifest per source root only.
  **Recommendation: one per source root initially.** Revisit if needed.
- Malformed manifest → log error, index source without manifest data

**Acceptance**: handcraft a manifest, run indexer, verify `source_metadata`
is populated in Solr for matching files.

**Estimate**: 2 hours.

---

## Phase 4 — PST source (pull)

**Goal**: wrap `libpff`-based PST extraction as a pull source. Extracts
archived PSTs into `/mnt/wd1/sources/pst/` with a manifest.

**Files touched**: new `sources/pst/extract.py` (Python, runs under WSL).

**Dependencies**:
- `pypff` (Python binding for libpff-python) or the older `libratom` wrapper
- `pypff` has been flaky historically; `libratom` has better ergonomics but
  adds ~20 deps. Recommendation: try `pypff` first; fall back to `libratom`
  if too painful.

**Implementation**:
- Read a list of PST files from a config variable or env (e.g.,
  `PST_ARCHIVE_DIR=/mnt/c/Users/.../Documents/Outlook Files/`)
- For each PST file:
  1. Open via `pypff`
  2. Walk folders and messages
  3. For each message, write a `.eml` or `.msg` file to
     `$SOURCE_ROOT/<pst-name>/<folder-path>/<sent-date>_<subject-slug>.eml`
  4. Append to manifest: relative path, message metadata (from, to,
     subject, sent-date, message-id)
- Incremental: track already-extracted message-ids in a sidecar sqlite so
  re-runs only extract new messages. PSTs rarely change, but archived PSTs
  can grow when Outlook appends.
- Exit nonzero on fatal errors; log and continue on per-message failures.

**Acceptance**:
- Running `extract.py` produces `.eml` files + a `.manifest.json` under the
  source root.
- `fs_indexer.py --source pst-archive` picks up the extracted messages
  and tags them with `source_name=pst-archive`, `source_kind=pst`, and
  `source_timestamp=<sent-date>`.
- A search for an email sender substring finds the message.

**Estimate**: 4–8 hours (depending on pypff install pain and PST
peculiarities).

**Risk**: pypff builds from source on some systems and needs libpff-dev.
If install is painful, `libratom` or even a shell wrapper around the
`readpst` CLI tool (from `libpst`) is a solid fallback.

---

## Phase 5 — Gmail source (pull)

**Goal**: OAuth2 refresh-token flow → download new messages → write `.eml`
+ manifest.

**Files touched**: new `sources/gmail/sync.py`.

**Auth setup (one-time, manual)**:
1. Create a GCP project, enable Gmail API
2. OAuth consent screen: "Desktop app" type, minimal scopes
   (`https://www.googleapis.com/auth/gmail.readonly`)
3. Download `credentials.json` (client ID + secret) to a secure location
   NOT in the repo
4. First run: interactive browser auth, exchange code for refresh token,
   save to `~/.config/fsearch/gmail_token.json`
5. Subsequent runs: load refresh token, get fresh access token headlessly

**Implementation**:
- State file: `~/.config/fsearch/gmail_state.json` with `last_history_id`
  (Gmail's incremental sync cursor, more efficient than date-based queries)
- On run:
  1. Load refresh token, get access token
  2. Query `users.history.list` with `startHistoryId=<last>` to get
     messages added since last sync
  3. For each message: fetch raw RFC822 via `users.messages.get?format=raw`,
     decode base64url, write as `.eml`
  4. Path layout: `$SOURCE_ROOT/<year>/<month>/<message-id>.eml`
  5. Append to `.manifest.json` (update, don't rewrite — manifest grows
     incrementally)
  6. Save new `last_history_id`
- First-ever run: no `last_history_id` → fetch all messages, page by page.
  Warn about duration (large mailboxes = hours).

**Acceptance**: after `sync.py` runs, `.eml` files exist under the source
root. fs_indexer picks them up. A search for a known subject finds the
message.

**Estimate**: 4–6 hours.

**Security note**: `credentials.json` and `gmail_token.json` are secrets.
They live outside the repo (`~/.config/fsearch/`). Add `.gitignore` entries
just in case anyone copies them into the repo by accident.

---

## Phase 6 — Outlook COM source (push, separate repo)

**Goal**: a standalone Windows-side tool that reads live Outlook mailboxes
via COM and drops `.msg` files + manifest into a WSL-visible directory.

**New repo**: `outlook_com_export/` (separate from solrIndexer)

**Why separate**: Windows-native, pywin32 dependency, different lifecycle,
runs on Windows Task Scheduler not WSL cron. fsearch merely consumes its
output directory as a push source with no hook.

**Implementation sketch** (not executed in this plan — just outlined so
we know what hooks we need on the fsearch side):
- Python + pywin32 on Windows
- `win32com.client.Dispatch("Outlook.Application")`
- Walk `Namespace.Folders → MAPIFolder → Items`
- For each `MailItem`, call `SaveAs(path, olMSG)` to export as `.msg`
- Track exported EntryIDs in a sqlite to support incremental export
- Emit `.manifest.json` with sent-date, from, to, subject, EntryID
- Run on a schedule via Task Scheduler (not WSL cron)

**What fsearch needs** (already covered by phases 1–3):
- A push source entry in `sources.yaml` with no hook, just a root
- Indexer must handle `.msg` files (Tika already does this) and read the
  manifest (phase 3)

**Estimate**: 1–2 days for the separate project. **Not started in this
plan** — documented here so the abstraction is right.

---

## Phase 5.1 — Gmail sync refinements

**Guiding principle** (captured 2026-04-11, post-bring-up):
**"Fail fast, then fix it the correct way."** The Phase 5 live bring-up
surfaced three real correctness gaps plus a trivial cleanup item. Rather
than layer a production cron on top of known rough edges, close every
gap before promoting the source to nightly cron status. Discomfort of
"still not production" is temporary; the alternative (cron running
against a flaky sync path and silently wasting API quota, or worse,
losing data on interruption) accumulates debt that is much harder to
repay later. The daily volume of trivial-but-deletable messages in a
typical user's Gmail is low enough that deferring cron integration by a
few days costs nothing meaningful.

**Step-wise plan** (sequenced, independent commits):

1. **Sidecar cleanup** (~15 min) — extend `file_to_doc()`'s skip
   list beyond `.manifest.json` to also cover `.gmail_state.json`,
   `.extract_state.json`, and `.extract_state.sqlite`. Any future
   source can add its state file to the same list. Tiny warmup
   change that lives in the same file as everything else we're about
   to touch.

2. **Phase 5.1 core** (3–5 hr) — sqlite state DB, skip-if-known,
   migration helper, archive/mirror toggle. Details below. The
   substantive correctness work.

3. **Phase 5.1.5a — `sync.py --prune` CLI** (~2 hr) — the hard-delete
   tool. Takes a list of filepaths, removes each from disk + state
   DB + manifest atomically. Source-agnostic philosophy: doesn't
   know how the list was produced. See Phase 5.1.5 section below.

4. **Phase 5.1.5b — GUI curation clipboard** (~3–4 hr) — PubMed-style
   session clipboard for accumulating "messages to delete" across
   multiple searches. Exports to TXT which feeds 5.1.5a. See Phase
   5.1.5 section below.

5. **Phase 5.1.6 fetch optimization** (2–6 hr) — benchmark script
   first, measurement, then implementation only if decision
   thresholds are met. The `benchmark_fetch.py` tool is a **permanent
   commitment**: it stays in the repo alongside the chosen
   implementation as a regression-test backstop for any future fetch
   logic changes (auth library updates, Google API shifts, quota
   adjustments). Not a throwaway one-off.

6. **Production cron wiring** (~30 min) — migrate `run_index.sh`
   from positional roots to a `sources.yaml`-driven model, adding
   the Gmail source entry. Done LAST so cron only picks up a
   fully-vetted integration.

**Goal of this phase**: bring the Gmail source up to the robustness bar set by
the PST and Outlook COM sources. Specifically: never re-fetch a message whose
bytes are already on disk, and give users an explicit choice between
"local archive" (default, safe) and "mirror Gmail state" (opt-in).

**Motivation**: discovered during the Gmail Phase 5 bring-up with a
live mailbox. Current behavior: `_fetch_and_save()` unconditionally
overwrites the `.eml` file on disk whether or not the same message
was already fetched in a previous run. Three concrete problems:

1. **Interruption recovery is expensive.** If the first full sync is
   Ctrl-C'd partway through its ~26k-message walk, the next run
   re-fetches everything from scratch because the on-disk files don't
   factor into the dedup check. Only the manifest is loaded back, and
   the manifest alone doesn't gate API calls.
2. **No mirror/archive distinction.** A user who deletes a Gmail
   message expecting it to disappear from search is surprised when
   it's still there. A user who deletes a Gmail message by accident
   and expects search to preserve history is surprised if it isn't.
   Same default can't be right for both — needs to be explicit.
3. **Labels go stale.** `labelsChanged` history events are ignored,
   so `source_metadata.labels` decays. Lower priority; deferred to
   Phase 5.2 after we've seen how much it matters in practice.

**Files touched**:
- `sources/gmail/sync.py` — state DB, skip-if-known, optional delete handling
- `sources/gmail/DESIGN.md` — record the decisions and rejected alternatives
- `docs/vignettes/gmail-wiring.md` — mention `FSEARCH_GMAIL_MIRROR` env var

**Changes**:

### 1. SQLite state DB

Add a sibling `.gmail_state.sqlite` next to `.gmail_state.json` with
one table:

```sql
CREATE TABLE fetched (
    msg_id       TEXT PRIMARY KEY,   -- Gmail message ID
    relpath      TEXT NOT NULL,      -- .eml path relative to output root
    internal_ms  INTEGER,            -- for sanity-check only
    fetched_at   TEXT                -- ISO8601 UTC
);
```

Why SQLite and not JSON: a 26k-msg mailbox is right at the edge where
JSON atomic writes get uncomfortable. Every subsequent sync appends
to the state, and by year 5 this is easily 50k+ entries. Matches the
Outlook source's choice for the same reason.

Separate from `.gmail_state.json`: the JSON file carries the history
cursor and is rewritten atomically on each run. The sqlite is the
"what's on disk" ledger and gets appended to as fetches succeed.
Two different lifecycles.

### 2. Skip-if-known check

`_fetch_and_save()` grows a preliminary query:

```python
if _is_known(conn, msg_id):
    return True   # already on disk from a previous run
```

Also wrap the existing manifest-update so a skipped message still has
its manifest entry copied forward from the loaded previous-run
manifest (already handled since we load existing entries at startup).

### 3. Migration helper

For users upgrading from Phase 5 to 5.1 with an already-populated
output dir, automatically backfill the sqlite DB from the on-disk
`.eml` tree on first startup:

- If the sqlite file is absent BUT the output root has `.eml` files,
  walk the tree, extract the Gmail msg_id from each filename
  (it's embedded in our path layout as the `<short-id>` suffix)
- Insert rows into the new DB without fetching anything
- Log `"Migrated N existing files to state DB"`
- Subsequent runs are fast

Caveat: the filename currently stores only the first 16 chars of the
msg_id, which is *probably* unique across a user's mailbox but isn't
guaranteed by the Gmail API. For the migration path, we take the
pragmatic approach: if two different full msg_ids truncate to the
same prefix, one of them gets re-fetched on the next incremental
run (the sqlite insertion for the dupe prefix fails, fetch proceeds,
new file overwrites). Acceptable edge case for a one-time migration.

### 4. Mirror vs archive toggle

New env var `FSEARCH_GMAIL_MIRROR` (default: unset = archive mode).

- **Archive mode (default)**: `messagesDeleted` history events are
  logged at debug and ignored. Local `.eml` files never disappear
  because Gmail state changed.
- **Mirror mode (`FSEARCH_GMAIL_MIRROR=true`)**: `messagesDeleted`
  events cause the corresponding `.eml` file AND sqlite row to be
  removed. The next `fs_indexer.py` run's purge pass notices the
  file is gone and deletes the Solr doc.

Rationale for archive-as-default:
- The risk of an accidental Gmail delete silently erasing a search
  archive is worse than the cost of some stale `.eml` files
- Once a message is in Solr, the local file is the "reason" for
  the Solr doc's existence — mirroring Gmail state breaks that
  causal chain
- Users who genuinely want mirror mode are technical enough to set
  an env var; users who haven't thought about it benefit from the
  safer default

### 5. Documentation

`sources/gmail/DESIGN.md` grows a new section explaining the
archive/mirror distinction, citing the causal-chain argument above.
`docs/vignettes/gmail-wiring.md` gets a new "Ongoing maintenance"
entry under "When deletes matter" explaining the knob.

### What this phase does NOT do

- **Label-change tracking** — deferred to Phase 5.2. The right fix
  is metadata-only refetch via `format=metadata` on `labelsChanged`
  history events, but we want real-world observation of staleness
  before investing in it.
- **`.eml` integrity verification** — no MD5/SHA check between disk
  and Gmail's returned bytes. Gmail's RFC822 output for a given
  message is stable; verification would cost an extra hash per
  existing file for zero real benefit.
- **Rate limiting / backoff tuning** — if the skip-if-known reduces
  API calls enough that Phase 5 quota pressure disappears, no need.
  Revisit only if we see 429s in practice.

**Acceptance criteria**:

1. Run a full sync, Ctrl-C at ~5% progress. Re-run. The second run
   completes in seconds, not hours, because `_is_known` returns True
   for everything already on disk.
2. Delete one `.eml` file manually and re-run. The script refetches
   *that* message (because it's not in the sqlite either) and leaves
   the rest alone.
3. `fs_indexer.py --source gmail` still produces the same Solr
   output as Phase 5. No schema changes.
4. Upgrading from a Phase-5 install with an existing manifest and
   ~26k `.eml` files: one migration log line, then normal
   incremental behavior.

**Estimate**: 3–5 hours. Includes DESIGN.md update, docs update,
and manual upgrade-path testing. No new dependencies.

---

## Phase 5.1.5 — Curation workflow

**Goal**: support the "find-review-curate-act" workflow where a user
searches for email they think should be deleted, reviews the hits,
accumulates a list across multiple searches, then uses that list
to drive a hard delete of both the remote Gmail messages (via the
Gmail web UI — outside fsearch) AND the local `.eml` archive + Solr
docs (via fsearch).

**Workflow end to end**:

1. Search by keywords, date ranges, sender, etc. in the web GUI
2. Review hits, optionally click rows to preview content
3. Check boxes next to messages that should be deleted
4. Click "Send to clipboard" — checked items join a session-scoped
   clipboard, checkboxes clear
5. Repeat 1-4 across as many searches as needed
6. Open the clipboard page, review full list, remove any
   mistaken entries via "Remove from clipboard"
7. Export clipboard as a TXT file (one filepath per line)
8. Delete the messages from Gmail via its web interface
9. Feed the TXT file to `sync.py --prune` which atomically removes
   each `.eml` from disk, its row from the state DB, and its
   manifest entry. The next `fs_indexer.py` run's purge pass
   removes the Solr doc.

Steps 1-7 are client-side curation. Steps 8-9 are the hard-delete
trigger. Keeping the destructive action in a separate CLI step
(not a GUI button) is deliberate — see "Rejected alternatives"
below.

**Two independent deliverables**, built in this order:

---

### 5.1.5a — `sync.py --prune` (CLI)

Ship first. ~100 LOC. Can be used immediately with manually-assembled
lists (e.g., paths copied from the existing "Copy path" row button
and pasted into a text file), so the hard-delete path gets validated
in isolation before any GUI wraps it.

**Interface**:

```bash
/opt/fsearch/sources/gmail/sync.py --prune <file>
/opt/fsearch/sources/gmail/sync.py --prune -      # read from stdin
```

Input format: one filepath per line. Lines starting with `#` are
comments. Blank lines skipped. Paths must be absolute. Each path
must match either a relative path under FSEARCH_GMAIL_OUTPUT OR an
absolute path that's inside FSEARCH_GMAIL_OUTPUT — rejecting paths
outside the source root is a safety check against fat-fingered
copy-paste deleting random files.

**Semantics** per path:

1. Resolve and validate (must be under output root, must exist or
   its state DB row must exist — a ghosted row with missing file is
   still cleanable)
2. Compute relative path from output root
3. Begin transaction on the state DB
4. Look up the msg_id by relpath (SELECT msg_id FROM fetched WHERE
   relpath = ?)
5. Delete the state DB row
6. Delete the `.eml` file on disk (unlink; ignore FileNotFoundError)
7. Commit transaction
8. Remove the entry from `.manifest.json` (in-memory, rewritten once
   at the end for all processed paths, atomic via `.tmp` + rename)

**Failure handling**:

- Per-path errors (not found, path outside root, state DB row missing
  AND file missing): log warning, continue with next path
- Unreadable input file or unwritable state DB: hard fail
- If the manifest rewrite fails at the end, the individual `.eml`
  + state DB removals stay (they're already committed). Log an
  error and exit nonzero. Next run will reconcile — the manifest
  will have stale entries pointing at missing files, which is
  handled gracefully by the Phase 3 manifest reader (missing files
  return no entry).

**Reporting**: print a summary line like
`Pruned: 47 successful, 2 skipped, 0 failed`. Exit 0 on full
success, 1 on partial, 2 on hard failure.

**Explicitly NOT done by this command**:

- Does NOT talk to Gmail. You delete from Gmail separately. If you
  run `sync.py --prune` without first deleting from Gmail, the next
  incremental sync will happily re-download the messages you just
  pruned (because Gmail still has them; the history cursor will
  report them as "added"). That's recoverable, just wasteful — a
  note in the DESIGN.md addition below makes this ordering
  requirement explicit.
- Does NOT remove the Solr document. fsearch's existing per-source
  purge pass (scoped by `source_name`) handles that on the next
  `fs_indexer.py` run.
- Does NOT modify the history cursor. Pruning doesn't advance time.

**Safety features**:

- Dry-run flag: `--prune-dry-run FILE` prints what would happen
  without touching disk or DB. Recommended for every first use.
- A confirmation prompt when run interactively (`isatty()`) for
  >10 paths. Skipped when reading from a pipe (for scripting).
  Override with `--yes`.

**Files touched**:
- `sources/gmail/sync.py` — new `--prune` / `--prune-dry-run` /
  `--yes` flags, new `_prune_from_file()` function
- `sources/gmail/DESIGN.md` — new "Destructive operations" section
  documenting the ordering requirement and why GUI doesn't have a
  delete button
- `docs/vignettes/gmail-wiring.md` — new "Pruning messages" section

**Acceptance criteria**:

1. Feed a TXT file with 5 valid paths → all 5 removed atomically,
   exit 0.
2. Feed a file with 1 valid + 1 path outside the source root → the
   valid path is pruned, the invalid one is rejected with a clear
   error, exit 1 (partial).
3. `--prune-dry-run` with the same inputs reports what would happen
   and returns exit 0 without touching anything.
4. Next `fs_indexer.py` run's purge pass removes the corresponding
   Solr docs via the `source_name:gmail` cursor scan (existing
   Phase 2 logic — just verify it still works with the new
   filesystem state).

**Estimate**: 2 hours.

---

### 5.1.5b — GUI curation clipboard

Ship after 5.1.5a validates the hard-delete path. ~300 LOC split
between `static/search.html` (JS+CSS) and one small new endpoint
in `fsearch_web.py`.

**Storage model**: `sessionStorage` keyed by browser tab. Single
clipboard (not multi-named — PubMed-style). Dies when the tab
closes. No time-based expiry. No auto-save to disk.

**Clipboard contents**: a list of Solr `id` strings (which is also
the filepath in our schema). IDs only, not cached metadata — the
clipboard page fetches fresh doc data from Solr when opened, so
any doc that's been re-indexed (hash update, source_timestamp
correction, etc.) shows its latest state.

**UI elements added to `static/search.html`**:

- **Checkbox column** (leftmost) in the results table. Narrow
  (~30px). Row checkbox `onclick` calls `stopPropagation()` so the
  existing click-to-expand-preview behavior still works on the row
  body.
- **Header checkbox**: "select/deselect all visible results".
  Applies only to current page, not to anything else.
- **Visual marker on rows already in the clipboard**: a
  pre-rendered "✓ In clipboard" indicator instead of a clickable
  checkbox, with tooltip, so the user can see "I already caught
  this one in an earlier search". Prevents re-adding duplicates
  and makes repeat queries useful for coverage-checking.
- **"Send to clipboard (N)" button** in the controls bar, alongside
  the existing Search / Add row / NOT→end / Clear / Export CSV /
  Export TXT buttons. Disabled when zero checkboxes are checked.
  Shows the pending-check count in its label.
- **Clipboard badge** in the top-right of the controls bar: `📋 47`.
  Clickable — opens the clipboard page. Updates live via a custom
  `clipboardchange` event dispatched on the window.

**Clipboard page** (modal overlay):

- Fetches full doc metadata via new `POST /api/docs_by_id` endpoint
  (see below). Batches if more than ~500 IDs to stay under Solr's
  default 1024-clause limit.
- Renders a table matching the main results layout: path, ext,
  size, modified. Each row has a ✕ button to remove from clipboard.
- Page-level actions: **Clear clipboard**, **Export CSV**,
  **Export TXT**, **Export JSON**. All exports are assembled
  client-side from the already-fetched docs — no new server-side
  export endpoint.
- Close button or click-outside-modal to dismiss.

**New Flask endpoint** — `POST /api/docs_by_id`:

- Request body: `{"ids": ["...", "..."]}`
- Uses Solr's `{!terms f=id}value1,value2,...` syntax which handles
  up to several thousand values per query by default (much more
  than `q=id:(a OR b OR ...)` with its 1024 Boolean clauses limit)
- Returns same doc shape as `/api/search` for consistency (same
  `fl` list: filepath, filename, size, mtime, extension, directory,
  content_preview, content_sha256, language, mimetype_detected,
  source_name, source_kind, source_timestamp, source_metadata)
- If more than ~1000 IDs, batches internally and merges results

**Export formats from the clipboard** (client-side assembly):

- **CSV**: same column set as the existing `EXPORT_COLUMNS` list in
  `fsearch_web.py` and `fsearch.py` — filepath, filename, extension,
  size_bytes, mtime, directory, content_sha256, language,
  mimetype_detected. Header row included.
- **TXT**: one filepath per line. This is the format `sync.py --prune`
  consumes, so it's the direct-pipeline export.
- **JSON**: array of full doc dicts.

Filename format: `fsearch_clipboard_YYYYMMDD_HHMMSS.{ext}`.

**Files touched**:
- `static/search.html` — biggest change, ~250 lines of JS for
  clipboard module + UI wiring + clipboard-page rendering + export
- `fsearch_web.py` — one new endpoint (~30 lines)
- `docs/vignettes/gmail-wiring.md` — new "Curation workflow" section
  showing the full 1-9 step flow end-to-end

**Acceptance criteria**:

1. Search → check 3 rows → Send to clipboard → badge shows "3".
   Checkboxes on those 3 rows clear. Run a new search that includes
   one of those 3 rows in its results — that row shows "✓ In
   clipboard" indicator instead of a checkbox.
2. Open clipboard page → see 3 rows with full metadata. Remove one
   via ✕ → badge drops to "2".
3. Export TXT → download a file with 2 filepaths, one per line.
4. Feed that TXT to `sync.py --prune` → both messages removed from
   state DB, disk, and manifest. Next fs_indexer run purges Solr
   docs. Clipboard is still populated (export doesn't clear), but
   a new search shows those rows are gone.
5. Close the tab → open fsearch in a new tab → clipboard is empty
   (`sessionStorage` scoping).
6. "Clear clipboard" on the clipboard page → empties the clipboard
   immediately.

**Explicitly NOT in this phase**:

- **Per-clipboard naming** ("receipts", "newsletters", ...): YAGNI.
  If a curation session needs multiple categories, do multiple
  export+prune cycles.
- **Saving clipboard state to disk**: sessionStorage only. The
  "export to TXT" path already serves as a save mechanism for
  anyone who wants persistence.
- **A GUI delete button**: destructive operations stay in the CLI.
  The GUI is read-only and curation-only, which keeps the blast
  radius of a browser bug or a misclick near-zero.
- **Selecting checkboxes across pages without "Send to clipboard"
  in between**: explicit commit required per page. This avoids
  losing pending selections when pagination changes the result
  set.
- **Keyboard shortcuts** for check all / send: the existing
  Ctrl+Enter / Ctrl+Shift+A shortcuts are enough for v1.

**Estimate**: 3–4 hours.

---

### Rejected alternatives for Phase 5.1.5 (the whole phase)

**GUI delete button** — a "Delete from clipboard + disk + Solr"
button on the clipboard page. Tempting because it collapses steps
7-9 into one click. Rejected because:

- Destructive operations deserve friction. A one-click delete
  against 500 messages is a foot-gun even with "Are you sure?"
  dialogs.
- The CLI step also enforces a natural "did you actually delete
  these from Gmail first?" pause.
- The separation makes the hard-delete path auditable. The TXT
  export file is a record of what was destroyed; running `--prune
  --prune-dry-run file.txt` and diffing its output against the
  real `--prune` run gives you a sanity check that no other process
  modified state between steps.

**Automatic Gmail delete via API** — have `sync.py --prune` also
delete the messages from Gmail. Rejected because:

- Would require re-scoping the OAuth credentials from `gmail.readonly`
  to `gmail.modify`, which is a much broader permission and a much
  scarier test-user consent dialog.
- The user-visible workflow benefits from Gmail's UI for the
  review-before-delete step — bulk operations in the Gmail web UI
  are well-understood and have their own undo affordances.
- The hard coupling would mean a bug in `--prune` could irreversibly
  destroy email. Keeping the two systems separate means a bad
  `--prune` only destroys local state, which can be re-fetched via
  a standard sync.

**Per-named clipboards** — like the "Collections" feature in
PubMed. Rejected for v1: PubMed supports many concurrent curation
sessions because its users are doing literature reviews. Our users
are doing one-shot cleanups. If we later find that one-shot is
wrong, multi-named is strictly additive.

**localStorage instead of sessionStorage** — survive tab close.
Rejected because the user explicitly said "strictly per-session"
and that matches the PubMed behavior they're used to. The export
path gives anyone who wants persistence a way to save.

---

### Sequencing inside 5.1.5

Build **5.1.5a first**, then **5.1.5b**, then test the end-to-end
workflow (search → clipboard → export → prune → re-index → verify).
Don't skip the dry-run validation at step 4 of the acceptance
criteria — it's the cheap insurance that guards against subtle
state-DB corruption bugs.

---

### 5.1.6 — Fetch optimization (optional sub-task)

**Motivation**: the Gmail Phase 5 first-sync is fetch-bound, not
index-bound. Concrete measurement from the initial bring-up showed
~100 msgs/min = ~600ms per message, almost entirely network round-trip
to `googleapis.com`. Tika indexing of the resulting `.eml` files is
12–30× faster. So any meaningful speedup for first-syncs and large
re-syncs lives in the fetch phase.

**Approach**: "measure first, then optimize." Do NOT implement any
parallelism before running a small benchmark against real API quota
limits. A blind parallel rewrite risks 429s, quota burn, and
wall-clock regressions from retry loops.

**The benchmark**:

After Phase 5.1 core (sqlite state DB + skip-if-known) ships and a
user has a populated state DB with N known-good message IDs, run a
standalone microbenchmark script `sources/gmail/benchmark_fetch.py`
that fetches a fixed subset (e.g., 200 messages selected by
`ORDER BY internal_ms DESC LIMIT 200`) in three modes:

1. **Serial** (current Phase 5 path): one `messages.get` at a time
2. **ThreadPoolExecutor with N=5**: each worker has its own HTTP
   connection but shares credentials + service object
3. **BatchHttpRequest with batch_size=50**: Google's native batching
   API (`googleapiclient.http.BatchHttpRequest`) — up to 100
   sub-requests per HTTP call, Gmail recommends ≤50

For each mode record:
- Wall-clock time for the 200-message run
- Total quota units charged (via response headers or a delta on
  Google's quota dashboard)
- Any HTTP 429 / `userRateLimitExceeded` / `rateLimitExceeded` errors
- Observed per-message throughput distribution (p50, p95, p99)

**Decision rules** (applied after measurement):

- If mode 3 is >5× faster than mode 1 with zero rate-limit errors,
  ship mode 3 as the default. This is the "real" expected outcome.
- If mode 3 hits rate limits but mode 2 is >3× faster without errors,
  ship mode 2 as the default. Threads are a cleaner fallback than
  half-batching.
- If neither is >3× faster than serial, ship neither; document the
  null result in DESIGN.md and move on.
- Keep serial code path as a `FSEARCH_GMAIL_CONCURRENCY=serial`
  fallback regardless of which mode wins, so users who hit quota
  issues have an escape hatch.

**Orthogonal micro-optimizations** (land with whichever concurrency
mode wins):

- **`fields` mask on messages.get**: current code accepts the full
  JSON response and discards most of it. Adding
  `fields='raw,internalDate,labelIds,threadId'` to the get request
  saves ~30% response bandwidth per call. One-line change, no risk,
  applies uniformly to serial and parallel paths.
- **HTTP/2 connection reuse**: the `googleapiclient` transport uses
  `httplib2` which supports keep-alive but not HTTP/2. Switching to
  `google-auth-httplib2` with a pooled session object is a separate
  experiment; measure whether it helps *after* the main concurrency
  change lands. Skip if mode 3 (batching) is already fast enough.

**Rejected approaches**:

- **`asyncio` + `aiohttp` rewrite**: would give cleaner concurrency
  primitives but no meaningful speedup over threads for this
  workload. The fetch loop is IO-bound; threads handle that just
  fine, and keeping the sync code structure means less risk of
  subtle bugs in the auth/retry paths.
- **Pipelining fetch vs. disk write**: ~5% potential win since
  writes to `/mnt/wd1` NVMe are in the low-millisecond range. Not
  worth the producer/consumer queue complexity unless we're already
  rewriting the fetch loop.
- **Overlapping sync with fs_indexer walk**: wall-clock win bounded
  by min(fetch, index), which for Gmail is ~20% (index is 12–30×
  faster than fetch, so most overlap is wasted). Breaks the clean
  hook+walk contract of the source abstraction. Explicitly deferred.
- **`watch` + Pub/Sub push**: Gmail supports real-time push
  notifications via Cloud Pub/Sub. Significant architectural shift
  (requires a long-running webhook receiver or a Pub/Sub pull
  worker), sub-minute latency is not a current goal, one-off first
  sync is the pain point. Not this phase. Documented as a "when to
  throw this away" trigger in sources/gmail/DESIGN.md.
- **Headers-only metadata-only indexing**: a different product, not
  an optimization. Solr would hold manifest-sourced metadata
  (from/subject/date) but not body content. No full-text search.
  Potentially valuable for mailboxes where full-body fetch is
  impractical (hundreds of thousands to millions of messages), but
  Phase 5.1 is scoped to "make the current functionality faster,"
  not "ship a lite variant." Record as a future Phase 5.3 idea if
  real demand appears.

**Files touched** (if an optimized mode ships):

- `sources/gmail/sync.py` — new concurrency dispatcher around
  `_fetch_and_save`, new `FSEARCH_GMAIL_CONCURRENCY` env var
- `sources/gmail/benchmark_fetch.py` — the measurement tool itself,
  **permanently committed** to the tree as a regression-test backstop.
  Any future change to fetch logic (auth library update, Google API
  shift, concurrency-mode re-tuning, quota policy changes) should
  re-run this before merging. The tool is NOT optional follow-up
  work; it ships alongside whichever implementation wins the
  benchmark, in the same commit.
- `sources/gmail/DESIGN.md` — decision record with actual measured
  numbers (not estimates) and the rejected approaches

**Acceptance criteria** (in addition to the core 5.1 criteria):

5. `benchmark_fetch.py` runs cleanly against a populated state DB
   and produces a three-column comparison table on stderr.
6. If an optimized mode ships, a 1000-message re-sync from a clean
   state is at least 3× faster than the Phase 5 baseline measured
   today (~100 msgs/min).
7. `FSEARCH_GMAIL_CONCURRENCY=serial` still works as an escape
   hatch and matches Phase 5 behavior byte-for-byte on output.
8. No new 429 errors in logs under default settings. Document the
   observed quota ceiling in DESIGN.md for future reference.

**Estimate**: 2–4 hours IF batching works as advertised. 4–6 hours
if we have to fall back to threads and re-measure. Includes the
benchmark tool, the chosen implementation, and the DESIGN.md update.

**Dependency note**: 5.1.6 depends on 5.1 core (sqlite + skip-if-known)
because the benchmark needs a populated state DB to select known-good
IDs. Ship core first, let it run against real data for at least one
incremental cycle, then benchmark.

---

## Sequencing summary

| Phase | Deps | Est. | Ship independently? |
|---|---|---|---|
| 0.1 Open-in  | — | 30–60 min | Yes |
| 0.2 Hash     | — | 3–4 hr | Yes |
| 0.3 MIME/lang| — | 2 hr | Yes |
| 1 Schema     | — | 30 min | Yes |
| 2 Sources    | 1 | 4–6 hr | Yes |
| 3 Manifest   | 1,2 | 2 hr | Yes |
| 4 PST        | 1,2,3 | 4–8 hr | Yes |
| 5 Gmail      | 1,2,3 | 4–6 hr | Yes |
| 5.1 Gmail refinements (core: sqlite + skip-if-known + mirror/archive) | 5 | 3–5 hr | Yes |
| 5.1.5a Curation — `sync.py --prune` CLI | 5.1 core | 2 hr | Yes |
| 5.1.5b Curation — GUI clipboard + `/api/docs_by_id` | 5.1.5a | 3–4 hr | Yes |
| 5.1.6 Fetch optimization (benchmark + concurrency) | 5.1 core | 2–6 hr | Yes |
| 6 Outlook    | 1,2,3 (+separate repo) | 1–2 days | External |

Each phase is committed separately. Total in-repo work: ~25 hours of focused
effort, spread across sessions.

---

## Commit strategy

One commit per phase, with a clear prefix:
- `feat(gui): open-in actions (vscode / folder / copy)` — phase 0.1
- `feat(index): content_sha256 field and hasher` — phase 0.2
- `feat(index): language and mimetype_detected from Tika rmeta` — phase 0.3
- `feat(schema): add source_name / source_kind / source_timestamp / source_metadata` — phase 1
- `feat(index): sources.yaml config and per-source indexer loop` — phase 2
- `feat(index): manifest.json reader for source metadata enrichment` — phase 3
- `feat(sources): pst archive extractor` — phase 4
- `feat(sources): gmail incremental sync via oauth refresh token` — phase 5

Phase 6 happens in its own repo and gets its own history.

---

## Execution notes

- Deploy via `./deploy.sh` after every phase to keep `/opt/fsearch` in sync.
- Schema changes require Solr restart? No — `/schema` API is live. But
  verify after each schema change with `/schema/fields`.
- Test incremental re-indexing after the indexer refactor (phase 2) — the
  cache+checkpoint interaction is the most likely regression surface.
- Do NOT backfill hashes or source-fields in the same phase that adds them.
  Backfill is its own step with its own acceptance criteria.

---
*This plan is a living document. Update acceptance criteria and estimates
as reality intrudes.*

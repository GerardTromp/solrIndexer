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

**Goal**: bring the Gmail source up to the robustness bar set by the PST
and Outlook COM sources. Specifically: never re-fetch a message whose
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
  kept in the tree so future regressions can be checked
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

# Gmail source — design notes

**Created**: 2026-04-11 (Phase 5)
**Updated**: 2026-04-11 (Phase 5.1 core: sqlite state DB, skip-if-known, mirror/archive)
**Status**: implemented as a Gmail-API + refresh-token pull source, with
Phase 5.1 correctness refinements applied

This document records the choices behind the Gmail extractor and the
options that were considered but not taken. Same purpose as the PST
sibling doc: a future visit shouldn't have to re-discover tradeoffs.

## Authentication strategy

Three options exist for headless Gmail access:

### Option A — IMAP with app password [REJECTED]

Classic IMAP with a Google "app password" (a 16-char generated
credential that bypasses 2FA for legacy clients).

**Why rejected**:
- Google has been progressively deprecating "less secure app access"
  and app passwords are only available when 2FA is enabled. Workspace
  admins can disable them entirely, which makes the integration
  fragile in any tenant other than personal Gmail.
- Doesn't expose Gmail-specific features (labels, threading, history
  cursor for incremental sync). You'd have to fall back to date-based
  IMAP queries which are slower and incomplete.
- No fine-grained scopes — IMAP gives full mailbox read access; the
  Gmail API can be locked to `gmail.readonly` or even
  `gmail.metadata`.

When to revisit: never, for personal use. Only consider for legacy
mailboxes hosted on non-Google IMAP servers, in which case build a
separate `imap` source rather than reusing this one.

### Option B — Service account + domain-wide delegation [REJECTED]

A GCP service account that's been granted domain-wide delegation in a
Google Workspace admin console. Truly headless — no refresh tokens,
no consent screens, no human in the loop.

**Why rejected**:
- Only works for **Workspace** accounts where you're the domain admin
  (or can convince one to grant the delegation). Doesn't work for
  personal `@gmail.com` addresses at all.
- Massive overkill for single-user indexing; the security implications
  of domain-wide delegation are significant and admins (rightly) push
  back on it for hobby tools.
- More moving parts to set up (service account JSON keys, scope
  whitelisting in admin console, audit log noise).

When to revisit: if fsearch ever becomes a multi-user service for a
Workspace tenant where one indexer should sync many mailboxes. That's
a different project.

### Option C — OAuth2 user consent + refresh token [CHOSEN]

Standard "installed app" OAuth flow:
1. Create a GCP project, enable Gmail API
2. Create an OAuth client ID (type: Desktop app)
3. First-run script does the browser consent dance, exchanges the
   authorization code for an access + refresh token, stores both
4. Subsequent runs use the refresh token to mint fresh access tokens
   headlessly

**Why chosen**:
- Works for **both** personal Gmail and Workspace mailboxes — same
  flow, same code, same scopes.
- Refresh tokens don't expire as long as you use them at least once
  every 6 months and don't revoke them, so a daily cron run keeps
  them alive indefinitely.
- Read-only scope (`gmail.readonly`) means even if the token leaks,
  the worst an attacker can do is read your email — they can't send,
  delete, or modify anything.
- This is exactly how Thunderbird, Outlook for Mac, and every other
  desktop Gmail client authenticates today. Battle-tested flow.

**Tradeoffs accepted**:
- One-time human-in-the-loop setup. Cannot be fully automated:
  someone has to click "Allow" in a browser exactly once. After
  that, headless forever.
- Refresh tokens are secrets. Live in `~/.config/fsearch/`, mode 600,
  out of the git repo by convention.
- Google Cloud Project must exist. For personal use this means
  creating a project (free), enabling the Gmail API, and creating
  an OAuth client. Took me ~5 minutes once you know the steps.

## Sync strategy: history cursor vs. date filter

Gmail offers two APIs for "what's new since last sync":

### Option A — Date-based query (`q='after:YYYY/MM/DD'`) [REJECTED]

Use the standard `users.messages.list` endpoint with a date filter.
Simple and works against any IMAP-style mental model.

**Why rejected**:
- Misses messages whose `internalDate` was set in the past but
  arrived recently (forwards from old archives, backdated drafts).
- One-day granularity at best — re-syncs the entire current day on
  every run, so the work scales linearly with mailbox volume.
- Can't detect deletes or label-only changes; only sees additions.

### Option B — History cursor (`users.history.list`) [CHOSEN]

Gmail tracks every change (message added, deleted, labeled, moved)
under a monotonically increasing `historyId`. The history endpoint
returns "everything that happened since historyId X" — additions,
deletions, label moves, the lot.

**Why chosen**:
- True incremental sync: each run does work proportional to
  *changes*, not to mailbox size. After the first full sync, daily
  runs typically pull a few hundred messages and run in seconds.
- Catches edge cases the date filter misses (label changes, deletes).
- The cursor is a single integer; trivial to persist and to reason
  about.

**Tradeoffs accepted**:
- The first run has no cursor and must fall back to a full list of
  message IDs (potentially thousands of API calls for large
  mailboxes). This is a one-time cost.
- History entries expire after about a week — if a sync is skipped
  for longer than that, the cursor becomes invalid and we have to
  re-list and re-sync from scratch. The script handles this
  automatically; the user just sees a slow run on the recovery day.

## Format: how messages land on disk

Three options for the on-disk representation:

### Option A — Gmail JSON dump [REJECTED]

Save each message as the raw JSON the Gmail API returns
(`format=full`). Preserves every Gmail-specific field (labels,
thread IDs, snippet, internal dates).

**Why rejected**:
- Tika doesn't natively parse Gmail JSON. We'd need a separate
  content extractor or custom Solr ingestion path.
- The same data is preserved in `.eml` form via `format=raw`, just
  in a parser-friendly shape.

### Option B — `.eml` files (RFC822 raw form) [CHOSEN]

Use `users.messages.get?format=raw`, base64url-decode the result,
write each message as a `.eml` file. Identical structure to what the
PST extractor produces.

**Why chosen**:
- Symmetric with the PST source: same on-disk format, same Tika
  pipeline, same fsearch code path. One mental model for emails
  regardless of origin.
- `.eml` is a universal format. Any future tooling (export, archive,
  re-extract) just needs to understand RFC822, which everything does.
- Preserves the original message bytes including all headers, MIME
  parts, attachments — no information loss.

**Tradeoffs accepted**:
- Loses Gmail-specific metadata (labels, thread IDs) from the
  on-disk file body. We work around this by recording it in the
  manifest's `metadata` blob instead, which keeps it queryable via
  fsearch's `source_metadata` field without polluting the .eml.

### Option C — mbox file [REJECTED]

One mbox file per label, append-only. Older email tools love mbox.

**Why rejected**:
- Mbox concatenates messages, making per-message file operations
  awkward (every dedup, every retry, every selective re-index would
  have to parse the mbox).
- Doesn't fit fsearch's "one Solr doc per file on disk" mental model.
- Easier to share `.eml` files between tools than to convince any
  modern tool to parse mbox correctly (escape rules differ between
  mboxo, mboxrd, mboxcl, mboxcl2 — historical quagmire).

## Path layout

```
<output_root>/
  <year>/
    <month>/
      <yyyy-mm-dd>_<short-msgid>.eml
```

Date prefix sorts naturally and groups messages by sent month. Short
message ID suffix prevents collisions when many messages share a
date (e.g., a busy day's mailing list). Folder/label info goes in the
manifest, NOT in the path — labels can change, paths shouldn't.

**Alternatives considered**:
- Path-by-label (`Inbox/...`, `Sent/...`) — labels can move; rebuilds
  on label change would create stale paths. Rejected.
- Flat directory with hash filenames — works but loses the natural
  chronological browsability that makes the path layout useful for
  manual inspection.

## Incremental state

**Two state artifacts**, separated by lifetime:

### `<output_root>/.gmail_state.json`

Carries the Gmail history cursor. Rewritten atomically at the end of
every run via `.tmp` + rename.

```json
{
  "history_id": "1234567",
  "last_sync_at": "2026-04-11T08:30:00Z",
  "first_sync_completed": true
}
```

- `history_id`: cursor for the next `users.history.list` call. Empty
  on first run.
- `last_sync_at`: human reference, not used by the script.
- `first_sync_completed`: distinguishes "no cursor → never run" from
  "no cursor → cursor expired, full re-sync needed".

### `<output_root>/.gmail_state.sqlite` (Phase 5.1)

Append-mostly ledger of "what messages we've already fetched". One row
per message:

```sql
CREATE TABLE fetched (
    msg_id       TEXT PRIMARY KEY,   -- Gmail message ID
    relpath      TEXT NOT NULL,      -- .eml path relative to output root
    internal_ms  INTEGER,            -- Gmail internalDate in ms (debug only)
    fetched_at   TEXT                -- ISO8601 UTC, or "migrated" for
                                     -- rows backfilled from an existing tree
);
```

**Why sqlite instead of JSON**: the two state artifacts have opposite
lifecycles. The cursor is rewritten whole every run; JSON's atomic-write
pattern is perfect. The fetched ledger is append-mostly and grows
monotonically; at 26k+ rows a JSON rewrite on every fetch is
painful. Same reasoning as the Outlook COM exporter's state DB.

**Why separate from the cursor JSON**: different semantics. Cursor
loss doesn't invalidate the fetched ledger; losing the ledger shouldn't
reset the cursor. Keeping them independent means each recovery scenario
works cleanly.

### Skip-if-known check

`_fetch_and_save()` consults the state DB before making an API call:

```python
if _is_known(state_conn, msg_id):
    return "skipped"
```

For a full-mailbox re-list after cursor loss, this turns a ~4-hour
re-fetch into a ~40-second no-op — the list phase (52 API calls
paging through message IDs) is the only thing that actually talks
to Google. Empirically validated during Phase 5.1 bring-up with the
author's 26k-message mailbox.

### Migration helper

On first invocation after upgrading from Phase 5 to 5.1, the state DB
is absent but the `.eml` tree exists. `_migrate_existing_tree()`
walks the tree, parses each filename for its embedded Gmail msg_id
(regex: `<YYYY-MM-DD>_<16-char-msgid>.eml`), and backfills the DB
with `fetched_at = "migrated"`.

Gmail message IDs are 16-hex-char strings across current API output,
so the filename suffix is losslessly round-trippable to the full
msg_id. If a future Gmail API version changes ID format and produces
longer IDs, the filename truncation would lose precision; the
migration would still work (backfills the truncated prefix), but
new fetches would write to a different filename than the migrated
entry claims. Very low probability; documented here as a known
edge case.

Idempotent: uses `INSERT OR IGNORE`, so re-running the migration
(e.g., if the DB was deleted) doesn't duplicate rows.

## Mirror vs archive mode (Phase 5.1)

Gmail history events include both message additions and deletions.
The script has to decide what to do when it sees a `messagesDeleted`
event for something it has locally.

### Option A — Mirror mode (opt-in)

Honor the delete: remove the state DB row, delete the `.eml` file
from disk, remove the manifest entry. The next `fs_indexer.py` run's
purge pass notices the file is gone and removes the Solr doc.

Behavior matches what a naive user expects: "I deleted this email
from Gmail, so my search shouldn't find it anymore."

### Option B — Archive mode (default)

Log the delete event, DO NOT remove the local file. The `.eml`
stays on disk, the state DB row stays, the manifest entry stays,
the Solr doc stays. A future `--list-orphaned` or similar tool
could surface what Gmail has forgotten but we still remember, if
that's ever useful.

Behavior: "my search archive accumulates over time; Gmail deletes
don't erase history."

### Why archive is the default

**The causal-chain argument**: once a message has been fetched
into fsearch, there are three separate states to consider —

1. The message exists on Gmail's servers
2. There's an `.eml` file on our disk
3. There's a Solr doc pointing at that file

In archive mode, state 2 is authoritative for state 3 — Solr reflects
what's on disk, period. Gmail state (1) only affects future fetches,
not existing data. This is a clean, one-way data flow.

In mirror mode, state 1 also affects state 2, which indirectly
affects state 3 via the purge pass. Two arrows of causality, both
pointing at the same local state. This is fine when everything works,
but failure modes are worse:

- **Accidental Gmail delete** (clicked the wrong thing, or an abusive
  process got access to your Google account): the local archive
  erases itself to match. In archive mode you'd notice the problem
  and still have the local copy to recover from.
- **Gmail service glitch** returning a false `messagesDeleted` event:
  same outcome — local data destroyed to match a transient upstream
  state.
- **Attacker with account access**: a malicious login can erase
  local search archives by issuing Gmail deletes. Archive mode
  closes this escalation path.

The asymmetry matters: a stale-but-present local archive is an
inconvenience. A missing local archive when you needed it is an
incident. Optimize for the worse failure mode.

**Users who want mirror mode** are technical enough to know they
want it. They can opt in by setting
`FSEARCH_GMAIL_MIRROR=true` in their cron environment or shell.
The sync logs the mode on every run:

```
Mirror mode enabled (FSEARCH_GMAIL_MIRROR) — Gmail deletes will prune local files
```

**Users who haven't thought about it** get the safe default with no
surprises. If they notice deleted Gmail messages still showing up
in fsearch and want the other behavior, a one-line env var change
fixes it.

### Cursor-loss interaction

When the history cursor expires (>7 days since last sync) and the
script falls back to a full re-list, **delete events are not
available** — `users.history.list` returns only what happened after
the given cursor, and we've lost ours. In that case, the script
clears the `deleted_ids` list and skips the delete-processing pass
entirely. Mirror-mode users will see the warning message
"can't trust history deletes after cursor loss" in the logs and
know that one sync window of deletes was silently dropped. Next
run with a fresh cursor resumes normal delete handling.

## What this source does NOT do

- **Send / modify / delete**: read-only scope, by design.
- **Attachments as separate files**: kept inside the .eml (Tika
  handles them at index time, same as PST source).
- **Drafts and chat / Hangouts data**: filtered out by default
  (`labelIds` excludes `DRAFT`, `CHAT`).
- **Multiple Gmail accounts**: one credential file → one mailbox.
  Multi-account would need either parallel source entries with
  different env vars, or a small loop in the script. Out of scope
  for the initial implementation; the architecture supports it
  trivially via FSEARCH_GMAIL_CREDENTIALS pointing at different
  files.
- **Label sync**: labels are recorded in the manifest at message-fetch
  time, but if a message gets re-labeled later, that change isn't
  reflected until the next history sync OR a forced re-fetch. Live
  with the staleness.

## Failure modes and observability

- **Credential file missing**: hard fail with instructions for the
  one-time setup
- **Refresh token revoked / expired**: hard fail with re-auth
  instructions (delete the token file and re-run)
- **Cursor expired** (HTTP 404 from history endpoint): log warning,
  reset state, fall through to a full re-list on next run
- **Per-message fetch error** (e.g., 403 on a quarantined message):
  log debug, skip that message, continue
- **Network / quota errors**: exponential backoff, then fail the run
  with a clear "try again later" message — the source's `on_failure`
  policy in sources.yaml decides whether to abort or skip

## Setup checklist (one-time, manual)

1. Create a project at `https://console.cloud.google.com/`
2. Enable the Gmail API (`APIs & Services → Enable APIs`)
3. Configure the OAuth consent screen:
   - User type: External (or Internal if Workspace admin)
   - Test users: add your Gmail address
   - Scopes: `gmail.readonly`
4. Create credentials → OAuth client ID:
   - Application type: Desktop app
   - Download the JSON file
5. Save it as `~/.config/fsearch/gmail_credentials.json`, mode 600
6. Run `sources/gmail/sync.py --auth` once — this will print a URL,
   you visit it in a browser, click "Allow", then paste the
   authorization code back into the terminal. The script saves a
   refresh token alongside the credentials file.
7. From then on, the cron job runs headlessly. Refresh tokens last
   indefinitely as long as the script runs at least every ~6 months.

## When to throw this away

- Need real-time sync (push notifications) → use Gmail API watch +
  Pub/Sub instead of cron polling. Significant complexity bump but
  enables sub-minute latency.
- Need IMAP for non-Gmail provider → fork this into a separate
  `imap` source. Most of the manifest-generation logic is reusable.
- Need labeled folder mirroring on disk → restructure the path
  layout, but you'll regret it the first time labels move.

# Wiring up Gmail

A walkthrough for getting a Gmail mailbox into fsearch. This includes
the one-time GCP OAuth setup, the interactive authorization dance,
and the cron wiring for subsequent headless runs.

!!! warning "One-time human setup required"
    Unlike the PST source, Gmail requires a one-time browser-based
    OAuth consent step before it can run headlessly. Plan for ~10
    minutes of GCP console clicking before the extractor works.

## What you'll end up with

- A nightly job that pulls new Gmail messages into per-message `.eml`
  files under `/mnt/wd1/sources/gmail/`
- A `.manifest.json` sidecar with Gmail-specific metadata: labels,
  thread ID, internal date, sender, subject
- Solr docs tagged `source_name=gmail, source_kind=imap`
- True incremental sync via the Gmail history cursor — daily runs
  typically fetch a handful of messages in seconds

## Prerequisites

- A Google account (personal Gmail OR Workspace)
- Python libraries: `google-api-python-client`, `google-auth-oauthlib`.
  Installed by `install.sh`; verify with:
  ```bash
  python3 -c "from googleapiclient.discovery import build; print('ok')"
  ```
- A browser on the machine where you run `--auth` for the first time
  (Windows side is fine — WSL's `wslview` hands URLs off to Windows)

## Step 1 — Create a GCP project

1. Visit [console.cloud.google.com](https://console.cloud.google.com/)
2. Click the project selector (top left) → **New Project**
3. Name it something like "fsearch-gmail", click Create
4. Wait ~10 seconds for provisioning

## Step 2 — Enable the Gmail API

1. In the left-side menu: **APIs & Services → Enable APIs and services**
2. Search for "Gmail API"
3. Click it, then click **Enable**

## Step 3 — Configure the OAuth consent screen

1. Left menu: **APIs & Services → OAuth consent screen**
2. User type: **External** (even for a personal account — "Internal"
   requires Workspace)
3. Click Create
4. App name: "fsearch" (shown once, on the consent screen)
5. User support email: your email
6. Developer contact: your email
7. Click Save and Continue
8. Scopes page: click **Add or Remove Scopes**, search for
   `gmail.readonly`, check the box, Update, Save and Continue
9. Test users: click **Add Users**, add your Gmail address
10. Save and Continue → Back to Dashboard

!!! note "Why 'Test users'"
    Personal-tier OAuth apps stay in "testing" mode unless you go
    through Google's app verification, which we don't need. Test
    users can authorize the app normally; you're just explicitly
    telling Google "this Gmail address is allowed to use this app".

## Step 4 — Create OAuth credentials

1. Left menu: **APIs & Services → Credentials**
2. **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name: "fsearch-gmail-desktop"
5. Click Create
6. Download the JSON (click the download icon on the client list)

## Step 5 — Save the credentials file

```bash
mkdir -p ~/.config/fsearch
chmod 700 ~/.config/fsearch
mv ~/Downloads/client_secret_*.json ~/.config/fsearch/gmail_credentials.json
chmod 600 ~/.config/fsearch/gmail_credentials.json
```

The filename matches the default the script looks for. Override with
`FSEARCH_GMAIL_CREDENTIALS` if you want it elsewhere.

## Step 6 — Create the output directory

```bash
sudo mkdir -p /mnt/wd1/sources/gmail
sudo chown "$USER:$USER" /mnt/wd1/sources/gmail
```

## Step 7 — Run the interactive authorization

```bash
FSEARCH_GMAIL_OUTPUT=/mnt/wd1/sources/gmail \
/opt/fsearch/sources/gmail/sync.py --auth
```

The script will:

1. Start a tiny local web server on a random port
2. Open your browser to a Google OAuth URL
3. You click **Allow** on the consent screen (Google warns "unverified
   app" — expected; click "Advanced → Go to fsearch")
4. Google redirects back to the local server with an auth code
5. The script exchanges the code for a refresh token and saves it to
   `~/.config/fsearch/gmail_token.json` (mode 600)
6. Prints "Authorization complete; token saved"

If no browser is available, the script falls back to a console flow
where it prints a URL, you visit it on any other machine, and paste
the resulting code back.

## Step 8 — First real sync (foreground, to see progress)

```bash
FSEARCH_GMAIL_OUTPUT=/mnt/wd1/sources/gmail \
/opt/fsearch/sources/gmail/sync.py
```

The first run has no history cursor, so it falls back to a full
message list. Expect:

```
14:32:11 INFO First sync — listing all messages
14:32:13 INFO   listed 5000 messages so far...
14:32:19 INFO   listed 10000 messages so far...
...
14:33:02 INFO Fetching 18543 message(s)
14:33:18 INFO   fetched 100/18543
14:33:34 INFO   fetched 200/18543
...
14:47:12 INFO Wrote manifest: /mnt/wd1/sources/gmail/.manifest.json (18543 entries)
14:47:12 INFO Done: ok=18543 failed=0 total_in_manifest=18543
```

Large mailboxes can take a while. Safe to Ctrl-C — the state file
tracks progress so the next run picks up where this one stopped.

## Step 9 — Add the source to `sources.yaml`

```yaml
sources:
  - name: gmail
    kind: imap
    root: /mnt/wd1/sources/gmail
    hook:
      command: /opt/fsearch/sources/gmail/sync.py
      timeout: 1800
      lockfile: /mnt/wd1/sources/gmail/.lock
      on_failure: skip
```

## Step 10 — Set env vars in the cron wrapper

Edit `/opt/fsearch/run_index.sh`:

```bash
export FSEARCH_GMAIL_OUTPUT="/mnt/wd1/sources/gmail"
```

`FSEARCH_GMAIL_CREDENTIALS` and `FSEARCH_GMAIL_TOKEN` use sensible
defaults under `~/.config/fsearch/` so they don't need to be exported
unless you're using a non-standard location.

## Step 11 — Run through the indexer end-to-end

```bash
FSEARCH_GMAIL_OUTPUT=/mnt/wd1/sources/gmail \
fs_indexer.py --source gmail
```

You should see:

1. The hook runs → Gmail sync → a small number of new messages
   fetched
2. `Loaded manifest from .../gmail/.manifest.json (N entries)`
3. The indexer walks the `/mnt/wd1/sources/gmail` tree, picks up
   any new `.eml` files, and commits them to Solr with `source_name=gmail`
4. Incremental — already-indexed messages are skipped via the
   `_file_unchanged` check

## Verify in Solr

```bash
# Count Gmail-sourced docs
curl -s 'http://localhost:8983/solr/filesystem/select?q=source_name:gmail&rows=0' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'])"

# Inspect one doc with all its Gmail metadata
fsearch -q 'source_kind:imap' -l 1 --json | python3 -m json.tool
```

## Searching Gmail content

```bash
# All Gmail messages about a topic
fsearch -q 'source_name:gmail AND content:"docker compose"'

# By Gmail label (labels are in the JSON metadata blob)
fsearch -q 'source_name:gmail AND source_metadata:"INBOX"'

# Newest Gmail messages first
fsearch -q 'source_name:gmail' --sort 'source_timestamp desc' -l 20

# Unified search across Gmail AND PST archives
fsearch -q '(source_name:gmail OR source_kind:pst) AND content:project'
```

## Ongoing maintenance

- **Refresh tokens last indefinitely** as long as the script runs at
  least once every ~6 months and you don't revoke access in the
  Google account security settings. Daily cron keeps them alive
  forever.
- **History cursor expiration**: if the script hasn't run for a week
  or more, the cursor expires and Google's history endpoint returns
  404. The script detects this, logs a warning, and falls back to a
  full re-list on the next run. No manual intervention needed.
- **Adding / removing labels in Gmail** is NOT reflected in fsearch
  until the message is re-fetched. Gmail's history only reports
  `messagesAdded`; label changes are intentionally ignored for
  simplicity. If you need label accuracy, run `sync.py --full`
  (not implemented yet — would need a small flag addition) or
  delete the state file to trigger a full re-sync.

## Troubleshooting

**"Google API libraries missing"**  
Install them: `pip install google-api-python-client google-auth-oauthlib --break-system-packages`.
`install.sh` normally does this.

**"No valid token at ~/.config/fsearch/gmail_token.json. Run with --auth"**  
First-time setup not done, or the token file was deleted. Re-run
with `--auth`.

**"invalid_grant" error at refresh time**  
Token was revoked (either by you in Google account settings or by
Google's automatic cleanup). Delete `~/.config/fsearch/gmail_token.json`
and re-run `--auth`.

**"accessNotConfigured" error**  
Gmail API isn't enabled for your GCP project. Back to Step 2.

**"app not verified" warning blocks consent**  
You forgot to add your Gmail address as a test user in Step 3, or
you're trying to use a different Gmail address from the one you
whitelisted. Add it via the OAuth consent screen config.

**Sync runs but 0 messages returned every time**  
The history cursor is stuck at a future point (very rare, happens
only if the state file got corrupted). Delete
`/mnt/wd1/sources/gmail/.gmail_state.json` and the next run will
do a full re-sync.

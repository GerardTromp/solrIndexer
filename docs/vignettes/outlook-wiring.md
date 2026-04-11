# Wiring up Outlook desktop (COM)

A walkthrough for exporting messages from a live Outlook desktop
client on Windows and feeding them into fsearch. This is the
"push source" pattern — the export runs on Windows, fsearch just
sees files appear.

!!! info "Two-sided setup"
    Unlike PST and Gmail, this source spans two operating systems.
    The **exporter** runs on Windows (pywin32 + Outlook COM). The
    **indexer** runs on WSL and consumes whatever the Windows side
    drops in a shared directory.

## What you'll end up with

- A Task Scheduler job on Windows that runs whenever you log in,
  exporting new Outlook messages as `.msg` files into a directory
  visible to WSL (e.g., `C:\Users\gerard\OutlookExport`)
- A `.manifest.json` with Outlook-specific metadata: store name,
  folder path, categories, EntryID, attachment flag
- An fsearch push source pointing at the same directory from the
  WSL side
- Searches that work across PST archives, Gmail, AND live Outlook
  mail in one unified index

## When to use this vs. PST wiring

| Use PST wiring if… | Use Outlook COM wiring if… |
|---|---|
| You have archived `.pst` files on disk | You have a live M365 / Exchange mailbox with the Graph API locked down |
| Archives don't change often | You want regular updates of recent mail |
| Outlook doesn't need to be running | Outlook is open on your desktop daily anyway |
| You want one-shot extraction | You want incremental sync |

They're not mutually exclusive — you can run both as separate sources,
pointing at different output roots. PST for historical archives,
Outlook COM for the live mailbox.

## Prerequisites

- Windows with Outlook desktop installed (2016 or newer)
- Python 3.10+ on Windows (from python.org or Microsoft Store)
- WSL side of fsearch already installed and working
- Write access to a directory reachable from both sides (anything
  under `C:\Users\<you>\` is visible as `/mnt/c/Users/<you>/` in WSL)

## Step 1 — Get the exporter

The Outlook exporter lives in a **separate repo** from solrIndexer
because it's Windows-native and has a different lifecycle. Check
out the companion project:

```powershell
cd C:\Users\<you>\
git clone https://github.com/<you>/outlook_com_export.git
cd outlook_com_export
```

Or grab it from the local path if you're working from the
development tree:

```powershell
robocopy \\wsl$\Ubuntu\mnt\d\GT\GitHub\outlook_com_export `
         C:\Users\<you>\outlook_com_export /E
cd C:\Users\<you>\outlook_com_export
```

## Step 2 — Install Windows dependencies

```powershell
pip install pywin32 pyyaml
```

## Step 3 — Create the output directory

```powershell
mkdir C:\Users\<you>\OutlookExport
```

This directory will hold the exported `.msg` files and the
`.manifest.json`. Pick something under your user profile so WSL
can see it via `/mnt/c/Users/<you>/OutlookExport`.

## Step 4 — Configure the exporter

```powershell
cd C:\Users\<you>\outlook_com_export
copy config.example.yaml config.yaml
notepad config.yaml
```

Edit at minimum the `output_root`:

```yaml
output_root: C:\Users\<you>\OutlookExport

# Folder-name exclusions (case-insensitive). Default excludes
# Drafts, Deleted Items, Junk Email, etc. Add your own:
exclude_folders:
  - Drafts
  - Deleted Items
  - Junk Email
  - Outbox
  - RSS Feeds
  - Some Huge Folder You Don't Want Indexed

# Cap per run — useful for throttling the first full export
max_messages_per_run: 0    # 0 = unlimited
```

See `config.example.yaml` for all available options.

## Step 5 — Dry run to verify COM reach

```powershell
python export.py --dry-run
```

You should see a list of folders and item counts:

```
14:05:21 INFO Connected to store: Outlook
14:05:21 INFO [Outlook/Inbox] 4213 items
14:05:23 INFO [Outlook/Inbox/Projects] 892 items
14:05:23 INFO [Outlook/Sent Items] 1756 items
...
14:05:45 INFO Done: folders=23 new=0 already_exported=0 failed=0 (dry-run)
```

If this fails, Outlook COM isn't reachable. See
"Troubleshooting" at the bottom.

## Step 6 — First real export

```powershell
python export.py
```

This walks every in-scope folder and exports every message that
isn't already in the SQLite state DB. The first run will take a
while (Outlook COM is one-item-at-a-time, typically ~10-50
items/second depending on message size).

Use `max_messages_per_run: 5000` in `config.yaml` if you want to
spread the initial export across several sessions.

## Step 7 — Verify the output

```powershell
dir C:\Users\<you>\OutlookExport
# Outlook\
# .manifest.json
# .export_state.sqlite

dir C:\Users\<you>\OutlookExport\Outlook\Inbox
# 2026-04-11_abc123.msg  2026-04-10_def456.msg  ...

type C:\Users\<you>\OutlookExport\.manifest.json
# {"version": 1, "source_name": "outlook-com", ...}
```

## Step 8 — Set up Task Scheduler

Create a scheduled task so the exporter runs automatically. From a
PowerShell (admin) prompt:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "C:\Users\<you>\outlook_com_export\export.py" `
    -WorkingDirectory "C:\Users\<you>\outlook_com_export"

$trigger = New-ScheduledTaskTrigger `
    -AtLogon `
    -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName "Outlook COM export" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Exports Outlook messages to .msg files for fsearch"
```

Or use the GUI: Task Scheduler → Create Task → General tab: "Run
only when user is logged on"; Triggers → New → "At log on" for your
user; Actions → New → `python` with argument to `export.py`.

!!! warning "Do NOT run as admin"
    Outlook COM expects to run in the same security context as the
    Outlook desktop session. Running as admin (elevated) fails because
    Outlook is usually running as your normal user. Keep the task at
    "Run only when user is logged on" with no "Run with highest
    privileges".

## Step 9 — Wire into fsearch on the WSL side

Edit `/opt/fsearch/sources.yaml`:

```yaml
sources:
  - name: outlook-com
    kind: msg
    root: /mnt/c/Users/<you>/OutlookExport
    # No hook — this is a push source; the Windows side writes here
    # on its own schedule
```

No lockfile, no timeout, no env vars — fsearch just sees the files
on the next walk.

## Step 10 — First indexer run

```bash
fs_indexer.py --source outlook-com
```

Expected:

1. `=== Source: outlook-com (msg) → /mnt/c/Users/.../OutlookExport ===`
2. No hook runs (push source)
3. `Loaded manifest from .../OutlookExport/.manifest.json (N entries)`
4. Tika extracts each `.msg` and Solr docs land tagged
   `source_name=outlook-com`, `source_kind=msg`

## Verify in Solr

```bash
curl -s 'http://localhost:8983/solr/filesystem/select?q=source_name:outlook-com&rows=0' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'])"

fsearch -q 'source_name:outlook-com' --sort 'source_timestamp desc' -l 5
```

## Searching live Outlook content

```bash
# Everything about a project, across all email sources
fsearch -q '(source_kind:msg OR source_kind:pst OR source_kind:imap) AND content:project'

# Outlook messages from a specific category
fsearch -q 'source_name:outlook-com AND source_metadata:"Follow-up"'

# Recent Outlook mail only
fsearch -q 'source_name:outlook-com AND source_timestamp:[NOW-30DAYS TO *]'
```

## Ongoing maintenance

- **Incremental**: the exporter tracks every EntryID it has seen in
  a SQLite DB. Subsequent runs skip already-exported messages, so a
  daily run typically exports 20-200 new items in seconds.
- **Deletes**: when you delete a message in Outlook and empty Deleted
  Items, its `.msg` file stays on disk. fsearch keeps indexing it
  until you manually clean up. This is intentional — treating
  Outlook deletes as authoritative would let a bad delete erase
  your archive.
- **Outlook upgrade breaks COM**: occasionally a Windows update
  changes the Outlook COM surface. Usually `pip install --upgrade
  pywin32` fixes it. If not, regenerate the COM stubs with
  `python -m win32com.client.makepy "Microsoft Outlook 16.0 Object Library"`.

## Troubleshooting

**"Could not start/attach to Outlook"**  
Outlook isn't running and can't be launched automatically. Open
Outlook manually, then re-run the exporter.

**CoInitialize errors from Task Scheduler**  
The task is running in a thread context COM doesn't like. Make sure
the task runs with "Run only when user is logged on" and NOT
"Whether user is logged on or not". Background sessions don't have
a desktop to attach COM to.

**Antivirus / EDR blocks the script**  
Security tools sometimes flag COM-automation scripts as suspicious.
Whitelist `python.exe` (or your specific Python launcher) for
`Outlook.Application` COM access in your AV settings.

**`.msg` files land but are empty or truncated**  
Outlook is in the middle of saving while you're reading. Add a
small retry loop to the exporter, or just wait — the next run will
re-fetch any message whose EntryID isn't in the state DB yet.

**Task runs but produces 0 files**  
Check the per-folder item counts — every folder might be excluded
by your `exclude_folders` list. Run once with `--dry-run` and
inspect the output.

**Path length errors on deep folder hierarchies**  
Windows has a 260-character path limit by default. Enable long paths
via Group Policy or the registry key
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled
= 1`, then reboot.

## Running both PST and Outlook COM in parallel

Nothing stops you. They have different `source_name` values and
different `root` directories:

```yaml
sources:
  - name: pst-archive      # historical
    kind: pst
    root: /mnt/wd1/sources/pst
    hook:
      command: /opt/fsearch/sources/pst/extract.py
      ...

  - name: outlook-com      # live
    kind: msg
    root: /mnt/c/Users/<you>/OutlookExport
```

Searches can mix and match:

```bash
# Everything from "alice@example.com" across archives and live mail
fsearch -q 'source_metadata:"alice@example.com"'

# Historical mail only
fsearch -q 'source_kind:pst AND content:migration'
```

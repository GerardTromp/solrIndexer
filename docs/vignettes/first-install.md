# First install

A first-time setup on WSL2 Ubuntu. If you're migrating an existing
installation, skip to the "Post-install: first index run" section at
the bottom.

## Prerequisites

- WSL2 Ubuntu (any recent LTS)
- A data disk mounted at `/mnt/wd1` (or edit `install.sh` to point
  elsewhere before running)
- ~20GB of free space for Solr index + logs + Tika cache
- Sudo access

## Run the installer

```bash
cd /path/to/solrIndexer
./install.sh
```

This will:

1. `apt install` Java 21, `curl`, `python3-pip`, `pst-utils`
2. `pip install` pysolr, requests, click, rich, tika, pyyaml,
   psutil, google-api-python-client, google-auth-oauthlib
3. Download and unpack Solr 10 to `~/opt/solr`
4. Download Tika 3 jar to `~/opt/tika-server.jar`
5. Write Solr data-home config to `~/opt/solr/bin/solr.in.sh`
6. Copy Python scripts to `/opt/fsearch/`
7. Copy `sources/` subdirectory (PST, Gmail extractors)
8. Drop `sources.yaml.example` (never overwrites a live
   `sources.yaml`)
9. Symlink `/usr/local/bin/fsearch → /opt/fsearch/fsearch.py`
10. Append Solr/Tika auto-start to `~/.bashrc`
11. Install a daily 2am cron entry for `run_index.sh`
12. Start Solr, create the `filesystem` core, post the schema

After it finishes, **open a new shell** (or `source ~/.bashrc`) so
`SOLR_URL` and the `solr-*` aliases become available.

## Verify the basics

```bash
# Solr is up
solr-status

# Count docs (should be 0 for a fresh install)
solr-count

# Tika is up
curl -s http://localhost:9998/version
```

## Create your first sources.yaml

```bash
sudo cp /opt/fsearch/sources.yaml.example /opt/fsearch/sources.yaml
sudo vi /opt/fsearch/sources.yaml
```

Edit to point at directories you actually want indexed. A minimal
starting point:

```yaml
sources:
  - name: home
    kind: fs
    root: /home/gerard
    excludes:
      - node_modules
      - .git
      - .venv
```

Validate that `fs_indexer.py` parses your config:

```bash
fs_indexer.py --list-sources
```

You should see each source printed with its name, kind, hook status,
and root.

## Post-install: first index run

Incremental mode is the default, but on a fresh install there's
nothing to be incremental against. Do an explicit first pass:

```bash
fs_indexer.py
```

The first run for each source walks every file in its root, hands
each one to Tika, and commits docs in batches of 300. Expect:

- A few minutes for a small source (< 10k files)
- Tens of minutes to a few hours for a source covering a whole
  home directory
- Progress is visible on stderr as each batch commits
- Safe to Ctrl-C — the next run picks up where this one left off
  thanks to the find cache and checkpoint

Watch the logs:

```bash
tail -f /mnt/wd1/solr/logs/indexer.log
```

## Start the web GUI

```bash
nohup /opt/fsearch/fsearch_web.py --port 8080 >> /mnt/wd1/solr/logs/web.log 2>&1 &
```

Visit `http://127.0.0.1:8080/` in a browser (Windows-side works —
WSL forwards localhost). Hit `?` to see the field reference
tooltip, `Ctrl+Enter` to search.

## First search to prove it works

From the CLI:

```bash
fsearch --ext py --limit 5
fsearch "some keyword from a file you know exists"
```

If you see results, you're done.

## What the cron job does

`/etc/cron.d` has an entry calling `/opt/fsearch/run_index.sh` at
2am daily. That script:

1. Waits for `/mnt/wd1` to be mounted (WSL disks can lag at boot)
2. Starts Tika if not running
3. Runs `fs_indexer.py` (no args — reads `sources.yaml`)
4. Logs everything to `/mnt/wd1/solr/logs/indexer.log`

If you want to change the schedule, edit the cron entry directly.

## Next steps

Now that the core works, pick a source kind to add:

- [Wiring up PST archives](pst-wiring.md)
- [Wiring up Gmail](gmail-wiring.md)
- [Wiring up Outlook desktop (COM)](outlook-wiring.md)

Or read [Export & duplicate detection](export-and-dedup.md) to learn
about the dedup and export features.

## Troubleshooting

**`solr-status` says "not running"**  
WSL disk may not be mounted. Check `mountpoint -q /mnt/wd1 && echo ok`.
If not mounted, mount it and run `solr-start`.

**`fsearch` command not found**  
Symlink to `/usr/local/bin/fsearch` is missing. Re-run:
`sudo ln -sf /opt/fsearch/fsearch.py /usr/local/bin/fsearch`

**Tika failures in the log**  
Normal for a handful of files (corrupt PDFs, unsupported archives).
fsearch auto-restarts Tika after 5 consecutive failures and tracks
known-bad files in `~/.solr/skip_content.tsv` so it stops retrying
them. If the whole indexer grinds to a halt, check
`/mnt/wd1/solr/logs/tika.log` for the Java traceback.

**`fs_indexer.py --status` shows a stale PID**  
Someone (probably a previous cron run) crashed without releasing the
lock. Delete `/mnt/wd1/solr/indexer.lock` manually. The next run
will work.

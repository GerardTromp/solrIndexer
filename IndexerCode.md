I want to set up a SOLR indexer for my local files system. Concept is to provide rapid search preferably via a command line interface utility to find files by name or keyword (content) or phrase where the query is essentially a regex or Boolean combination of regexes, including size and date. 
CLI tool can be a python or bash script that interfaces with the SOLR backend

Mar 24

Check available skills

Check available skills

Here's a comprehensive design and implementation plan for a Solr-backed filesystem search system.

---

## Architecture Overview

```
Filesystem
    │
    ▼
[Indexer daemon / one-shot crawler]
    │  inotifywait (live) or find (batch)
    ▼
[Document extractor]  ←── Apache Tika (content extraction)
    │  metadata + full text
    ▼
[Solr 9.x]  ←── schema: path, filename, ext, size, mtime, content, mimetype
    │
    ▼
[CLI tool: fsearch]  ←── Python, argparse, rich output
```

---

## 1. Solr Setup

### Install & start Solr (Docker is cleanest)

bash

```bash
docker run -d --name solr-fs \
  -p 8983:8983 \
  -v $HOME/solr-data:/var/solr \
  solr:9

# Create core
docker exec -it solr-fs solr create_core -c filesystem
```

Or binary install:

bash

```bash
wget https://downloads.apache.org/solr/solr/9.7.0/solr-9.7.0.tgz
tar xzf solr-9.7.0.tgz
./solr-9.7.0/bin/solr start
./solr-9.7.0/bin/solr create -c filesystem
```

---

## 2. Schema Definition

Post this to `http://localhost:8983/solr/filesystem/schema` — use managed schema (Schema API):

bash

```bash
curl -X POST http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{
  "add-field": [
    {"name": "filepath",   "type": "string",    "stored": true, "indexed": true},
    {"name": "filename",   "type": "text_general","stored": true, "indexed": true},
    {"name": "extension",  "type": "string",    "stored": true, "indexed": true},
    {"name": "directory",  "type": "string",    "stored": true, "indexed": true},
    {"name": "size_bytes", "type": "plong",     "stored": true, "indexed": true},
    {"name": "mtime",      "type": "pdate",     "stored": true, "indexed": true},
    {"name": "ctime",      "type": "pdate",     "stored": true, "indexed": true},
    {"name": "mimetype",   "type": "string",    "stored": true, "indexed": true},
    {"name": "content",    "type": "text_general","stored": true, "indexed": true},
    {"name": "owner",      "type": "string",    "stored": true, "indexed": true},
    {"name": "permissions","type": "string",    "stored": true, "indexed": false}
  ],
  "add-copy-field": [
    {"source": "filename", "dest": "_text_"},
    {"source": "content",  "dest": "_text_"}
  ]
}'
```

Key design choices:

- `plong`/`pdate` are Solr's numeric/range-query types (point fields)
- `_text_` catchall field enables single-field cross-field queries
- `filepath` is the document `id` — set `uniqueKey` to it

Set `filepath` as the unique key by modifying `managed-schema` or posting:

bash

```bash
# In solrconfig, id field defaults to "id" — easiest to just use id=filepath value
```

---

## 3. Indexer Script

**`fs_indexer.py`** — handles both batch crawl and inotify watch mode:

python

```python
#!/usr/bin/env python3
"""
fs_indexer.py  — Filesystem → Solr indexer
Deps: pip install pysolr tika inotify-simple click rich
Tika server must be running: java -jar tika-server.jar
"""

import os
import sys
import stat
import hashlib
import mimetypes
import datetime
import time
import logging
from pathlib import Path
from typing import Generator

import click
import pysolr
from tika import parser as tika_parser
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.logging import RichHandler

logging.basicConfig(handlers=[RichHandler()], level=logging.INFO)
log = logging.getLogger("indexer")

SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr/filesystem")
BATCH_SIZE = 500

# Extensions where content extraction is worthwhile
TEXT_EXTS = {
    ".txt", ".md", ".rst", ".py", ".r", ".R", ".sh", ".bash",
    ".c", ".cpp", ".h", ".java", ".js", ".ts", ".go", ".rs",
    ".yaml", ".yml", ".toml", ".json", ".xml", ".csv", ".tsv",
    ".html", ".htm", ".css", ".sql", ".nf", ".snakefile",
    ".fasta", ".fa", ".fastq", ".fq", ".vcf", ".gff", ".gtf", ".bed",
    ".log", ".conf", ".cfg", ".ini"
}
TIKA_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".epub"}
MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5MB cap for content extraction


def ts_to_solr(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_content(path: Path, ext: str) -> str:
    """Extract text content; returns empty string on failure."""
    try:
        if ext in TEXT_EXTS:
            size = path.stat().st_size
            if size > MAX_CONTENT_BYTES:
                with open(path, "rb") as f:
                    return f.read(MAX_CONTENT_BYTES).decode("utf-8", errors="replace")
            return path.read_text(errors="replace")
        elif ext in TIKA_EXTS:
            parsed = tika_parser.from_file(str(path))
            return parsed.get("content") or ""
    except Exception as e:
        log.debug(f"Content extraction failed for {path}: {e}")
    return ""


def file_to_doc(path: Path) -> dict | None:
    try:
        s = path.stat()
        ext = path.suffix.lower()
        content = extract_content(path, ext)
        mime, _ = mimetypes.guess_type(str(path))
        return {
            "id":          str(path),          # unique key
            "filepath":    str(path),
            "filename":    path.name,
            "extension":   ext.lstrip(".") if ext else "",
            "directory":   str(path.parent),
            "size_bytes":  s.st_size,
            "mtime":       ts_to_solr(s.st_mtime),
            "ctime":       ts_to_solr(s.st_ctime),
            "mimetype":    mime or "application/octet-stream",
            "content":     content,
            "owner":       str(s.st_uid),
            "permissions": oct(stat.S_IMODE(s.st_mode)),
        }
    except (PermissionError, FileNotFoundError, OSError) as e:
        log.debug(f"Skipping {path}: {e}")
        return None


def crawl(roots: list[str], exclude: list[str]) -> Generator[Path, None, None]:
    exclude_set = {Path(e).resolve() for e in exclude}
    for root in roots:
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            dp = Path(dirpath).resolve()
            # Prune excluded dirs in-place
            dirs[:] = [
                d for d in dirs
                if (dp / d).resolve() not in exclude_set
                and not d.startswith(".")  # skip hidden by default
            ]
            for fname in files:
                yield dp / fname


@click.group()
def cli():
    pass


@cli.command()
@click.argument("roots", nargs=-1, required=True)
@click.option("--exclude", "-x", multiple=True, help="Paths to exclude")
@click.option("--solr-url", default=SOLR_URL, show_default=True)
@click.option("--commit-within", default=5000, help="Solr commitWithin ms")
def index(roots, exclude, solr_url, commit_within):
    """Crawl ROOT paths and index into Solr."""
    solr = pysolr.Solr(solr_url, always_commit=False, timeout=30)
    batch = []
    total = skipped = 0

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), TextColumn("{task.fields[count]} docs")) as prog:
        task = prog.add_task("Indexing...", count=0)
        for path in crawl(list(roots), list(exclude)):
            doc = file_to_doc(path)
            if doc:
                batch.append(doc)
                total += 1
            else:
                skipped += 1

            if len(batch) >= BATCH_SIZE:
                solr.add(batch, commit=False, commitWithin=commit_within)
                batch.clear()
            prog.update(task, count=total)

    if batch:
        solr.add(batch, commit=False, commitWithin=commit_within)
    solr.commit()
    log.info(f"Done. Indexed: {total}, Skipped: {skipped}")


@cli.command()
@click.argument("roots", nargs=-1, required=True)
@click.option("--solr-url", default=SOLR_URL, show_default=True)
def watch(roots, solr_url):
    """Watch filesystem with inotify and incrementally update Solr."""
    try:
        import inotify_simple
    except ImportError:
        log.error("pip install inotify-simple")
        sys.exit(1)

    solr = pysolr.Solr(solr_url, always_commit=False, timeout=30)
    inotify = inotify_simple.INotify()
    flags = (inotify_simple.flags.CLOSE_WRITE |
             inotify_simple.flags.CREATE |
             inotify_simple.flags.DELETE |
             inotify_simple.flags.MOVED_FROM |
             inotify_simple.flags.MOVED_TO)

    wd_to_path = {}
    for root in roots:
        for dirpath, dirs, _ in os.walk(root):
            wd = inotify.add_watch(dirpath, flags)
            wd_to_path[wd] = dirpath
            dirs[:] = [d for d in dirs if not d.startswith(".")]

    log.info(f"Watching {len(wd_to_path)} directories...")
    while True:
        for event in inotify.read(timeout=1000):
            dpath = Path(wd_to_path.get(event.wd, ""))
            fpath = dpath / event.name if event.name else dpath
            mask = event.mask

            if mask & (inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO):
                doc = file_to_doc(fpath)
                if doc:
                    solr.add([doc], commitWithin=2000)
                    log.info(f"Indexed: {fpath}")
            elif mask & (inotify_simple.flags.DELETE | inotify_simple.flags.MOVED_FROM):
                solr.delete(id=str(fpath))
                solr.commit()
                log.info(f"Deleted: {fpath}")


if __name__ == "__main__":
    cli()
```

---

## 4. CLI Search Tool

**`fsearch.py`** — the query frontend:

python

```python
#!/usr/bin/env python3
"""
fsearch — Query Solr filesystem index
Usage examples:
  fsearch "BRCA1"
  fsearch --name "*.vcf" --since 2024-01-01
  fsearch --content "p.value < 0.05" --ext py,r,R
  fsearch --size ">10MB" --before 2023-06-01
  fsearch --regex "chr[0-9]+.*variant" --field content
  fsearch --query 'content:GATK AND filename:*hg38* AND size_bytes:[1000 TO *]'
"""

import os
import re
import sys
import datetime
import argparse
import shutil
from pathlib import Path

import pysolr
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box

SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr/filesystem")
console = Console()


def parse_size(s: str) -> str:
    """Convert human size filter like '>10MB' to Solr range query."""
    m = re.match(r'([><]=?)\s*(\d+\.?\d*)\s*(B|KB|MB|GB|TB)?', s, re.I)
    if not m:
        raise ValueError(f"Invalid size: {s}")
    op, val, unit = m.groups()
    mult = {"b":1,"kb":1024,"mb":1024**2,"gb":1024**3,"tb":1024**4}.get(
        (unit or "b").lower(), 1)
    bytes_val = int(float(val) * mult)
    if op == ">":   return f"size_bytes:{{{bytes_val} TO *]}"
    if op == ">=":  return f"size_bytes:[{bytes_val} TO *]"
    if op == "<":   return f"size_bytes:[* TO {bytes_val}}}}"
    if op == "<=":  return f"size_bytes:[* TO {bytes_val}]"
    return f"size_bytes:{bytes_val}"


def parse_date(s: str) -> str:
    """Return Solr datetime string from YYYY-MM-DD."""
    return datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")


def glob_to_solr(pattern: str) -> str:
    """Convert shell glob to Solr wildcard query fragment."""
    # Solr supports * and ? wildcards natively
    return pattern.replace(" ", "\\ ")


def build_query(args) -> tuple[str, dict]:
    clauses = []
    params = {"fl": "filepath,filename,size_bytes,mtime,extension,directory",
              "rows": args.limit,
              "sort": args.sort}

    if args.query:
        # Raw Solr/Lucene query passthrough
        clauses.append(args.query)
    else:
        if args.text:
            # Treat as regex-ish: convert to Solr regex if slashes present, else phrase/wildcard
            t = args.text
            if t.startswith("/") and t.endswith("/"):
                inner = t[1:-1]
                clauses.append(f"_text_:/{inner}/")
            else:
                # Support Boolean operators natively
                clauses.append(f"_text_:({t})")

        if args.name:
            clauses.append(f"filename:{glob_to_solr(args.name)}")

        if args.ext:
            exts = [e.lstrip(".") for e in args.ext.split(",")]
            ext_q = " OR ".join(f"extension:{e}" for e in exts)
            clauses.append(f"({ext_q})")

        if args.dir:
            clauses.append(f'directory:"{args.dir}"')

        if args.content:
            t = args.content
            if t.startswith("/") and t.endswith("/"):
                clauses.append(f"content:/{t[1:-1]}/")
            else:
                clauses.append(f"content:({t})")

        if args.size:
            clauses.append(parse_size(args.size))

        if args.since:
            dt = parse_date(args.since)
            clauses.append(f"mtime:[{dt} TO *]")

        if args.before:
            dt = parse_date(args.before)
            clauses.append(f"mtime:[* TO {dt}]")

    q = " AND ".join(clauses) if clauses else "*:*"

    if args.highlight and not args.quiet:
        params.update({
            "hl": "true",
            "hl.fl": "content,filename",
            "hl.snippets": 2,
            "hl.fragsize": 120,
            "hl.simple.pre": ">>>",
            "hl.simple.post": "<<<",
        })

    return q, params


def fmt_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_date(s: str) -> str:
    return s[:10] if s else ""


def display_results(results, highlights=None, args=None):
    if args.quiet:
        for doc in results:
            print(doc.get("filepath", ""))
        return

    if args.jsonout:
        import json
        docs = list(results)
        print(json.dumps(docs, indent=2))
        return

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
                  expand=True)
    table.add_column("Path", style="green", no_wrap=False, ratio=5)
    table.add_column("Ext",  style="yellow", width=6)
    table.add_column("Size", style="blue", width=9, justify="right")
    table.add_column("Modified", style="white", width=11)

    for doc in results:
        fp = doc.get("filepath", "")
        ext = doc.get("extension", "")
        sz = fmt_size(doc.get("size_bytes", 0))
        mt = fmt_date(doc.get("mtime", ""))
        table.add_row(fp, ext, sz, mt)

        # Show highlight snippets if available
        if highlights and fp in highlights:
            for field, snips in highlights[fp].items():
                for snip in snips:
                    snip_clean = snip.replace(">>>", "[bold red]").replace("<<<", "[/bold red]")
                    table.add_row(f"  [dim]{snip_clean}[/dim]", "", "", "")

    console.print(table)
    console.print(f"[dim]Found {results.hits} total results (showing {min(args.limit, results.hits)})[/dim]")


def main():
    ap = argparse.ArgumentParser(
        prog="fsearch",
        description="Search your Solr-indexed filesystem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    ap.add_argument("text", nargs="?", help="Free text / regex (/pattern/) / Boolean query")
    ap.add_argument("-n", "--name",    help="Filename glob, e.g. '*.vcf'")
    ap.add_argument("-e", "--ext",     help="Comma-separated extensions: py,r,sh")
    ap.add_argument("-d", "--dir",     help="Restrict to directory prefix")
    ap.add_argument("-c", "--content", help="Search file content (supports /regex/)")
    ap.add_argument("-s", "--size",    help="Size filter: >10MB, <1GB, >=500KB")
    ap.add_argument("--since",        help="Modified after YYYY-MM-DD")
    ap.add_argument("--before",       help="Modified before YYYY-MM-DD")
    ap.add_argument("-q", "--query",  help="Raw Solr/Lucene query string (expert)")
    ap.add_argument("-l", "--limit",  type=int, default=50, help="Max results [50]")
    ap.add_argument("--sort",         default="score desc",
                    help="Sort: score desc | mtime desc | size_bytes asc | filename asc")
    ap.add_argument("--highlight",    action="store_true", default=True,
                    help="Show content snippets (default: on)")
    ap.add_argument("--no-highlight", dest="highlight", action="store_false")
    ap.add_argument("--quiet",  "-Q", action="store_true", help="Print paths only (for piping)")
    ap.add_argument("--json",         dest="jsonout", action="store_true", help="JSON output")
    ap.add_argument("--solr-url",     default=SOLR_URL, help="Solr URL")

    args = ap.parse_args()

    solr = pysolr.Solr(args.solr_url, timeout=15)
    q, params = build_query(args)

    try:
        results = solr.search(q, **params)
    except pysolr.SolrError as e:
        console.print(f"[red]Solr error:[/red] {e}")
        sys.exit(1)

    highlights = getattr(results, "highlighting", {})
    display_results(results, highlights, args)


if __name__ == "__main__":
    main()
```

---

## 5. Tika Server (for PDF/DOCX content)

bash

```bash
# Download once
wget https://downloads.apache.org/tika/3.0.0/tika-server-standard-3.0.0.jar \
  -O ~/bin/tika-server.jar

# Run as daemon
java -jar ~/bin/tika-server.jar --port 9998 &

# Or systemd unit for persistence
```

The Python `tika` library auto-discovers it at `localhost:9998`.

---

## 6. Query Examples

bash

```bash
# Full-text search
fsearch "DESeq2 results"

# Regex on content
fsearch "/p[._-]?adj\s*<\s*0\.05/"

# Boolean
fsearch "GATK AND (haplotype OR genotype) AND NOT test"

# File metadata combos
fsearch --name "*.fastq.gz" --size ">500MB" --since 2024-01-01

# Content + extension filter
fsearch --content "import pandas" --ext py --dir ~/projects

# Raw Lucene (expert)
fsearch --query 'directory:"/data/rnaseq" AND extension:bam AND size_bytes:[1000000000 TO *]'

# Pipe-friendly
fsearch -Q "variant_calling" | xargs ls -lh

# Sort by size descending
fsearch --ext fastq --sort "size_bytes desc" --limit 20
```

---

## 7. Systemd Service for the Watcher

ini

```ini
# ~/.config/systemd/user/fs-indexer-watch.service
[Unit]
Description=Solr Filesystem Watcher

[Service]
ExecStart=/usr/bin/python3 /opt/fsearch/fs_indexer.py watch /home /data /mnt
Restart=on-failure
Environment=SOLR_URL=http://localhost:8983/solr/filesystem

[Install]
WantedBy=default.target
```

bash

```bash
systemctl --user enable --now fs-indexer-watch
```

---

## 8. Install Summary

bash

```bash
pip install pysolr tika inotify-simple click rich

# One-time full index
python fs_indexer.py index /home /data /mnt/projects --exclude /home/.cache /tmp

# Then watch for changes
python fs_indexer.py watch /home /data /mnt/projects

# Search
chmod +x fsearch.py && ln -s $(pwd)/fsearch.py ~/bin/fsearch
fsearch "your query"
```

---

## Key Design Decisions Worth Noting

**Regex support**: Solr natively supports `/regex/` syntax in Lucene queries against `text_general` fields — no post-filtering needed, it's index-time regex. For very complex regexes, `--quiet` + `grep` piping is the escape hatch.

**Content extraction strategy**: Plain text/code files read directly (fast); binary formats (PDF, DOCX, etc.) go through Tika. Bioinformatics formats (FASTA, VCF, GFF) are treated as plain text — content-indexed and regex-searchable.

**The `_text_` copy field** means a bare `fsearch "BRCA1"` searches both filename and content simultaneously without specifying fields.

**inotify watch** covers `CLOSE_WRITE` (not `MODIFY`) to avoid partial-write indexing. `MOVED_TO/FROM` handles `mv` correctly as delete+add.

It is OK to run indexer incrementally once per day on changed files (my memory for the past few days is OK). Also, I would be running this under wsl. Could run docker via Docker desktop but prefer Linux interface

Mar 24

Good call — ditching the inotify watcher simplifies things considerably, and WSL2 with a native Linux Solr install is cleaner than fighting Docker Desktop's WSL integration. Here's the revised, WSL-optimized approach.

---

## WSL2 Architecture

```
WSL2 Ubuntu
├── Solr 9.x (native Java, runs as user service)
├── Tika server (background Java process)
├── fs_indexer.py --incremental  (cron/Task Scheduler daily)
└── fsearch  (CLI, callable from both bash and Windows Terminal)
```

No Docker, no inotify daemon — just a daily incremental crawl and on-demand search.

---

## 1. Solr Native Install on WSL2

bash

```bash
# Java first (Solr 9 requires Java 11+)
sudo apt update && sudo apt install -y openjdk-17-jdk-headless

# Download and install Solr
SOLR_VER=9.7.0
wget -q https://downloads.apache.org/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz
tar xzf solr-${SOLR_VER}.tgz

# Install to ~/opt so no sudo needed
mkdir -p ~/opt
mv solr-${SOLR_VER} ~/opt/solr

# Add to PATH
echo 'export SOLR_HOME=~/opt/solr' >> ~/.bashrc
echo 'export PATH=$SOLR_HOME/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

### WSL-specific Solr config

WSL2 has no systemd by default (unless you've enabled it). Use a start script instead:

bash

```bash
# ~/opt/solr/bin/solr.in.sh tweaks — add these lines:
SOLR_JAVA_MEM="-Xms512m -Xmx2g"         # tune to your RAM
SOLR_PORT=8983
SOLR_DATA_HOME="$HOME/.solr/data"        # keep data outside install dir
SOLR_LOGS_DIR="$HOME/.solr/logs"
SOLR_PID_DIR="$HOME/.solr"
ZK_CLIENT_TIMEOUT=30000
```

bash

```bash
mkdir -p ~/.solr/{data,logs}

# Start/stop aliases
echo 'alias solr-start="$SOLR_HOME/bin/solr start -force"' >> ~/.bashrc
echo 'alias solr-stop="$SOLR_HOME/bin/solr stop"'          >> ~/.bashrc
echo 'alias solr-status="$SOLR_HOME/bin/solr status"'      >> ~/.bashrc
source ~/.bashrc
```

> **WSL note**: Solr refuses to run as root; WSL default user is fine. The `-force` flag suppresses the "are you sure you're not root" check that sometimes trips up WSL user detection.

### Create the core

bash

```bash
solr-start
solr create_core -c filesystem
```

### Post schema

bash

```bash
curl -s -X POST http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' -d @- <<'EOF'
{
  "add-field": [
    {"name":"filepath",    "type":"string",      "stored":true,  "indexed":true},
    {"name":"filename",    "type":"text_general", "stored":true,  "indexed":true},
    {"name":"filename_exact","type":"string",    "stored":false, "indexed":true},
    {"name":"extension",   "type":"string",      "stored":true,  "indexed":true},
    {"name":"directory",   "type":"string",      "stored":true,  "indexed":true},
    {"name":"size_bytes",  "type":"plong",        "stored":true,  "indexed":true},
    {"name":"mtime",       "type":"pdate",        "stored":true,  "indexed":true},
    {"name":"mimetype",    "type":"string",       "stored":true,  "indexed":true},
    {"name":"content",     "type":"text_general", "stored":false, "indexed":true},
    {"name":"owner",       "type":"string",       "stored":true,  "indexed":false}
  ],
  "add-copy-field": [
    {"source":"filename", "dest":"filename_exact"},
    {"source":"filename", "dest":"_text_"},
    {"source":"content",  "dest":"_text_"}
  ]
}
EOF
```

`filename_exact` enables case-sensitive glob/wildcard on the raw filename string while `filename` stays tokenized for full-text. `content` is not stored (saves disk — it's index-only) since we can re-read the file if needed.

---

## 2. WSL Path Handling

A critical WSL gotcha: your Windows drives are mounted at `/mnt/c`, `/mnt/d`, etc. The indexer needs to handle both pure Linux paths (`/home/user/...`) and Windows-drive paths gracefully.

python

```python
# In fs_indexer.py — add this utility
def is_wsl_windows_path(p: Path) -> bool:
    return str(p).startswith("/mnt/") and len(str(p)) > 6 and str(p)[5].isalpha()

def wsl_to_windows_path(p: Path) -> str:
    """For display purposes: /mnt/c/Users/foo -> C:\\Users\\foo"""
    parts = str(p)[5:]  # strip /mnt/
    drive = parts[0].upper()
    rest = parts[1:].replace("/", "\\")
    return f"{drive}:{rest}"
```

---

## 3. Incremental Indexer (Daily Delta)

The key change: use `find -newer` against a state file to identify changed/new files, and a separate delete pass for files that have vanished from the index.

python

```python
#!/usr/bin/env python3
"""
fs_indexer.py  — Incremental filesystem → Solr indexer for WSL2
Deps: pip install pysolr tika requests click rich
"""

import os, sys, stat, mimetypes, datetime, time, json, logging
from pathlib import Path
from typing import Generator

import click
import pysolr
import requests
from rich.progress import Progress, SpinnerColumn, TextColumn, MofNCompleteColumn
from rich.logging import RichHandler
from rich.console import Console

logging.basicConfig(handlers=[RichHandler(markup=True)], level=logging.INFO,
                    format="%(message)s", datefmt="[%X]")
log = logging.getLogger("fs_indexer")
console = Console(stderr=True)

SOLR_URL     = os.environ.get("SOLR_URL", "http://localhost:8983/solr/filesystem")
TIKA_URL     = os.environ.get("TIKA_URL", "http://localhost:9998/tika")
BATCH_SIZE   = 500
STATE_FILE   = Path.home() / ".solr" / "indexer_state.json"
MAX_CONTENT  = 10 * 1024 * 1024  # 10MB hard cap

TEXT_EXTS = {
    ".txt",".md",".rst",".org",
    ".py",".r",".R",".jl",".pl",".perl",
    ".sh",".bash",".zsh",".fish",
    ".c",".cpp",".h",".hpp",".java",".js",".ts",".go",".rs",".scala",
    ".yaml",".yml",".toml",".json",".xml",".csv",".tsv",".ndjson",
    ".html",".htm",".css",".sql",
    ".nf",".snakemake",         # nextflow / snakemake
    ".fasta",".fa",".fna",".faa",".ffn",
    ".fastq",".fq",
    ".vcf",".bcf",".gff",".gff3",".gtf",".bed",".bedgraph",
    ".sam",                      # BAM is binary, skip
    ".log",".out",".err",
    ".conf",".cfg",".ini",".env",
    ".tex",".bib",
    ".ipynb",                    # notebook JSON — content-searchable
}

TIKA_EXTS = {".pdf",".docx",".xlsx",".pptx",".odt",".ods",".odp",".epub",".rtf"}

SKIP_DIRS = {
    ".git",".svn",".hg",
    "__pycache__",".pytest_cache",".mypy_cache",
    "node_modules",".cargo",
    ".solr",".cache",".thumbnails",
    "snap","proc","sys","dev",   # Linux pseudo-fs
}

# ── State management ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "indexed_count": 0}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Content extraction ───────────────────────────────────────────────────────

def extract_via_tika(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CONTENT)
        resp = requests.put(TIKA_URL, data=data,
                            headers={"Accept": "text/plain",
                                     "Content-Type": "application/octet-stream"},
                            timeout=30)
        return resp.text if resp.ok else ""
    except Exception as e:
        log.debug(f"Tika failed for {path}: {e}")
        return ""

def extract_content(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            return raw.decode("utf-8", errors="replace")
        elif ext in TIKA_EXTS:
            return extract_via_tika(path)
    except Exception as e:
        log.debug(f"Content extraction error {path}: {e}")
    return ""

# ── Document builder ─────────────────────────────────────────────────────────

def ts_to_solr(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

def file_to_doc(path: Path) -> dict | None:
    try:
        s = path.stat()
        if stat.S_ISLNK(s.st_mode):   # skip symlinks to avoid loops
            return None
        ext = path.suffix.lower()
        mime, _ = mimetypes.guess_type(str(path))
        return {
            "id":          str(path),
            "filepath":    str(path),
            "filename":    path.name,
            "extension":   ext.lstrip(".") if ext else "",
            "directory":   str(path.parent),
            "size_bytes":  s.st_size,
            "mtime":       ts_to_solr(s.st_mtime),
            "mimetype":    mime or "application/octet-stream",
            "content":     extract_content(path),
            "owner":       str(s.st_uid),
        }
    except (PermissionError, FileNotFoundError, OSError) as e:
        log.debug(f"Skipping {path}: {e}")
        return None

# ── Crawlers ─────────────────────────────────────────────────────────────────

def crawl_full(roots: list[Path], exclude: set[Path]) -> Generator[Path, None, None]:
    for root in roots:
        for dirpath, dirs, files in os.walk(str(root), followlinks=False):
            dp = Path(dirpath)
            dirs[:] = sorted([
                d for d in dirs
                if d not in SKIP_DIRS
                and not d.startswith(".")
                and (dp / d).resolve() not in exclude
            ])
            for f in files:
                yield dp / f

def crawl_incremental(roots: list[Path], since_ts: float,
                       exclude: set[Path]) -> Generator[Path, None, None]:
    """
    Use find -newer for efficiency — avoids stat-ing every file in Python.
    Falls back to full crawl if find is unavailable.
    """
    import subprocess, tempfile

    # Write a reference timestamp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ts") as tf:
        tf_path = tf.name
    os.utime(tf_path, (since_ts, since_ts))

    try:
        for root in roots:
            exclude_args = []
            for ex in exclude:
                exclude_args += ["-path", str(ex), "-prune", "-o"]

            cmd = ["find", str(root)] + exclude_args + \
                  ["-newer", tf_path, "-type", "f", "-print0"]

            result = subprocess.run(cmd, capture_output=True)
            for fp in result.stdout.split(b"\0"):
                if fp:
                    yield Path(fp.decode("utf-8", errors="replace"))
    finally:
        os.unlink(tf_path)

# ── Delete pass: remove Solr docs for files that no longer exist ─────────────

def purge_deleted(solr: pysolr.Solr, batch_size: int = 1000):
    """
    Page through all indexed filepaths; delete docs whose file is gone.
    Runs after the index pass. Efficient: no content re-read.
    """
    log.info("Running delete pass...")
    cursor = "*"
    deleted = 0
    while True:
        results = solr.search("*:*",
                              fl="id",
                              rows=batch_size,
                              sort="id asc",
                              cursorMark=cursor)
        to_delete = [r["id"] for r in results if not Path(r["id"]).exists()]
        if to_delete:
            solr.delete(id=to_delete)
            deleted += len(to_delete)
            log.info(f"  Purged {deleted} deleted files so far...")
        new_cursor = results.nextCursorMark
        if new_cursor == cursor:
            break
        cursor = new_cursor
    solr.commit()
    log.info(f"Delete pass complete. Total purged: {deleted}")

# ── Core index routine ────────────────────────────────────────────────────────

def run_index(roots, exclude_paths, incremental: bool, no_purge: bool,
              solr_url: str, dry_run: bool):
    state = load_state()
    solr  = pysolr.Solr(solr_url, always_commit=False, timeout=60)

    since_ts = None
    if incremental:
        last = state.get("last_run")
        if last:
            since_ts = datetime.datetime.fromisoformat(last).timestamp()
            log.info(f"Incremental mode: indexing files newer than {last}")
        else:
            log.info("No previous run found — falling back to full index")

    exclude = {Path(e).resolve() for e in exclude_paths}
    roots_p = [Path(r) for r in roots]

    crawler = (crawl_incremental(roots_p, since_ts, exclude)
               if since_ts else crawl_full(roots_p, exclude))

    run_start = datetime.datetime.utcnow()
    batch, total, skipped = [], 0, 0

    with Progress(SpinnerColumn(),
                  TextColumn("[progress.description]{task.description}"),
                  MofNCompleteColumn(),
                  console=console) as prog:
        task = prog.add_task("Indexing files...", total=None)
        for path in crawler:
            doc = file_to_doc(path)
            if doc:
                batch.append(doc)
                total += 1
            else:
                skipped += 1

            if len(batch) >= BATCH_SIZE:
                if not dry_run:
                    solr.add(batch, commitWithin=10000)
                batch.clear()
            prog.update(task, completed=total,
                        description=f"[cyan]Indexed[/cyan] {path.name[:40]}")

    if batch and not dry_run:
        solr.add(batch, commitWithin=10000)

    if not dry_run:
        solr.commit()
        if not no_purge:
            purge_deleted(solr)

    state["last_run"]      = run_start.isoformat()
    state["indexed_count"] = state.get("indexed_count", 0) + total
    if not dry_run:
        save_state(state)

    log.info(f"[green]Done.[/green] Indexed: {total}, Skipped: {skipped}")

# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("roots", nargs=-1, required=True)
@click.option("-x","--exclude", multiple=True, help="Paths to exclude")
@click.option("--full",     is_flag=True, help="Force full re-index (ignore last_run)")
@click.option("--no-purge", is_flag=True, help="Skip the deleted-files purge pass")
@click.option("--dry-run",  is_flag=True, help="Parse and extract but don't write to Solr")
@click.option("--solr-url", default=SOLR_URL, show_default=True)
def main(roots, exclude, full, no_purge, dry_run, solr_url):
    """Crawl ROOT paths and index (or incrementally update) Solr."""
    run_index(roots, exclude,
              incremental=not full,
              no_purge=no_purge,
              solr_url=solr_url,
              dry_run=dry_run)

if __name__ == "__main__":
    main()
```

---

## 4. Scheduling: WSL Cron (not systemd)

WSL2 doesn't auto-start systemd services on login, but cron works fine if you start it once:

bash

```bash
# Start cron daemon (add to ~/.bashrc or Windows startup)
sudo service cron start

# Edit crontab
crontab -e
```

cron

```cron
# Run incremental index nightly at 2am
0 2 * * * /usr/bin/python3 /opt/fsearch/fs_indexer.py \
    /home/youruser /mnt/c/Users/youruser/Documents /mnt/d/data \
    --exclude /home/youruser/.cache \
    >> /home/youruser/.solr/logs/indexer.log 2>&1
```

### Auto-start Solr when WSL launches

WSL doesn't have init, so hook into `.bashrc` or use the Windows Task Scheduler to run a startup script:

bash

```bash
# ~/.bashrc — start Solr if not already running
if ! pgrep -f "solr.jetty" > /dev/null 2>&1; then
    $SOLR_HOME/bin/solr start -force -q
fi
```

Or, a dedicated startup script callable from Windows Task Scheduler:

bash

```bash
# ~/bin/wsl-startup.sh
#!/bin/bash
# Called by Windows Task Scheduler at login via:
# wsl.exe -d Ubuntu -- bash /home/youruser/bin/wsl-startup.sh
/home/youruser/opt/solr/bin/solr start -force -q
sudo service cron start
java -jar /home/youruser/opt/tika-server.jar --port 9998 &
```

powershell

```powershell
# Windows Task Scheduler action (run at logon):
# Program: wsl.exe
# Arguments: -d Ubuntu -- bash /home/youruser/bin/wsl-startup.sh
```

---

## 5. `fsearch` — no changes needed, but a WSL path tip

Add this to `fsearch.py` output for Windows-drive files so you can copy-paste into Explorer:

python

```python
def display_path(filepath: str, show_winpath: bool) -> str:
    if show_winpath and filepath.startswith("/mnt/") and filepath[5:6].isalpha():
        drive = filepath[5].upper()
        rest  = filepath[6:].replace("/", "\\")
        return f"{filepath}  [dim](→ {drive}:{rest})[/dim]"
    return filepath
```

bash

```bash
# Usage
fsearch --win-path "pandas DataFrame"
```

---

## 6. Tika for WSL

bash

```bash
TIKA_VER=3.0.0
wget https://downloads.apache.org/tika/${TIKA_VER}/tika-server-standard-${TIKA_VER}.jar \
     -O ~/opt/tika-server.jar

# Test
java -jar ~/opt/tika-server.jar --port 9998 &
curl -T some.pdf http://localhost:9998/tika   # should return plain text
```

---

## Quick Reference

bash

```bash
# First-time full index
python fs_indexer.py /home/$USER /mnt/d/data --full

# Daily delta (what cron runs)
python fs_indexer.py /home/$USER /mnt/d/data

# Force re-index specific subtree
python fs_indexer.py /mnt/d/data/newproject --full --no-purge

# Check what would be indexed without writing
python fs_indexer.py /home/$USER --dry-run

# Search
fsearch "DESeq2"
fsearch --name "*.vcf" --size ">100MB"
fsearch --content "/p[._]?adj\s*<\s*0\.05/"
fsearch -Q "salmon quantification" | xargs wc -l
```

The `find -newer` trick is the key performance win for incremental — on a large filesystem it turns a full stat-scan into a kernel-accelerated mtime comparison, typically 10-50x faster than walking the tree in Python.

To what degree is wsl Ubuntu upgradeable to a newer version? In standard Ubuntu one can migrate to the new version. Not sure if that is possible on WSL. If not, it would be necessary to migrate the system to a newer version intermittently.

Mar 24

Searched the web

Searched the web

Good news — it's more capable than you might expect, with some important caveats.

## In-place upgrade: yes, it works

`do-release-upgrade` works on WSL2, but there's a deliberate lock you have to undo first. WSL Ubuntu instances do not allow upgrading to a new release by default — you have to open `/etc/update-manager/release-upgrades` and change `Prompt=never` to `Prompt=lts`. [PhoenixNAP](https://phoenixnap.com/kb/wsl-upgrade-ubuntu)

Once unlocked, the standard procedure works:

bash

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install update-manager-core
sudo do-release-upgrade
# WSL "restart" = wsl --shutdown from PowerShell, then reopen
```

The default Ubuntu distro (`wsl --install Ubuntu`) always ships the latest stable LTS and can be upgraded once the first point release of a new LTS is available. However, explicitly versioned installs (`wsl --install Ubuntu-24.04`) won't upgrade this way unless you change the configuration. [Ubuntu](https://documentation.ubuntu.com/wsl/latest/reference/release_notes/)

## Key differences from bare-metal Ubuntu upgrades

**The kernel is decoupled.** WSL uses a modified Microsoft-supported Linux kernel (currently 5.15) rather than the kernel that ships with Ubuntu Desktop — so upgrading your Ubuntu userland does *not* upgrade the kernel. That's Microsoft's responsibility, via Windows Update. [4sysops](https://4sysops.com/archives/upgrade-from-ubuntu-2204-to-2404-lts-on-windows-subsystem-for-linux-wsl2/)

**Systemd matters now.** Ubuntu 24.04 for WSL ships with systemd enabled by default, unlike previous releases where it was optional. [4sysops](https://4sysops.com/archives/upgrade-from-ubuntu-2204-to-2404-lts-on-windows-subsystem-for-linux-wsl2/) If you're upgrading an older instance that has systemd disabled, some packages (notably snapd) will break post-upgrade until you enable it in `/etc/wsl.conf`.

**Third-party apt sources are a landmine.** Custom apt sources can cause errors during the upgrade — PPA entries for things like ngrok, 1Password, or any repo pinned to a specific Ubuntu codename should be removed from `/etc/apt/sources.list.d/` before running `do-release-upgrade`, then re-added afterward. [Mostviertel](https://www.mostviertel.tech/blog/2025/upgrade-ubuntu-on-wsl)

## The clean-slate alternative: export/import

For major version jumps or if your instance has accumulated cruft, the WSL export/import approach is actually cleaner than bare-metal equivalent:

powershell

```powershell
# Backup current instance
wsl --export Ubuntu-22.04 C:\backups\ubuntu2204_backup.tar

# Install fresh 24.04 alongside or after
wsl --install Ubuntu-24.04

# If needed, import backup elsewhere
wsl --import Ubuntu-backup D:\wsl\backup C:\backups\ubuntu2204_backup.tar
```

This gives you a guaranteed clean 24.04 base while preserving the old instance as a tarball — much safer than bare-metal where you can't trivially snapshot before upgrading.

## Practical recommendation for your use case

Given that you're running Solr, Tika, and Python tooling on a fairly stable personal workstation, the in-place upgrade path is fine. The main things to protect are:

- `~/.solr/data/` (Solr index — export before upgrading, re-index if the Solr version also bumps)
- Your Python virtualenvs (will likely need rebuild post-upgrade due to interpreter version change)
- Any custom apt sources as noted above

The WSL export-before-upgrade is essentially a free snapshot, so the risk profile is actually lower than upgrading a bare-metal Ubuntu system.

Oh yes. Now I remember why I did not install 24.04 -- snap. I dislike it with a passion. Not sure if it has adequately matured, but it broke all sorts of things including causing a bare-metal computer to freeze (cupsd in snap). It also wastes a huge amount of space

Mar 24

Your frustration is well-founded and widely shared. Let me check the current state of snap in 24.04.

Searched the web

Searched the web

Your instincts are sound, and the problems haven't gone away. Here's the honest picture:

## Snap in 2025/2026: Still controversial, not fixed

The core complaints remain valid. Snap is hard-coded to use Canonical's servers to fetch snaps and all metadata, and the code for those backend servers is not open source [LinuxBlog](https://linuxblog.io/remove-snap-ubuntu-22-04-lts/) — so the transparency and vendor lock-in issues are architectural, not bugs to be fixed. Performance at startup, squashfs loop-mount overhead, and disk waste are also structural.

Issues with snap programs not accepting keyboard input and failing to run under different user IDs have been reported as recently as 2025. [Ubuntu Community Hub](https://discourse.ubuntu.com/t/upgraded-from-ubuntu-20-04-to-24-04-problem-with-snap-programs/60980) Snap is more deeply embedded in 24.04 than 22.04, not less — Thunderbird now ships only as a snap in 24.04, and the old Snap Store was replaced by the Ubuntu App Center, which is itself a snap. [LinuxBlog](https://linuxblog.io/remove-snap-ubuntu-22-04-lts/)

## The good news: excision is well-understood

For a WSL/server/CLI-only use case like yours, snap removal is clean because **you don't need any of the GUI snap packages** (Firefox, Thunderbird, App Center, etc.). The procedure is well-established:

bash

```bash
# 1. Remove all snap packages first (order matters — apps before runtimes)
sudo snap remove --purge $(snap list | awk 'NR>1 {print $1}' | grep -v snapd) 2>/dev/null
sudo snap remove --purge snapd

# 2. Remove the daemon
sudo apt remove --autoremove snapd
sudo apt purge snapd

# 3. Clean up directories
sudo rm -rf /var/cache/snapd/ /var/snap/ /snap/ ~/snap/

# 4. The critical step: prevent apt from ever pulling it back in
sudo tee /etc/apt/preferences.d/nosnap.pref <<'EOF'
Package: snapd
Pin: release a=*
Pin-Priority: -10
EOF

sudo apt update
```

Without this APT preferences pin, `apt install firefox` on Ubuntu 22.04+ will silently reinstall snapd and install Firefox as a snap. [OneUptime](https://oneuptime.com/blog/post/2026-03-02-how-to-remove-snap-completely-and-use-apt-instead-on-ubuntu/view) The `-10` priority is the nuclear option — it tells APT to never install snapd even as a dependency.

If you want a stricter hold, `apt-mark hold snapd` is a simpler alternative, though the preferences file approach is more complete since it creates an APT preferences file that assigns snapd a negative priority, telling APT "never install this, even if something depends on it." [OSTechNix](https://ostechnix.com/remove-snap-ubuntu-24-04-lts/)

## Replacements for snap-only packages you might actually want

| Snap default                | APT/deb replacement                                 |
| --------------------------- | --------------------------------------------------- |
| Firefox                     | `ppa:mozillateam/ppa`                               |
| Thunderbird                 | `ppa:mozillateam/ppa`                               |
| App Center / GNOME Software | `sudo apt install gnome-software` (optional on WSL) |
| `chromium`                  | `ppa:saiarcot895/chromium-beta` or just use Firefox |

For your WSL + CLI-only context, you likely don't need any of these anyway.

## Alternative distro angle

If you're ever doing a fresh WSL install rather than upgrading in place, Linux Mint is a notable Ubuntu-based alternative that ships with Flatpak support but treats it as strictly optional and easily removable, without the risk of snap being reinstalled behind your back through apt dependencies. [The Register](https://forums.theregister.com/forum/all/2024/05/15/asmi_2404_ubuntu_without_snap/) For WSL specifically, Debian is another clean option — snap has never been part of it at all.

## Bottom line

For your use case (WSL2, CLI tools, bioinformatics stack, Solr/Python), snap is pure overhead with zero benefit. Excise it immediately on any fresh install, pin the APT preference, and forget it exists. The procedure above is stable and well-tested through 24.04. The cupsd-in-snap freezing class of bugs is a real pattern — snap's sandbox interacts badly with system services that need tight hardware/IPC access — and printing/CUPS is a notorious example because Canonical has been pushing cupsd into snap on desktop Ubuntu.

Hmm! SOLR is no longer where it should be / is thought to be:
wget [https://downloads.apache.org/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz](https://downloads.apache.org/solr/solr/$%7BSOLR_VER%7D/solr-$%7BSOLR_VER%7D.tgz) --2026-03-25 14:42:02-- https://downloads.apache.org/solr/solr/9.7.0/solr-9.7.0.tgz Resolving downloads.apache.org (downloads.apache.org)... 88.99.208.237, 135.181.214.104, 2a01:4f9:3a:2c57::2, ...
Connecting to downloads.apache.org (downloads.apache.org)|88.99.208.237|:443... connected.
HTTP request sent, awaiting response... 404 Not Found
2026-03-25 14:42:02 ERROR 404: Not Found.

2:42 PM

Searched the web

Searched the web

Two things happened simultaneously: Solr 10.0.0 was released on 3 March 2026 [Apache Solr](https://solr.apache.org/), and Apache's policy is that older releases get moved off `downloads.apache.org` once superseded. So 9.7.0 is now only in the archive, and 9.x itself is the previous major version getting only critical security fixes going forward.

The correct download URLs are now:

bash

```bash
# Solr 10.0.0 (current) — via Apache mirror redirector
SOLR_VER=10.0.0
wget "https://www.apache.org/dyn/closer.lua/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz?action=download" \
     -O solr-${SOLR_VER}.tgz

# Or if you want the last 9.x (9.10.1) — still in active archive
SOLR_VER=9.10.1
wget "https://www.apache.org/dyn/closer.lua/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz?action=download" \
     -O solr-${SOLR_VER}.tgz
```

The `closer.lua` redirector picks a geographically close mirror automatically — that's the canonical way to download current Apache releases. Direct `downloads.apache.org` paths only reliably work for the very latest release and for signature/checksum files.

**Which version to use?** 10.0.0 is a major version bump, so worth checking the upgrade notes for breaking changes — particularly around schema API and `solr.in.sh` options. For a fresh install from scratch like yours, just go straight to 10.0.0. The 9.x line is still receiving maintenance releases (currently 9.10.1) [Softpedia](https://www.softpedia.com/get/Internet/Servers/Other-Servers/Apache-Solr.shtml) if you want to stay conservative, but there's no real reason to avoid 10 on a new setup.

One other thing to update in the scripts I provided: the `solr create_core` command was deprecated in favor of `solr create` in recent versions:

bash

```bash
# 10.x syntax
solr start -force
solr create -c filesystem
```

Will go for 10 since upgrade maintenance route is shorter

2:43 PM

Good pragmatic call — starting on 10 means you won't need to cross a major version boundary for years. Just a few things to verify/adjust for 10.0.0 specifically since it's a fresh major release:

Searched the web

Searched the web

Good, got everything needed. Here's the full corrected install sequence for Solr 10 on WSL2:

---

## Key Solr 10 Requirement: Java 21

Solr 10.0.0 requires Java 21 minimum for the server itself (Java 17 is sufficient only for the SolrJ client library). [Apache Software Foundation](https://cwiki.apache.org/confluence/pages/viewpage.action?pageId=393677254) The Java 17 we installed earlier for Solr 9 is **not enough**. First thing to fix:

bash

```bash
# Remove 17, install 21
sudo apt remove openjdk-17-jdk-headless
sudo apt install openjdk-21-jdk-headless

# Verify
java -version   # must show 21.x
```

---

## Correct Download

bash

```bash
SOLR_VER=10.0.0

# The closer.lua redirector is required — direct downloads.apache.org no longer hosts it
wget "https://www.apache.org/dyn/closer.lua/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz?action=download" \
     -O solr-${SOLR_VER}.tgz

tar xzf solr-${SOLR_VER}.tgz
mv solr-${SOLR_VER} ~/opt/solr
```

There's also a new `solr-10.0.0-slim.tgz` variant that strips the bundled docs and examples — fine for a server-only install and noticeably smaller.

---

## Notable Solr 10 Changes That Affect Our Setup

**1. `solr create_core` is gone — use `solr create`**

Already covered in the previous update, but confirmed: the old `create_core` subcommand is removed entirely in 10.

bash

```bash
solr start -force
solr create -c filesystem
```

**2. `pysolr` client URL format tightened**

SolrClient implementations now only accept "root" URL paths ending in `/solr`, and users who relied on collection-specific URLs must instead specify a default collection via `withDefaultCollection`. [Apache Solr](https://solr.apache.org/guide/solr/latest/upgrade-notes/major-changes-in-solr-10.html) This affects our `pysolr` connection string — verify it ends in `/solr` not `/solr/filesystem`:

python

```python
# Correct for Solr 10
SOLR_URL = "http://localhost:8983/solr/filesystem"   # this form is still fine for pysolr
# pysolr handles the collection path correctly as long as it's the full URL
```

**3. Metrics changed to OpenTelemetry / snake_case**

Solr 10 migrated from Dropwizard metrics to OpenTelemetry, and all metric names changed from dot-delimited to snake_case format. [Apache Solr](https://solr.apache.org/guide/solr/latest/upgrade-notes/major-changes-in-solr-10.html) Irrelevant for our use case since we're not scraping metrics, but worth knowing if you ever add monitoring.

**4. Removed field types to avoid**

`ExternalFileField`, `CurrencyField`, and `EnumField` have been removed. [Apache Solr](https://solr.apache.org/guide/solr/latest/upgrade-notes/major-changes-in-solr-10.html) None of these are in our schema, so no impact.

**5. `solr.in.sh` path**

Location is unchanged — still at `~/opt/solr/bin/solr.in.sh`. The config options we set previously all remain valid in 10.

---

## Updated `solr.in.sh` for 10

One addition worth making — Solr 10's OTEL metrics can be noisy in logs for a single-node personal install. Suppress it:

bash

```bash
# Add to ~/opt/solr/bin/solr.in.sh
SOLR_JAVA_MEM="-Xms512m -Xmx2g"
SOLR_PORT=8983
SOLR_DATA_HOME="$HOME/.solr/data"
SOLR_LOGS_DIR="$HOME/.solr/logs"
SOLR_PID_DIR="$HOME/.solr"

# Disable OTEL telemetry export (unnecessary for local single-node)
SOLR_OPTS="$SOLR_OPTS -Dsolr.otel.exporter.otlp.endpoint="
```

---

## Full Fresh-Start Sequence

bash

```bash
# 1. Java 21
sudo apt install openjdk-21-jdk-headless
java -version

# 2. Download Solr 10
SOLR_VER=10.0.0
wget "https://www.apache.org/dyn/closer.lua/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz?action=download" \
     -O solr-${SOLR_VER}.tgz
tar xzf solr-${SOLR_VER}.tgz
mkdir -p ~/opt && mv solr-${SOLR_VER} ~/opt/solr

# 3. Env
echo 'export SOLR_HOME=~/opt/solr' >> ~/.bashrc
echo 'export PATH=$SOLR_HOME/bin:$PATH' >> ~/.bashrc
echo 'alias solr-start="$SOLR_HOME/bin/solr start -force"' >> ~/.bashrc
echo 'alias solr-stop="$SOLR_HOME/bin/solr stop"'          >> ~/.bashrc
echo 'alias solr-status="$SOLR_HOME/bin/solr status"'      >> ~/.bashrc
source ~/.bashrc

# 4. Data dirs
mkdir -p ~/.solr/{data,logs}

# 5. Start and create core
solr-start
solr create -c filesystem

# 6. Post schema (same curl command as before — Schema API unchanged)
```

Everything else in the indexer and `fsearch` scripts is unaffected — the Schema API, query API, and pysolr usage patterns are all stable across this transition.

could the data and logs be pointed to another disk? There is a near empty ext4 disk available which should have less IO than the wsl disk (home) which is the system disk?

2:49 PM

Yes, absolutely — and it's a good instinct. The WSL virtual disk (`ext4.vhdx`) is a single file on your Windows system drive, so all Solr I/O (index writes, log rotation, commit flushing) competes with general system I/O through that VHDX layer. A separate physical disk mounted as ext4 in WSL will give you direct block device access with no VHDX overhead.

Let me check the current best practice for mounting secondary disks in WSL2:

Searched the web

Searched the web

Good, that's a complete picture. Here's exactly how to do this, including the persistent mount problem which is the awkward part.

---

## The Setup

There are two distinct approaches depending on the situation:

**A) Disk is internal and currently visible to Windows** — use `wsl --mount` from an elevated PowerShell  
**B) Disk is internal and Windows has no driver for it (pure ext4, never assigned a drive letter)** — same `wsl --mount`, Windows just ignores it, WSL takes full ownership

Since you said it's a near-empty ext4 disk, scenario B is likely — Windows can't read ext4 natively so the disk just sits there unassigned.

---

## Step 1: Identify the Physical Drive Number

From an **elevated PowerShell** (Run as Administrator):

powershell

```powershell
Get-CimInstance -Query "SELECT * FROM Win32_DiskDrive" | Select-Object DeviceID, Model, Size, MediaType
```

Output will look like:

```
DeviceID            Model                    Size
\\.\PHYSICALDRIVE0  Samsung SSD 970 ...      512GB   ← system disk
\\.\PHYSICALDRIVE1  WDC WD10EZEX ...         1000GB  ← your ext4 disk
```

---

## Step 2: Mount the Disk into WSL2

From the same **elevated PowerShell**:

powershell

```powershell
# Mount the whole disk (WSL auto-detects ext4)
wsl --mount \\.\PHYSICALDRIVE1 --type ext4

# If it has a partition table (likely), mount partition 1 specifically:
wsl --mount \\.\PHYSICALDRIVE1 --partition 1 --type ext4
```

When a disk has been mounted via WSL2, it is no longer available to mount via an ext4 driver on the Windows filesystem — WSL takes exclusive ownership. [Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/wsl2-mount-disk)

The disk lands at `/mnt/wsl/PHYSICALDRIVE1` (or `PHYSICALDRIVE1p1` for a specific partition). Verify inside WSL:

bash

```bash
lsblk
ls /mnt/wsl/
df -h /mnt/wsl/PHYSICALDRIVE1p1
```

---

## Step 3: Create a Stable Mountpoint and Bind-Mount

`/mnt/wsl/` paths work but are ugly. Create a clean, stable path:

bash

```bash
sudo mkdir -p /mnt/data
sudo mount --bind /mnt/wsl/PHYSICALDRIVE1p1 /mnt/data
```

Or skip the bind and just symlink:

bash

```bash
ln -s /mnt/wsl/PHYSICALDRIVE1p1 /mnt/data
```

---

## Step 4: Point Solr at the New Disk

bash

```bash
# Create the directories on the new disk
mkdir -p /mnt/data/solr/{data,logs}

# Update solr.in.sh
sed -i 's|SOLR_DATA_HOME=.*|SOLR_DATA_HOME="/mnt/data/solr/data"|' ~/opt/solr/bin/solr.in.sh
sed -i 's|SOLR_LOGS_DIR=.*|SOLR_LOGS_DIR="/mnt/data/solr/logs"|' ~/opt/solr/bin/solr.in.sh
```

Or edit `~/opt/solr/bin/solr.in.sh` directly:

bash

```bash
SOLR_DATA_HOME="/mnt/data/solr/data"
SOLR_LOGS_DIR="/mnt/data/solr/logs"
SOLR_PID_DIR="/mnt/data/solr"     # PID file here too, off the system disk
```

Also point the indexer state file there:

python

```python
# In fs_indexer.py, change:
STATE_FILE = Path("/mnt/data/solr/indexer_state.json")
```

---

## Step 5: The Persistence Problem

This is the main WSL2 gotcha — `wsl --mount` requires Administrator access and must be re-run after every reboot. [Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/wsl2-mount-disk) There's no `/etc/fstab` equivalent that fires automatically for `wsl --mount`. The cleanest solution is a Windows Task Scheduler task that runs at logon:

**Create `C:\scripts\wsl-mount.ps1`:**

powershell

```powershell
# Wait for WSL to be ready
Start-Sleep -Seconds 5

# Mount the disk
wsl --mount \\.\PHYSICALDRIVE1 --partition 1 --type ext4

# Then start Solr (replaces the .bashrc autostart approach)
wsl -d Ubuntu -- bash -c "
    ln -sfn /mnt/wsl/PHYSICALDRIVE1p1 /mnt/data 2>/dev/null
    /home/YOURUSER/opt/solr/bin/solr start -force -q
    sudo service cron start
    nohup java -jar /home/YOURUSER/opt/tika-server.jar --port 9998 \
        > /mnt/data/solr/logs/tika.log 2>&1 &
"
```

**Register in Task Scheduler (elevated PowerShell):**

powershell

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument "-NonInteractive -WindowStyle Hidden -File C:\scripts\wsl-mount.ps1"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable:$false
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" `
             -RunLevel Highest   # needed for wsl --mount (requires elevation)

Register-ScheduledTask -TaskName "WSL-Mount-and-Start" `
    -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force
```

The `-RunLevel Highest` is essential — `wsl --mount` silently fails without elevation.

---

## Alternative: Move the WSL VHDX Instead

If the above feels like too much orchestration, there's a simpler alternative — move the entire WSL virtual disk to the secondary drive. This keeps all the standard WSL mount machinery working with no Task Scheduler gymnastics:

powershell

```powershell
# From PowerShell (not elevated needed for export)
wsl --shutdown
wsl --export Ubuntu C:\temp\ubuntu-backup.tar

# Re-import onto the other disk (e.g. D:\wsl\)
wsl --import Ubuntu D:\wsl\Ubuntu C:\temp\ubuntu-backup.tar --version 2

# Remove old registration
wsl --unregister Ubuntu   # WARNING: deletes the old VHDX, backup is your safety net
```

The WSL VHDX then lives on D: and all I/O — Solr index, logs, everything — goes to that disk without any mounting complexity. The tradeoff is that Windows still mediates access through the VHDX layer, whereas `wsl --mount` with a raw ext4 partition gives you the kernel talking directly to the block device. For Solr's sequential write pattern (index segments, WAL) the difference is real but not dramatic on modern hardware.

**Recommendation:** If the disk is a dedicated data drive you don't need to access from Windows at all, use `wsl --mount` for the cleaner I/O path. If you want simplicity and occasionally access files from Windows Explorer too, move the VHDX.

Disk is automatically mounted by wsl and is available as /mnt/wd1

2:50 PM

That's the best possible situation — WSL is already handling it via `drvfs` or automount, which means it's likely NTFS or a Windows-recognised filesystem, already mounted and stable across reboots with no Task Scheduler needed.

Searched the web

Searched the web

Good — there's an important performance implication to surface here before you proceed.

First, let's check what filesystem `/mnt/wd1` actually is, since that determines everything:

bash

```bash
# Run this in WSL
mount | grep wd1
# or
df -Th /mnt/wd1
```

The result will be one of:

- `type 9p` → **NTFS via drvfs** — WSL's automount for Windows-visible drives
- `type ext4` or `type fuseblk` → native filesystem, accessed directly

This matters enormously. WSL2 using its local ext4 filesystem is very close to native Linux performance, but WSL2 accessing host NTFS files via the 9P protocol is unusably slow — roughly 5x slower than WSL1 accessing the same files. [vxlabs](https://vxlabs.com/2019/12/06/wsl2-io-measurements/) The 9P protocol is a network-style IPC layer between the Linux VM and Windows host, and every file read and write must be translated and transmitted through this network layer, which is the significant bottleneck. [CodingEasyPeasy](https://www.codingeasypeasy.com/blog/why-is-wsl-disk-performance-slow-ntfs-vs-wsl2-virtual-disk-explained)

The practical consequence for Solr: drvfs on an SSD gets around 1 GB/s for sequential writes, which sounds fine, but NTFS mounted manually gets as low as 54 MB/s [GitHub](https://github.com/microsoft/WSL/issues/6985) — and Solr's index write pattern (many small segment files, frequent fsyncs) is particularly sensitive to this overhead.

---

## Two Paths Forward

### If `/mnt/wd1` is NTFS (drvfs / 9P)

You're currently better off keeping Solr data on the WSL system VHDX (`~/.solr/`) than moving it to `/mnt/wd1`. The VHDX is ext4 inside and doesn't go through 9P. Your instinct about IO separation was right, but NTFS via drvfs would actually be *worse* for Solr.

The correct fix here is to reformat the disk as ext4 and mount it via `wsl --mount`, or create an ext4 VHDX on that disk:

powershell

```powershell
# PowerShell — create a 200GB ext4 VHDX that lives on the WD disk
# (adjust path and size to suit)
New-VHD -Path "W:\solr-data.vhdx" -Dynamic -SizeBytes 200GB
wsl --mount --vhd "W:\solr-data.vhdx" --bare
```

Then in WSL:

bash

```bash
# Find the new device (will be /dev/sdX)
lsblk
# Format and mount it
sudo mkfs.ext4 /dev/sdX
sudo mkdir -p /mnt/solrdata
sudo mount /dev/sdX /mnt/solrdata
sudo chown $USER:$USER /mnt/solrdata
```

This gives you a proper ext4 filesystem living on the WD disk, accessed natively without 9P overhead.

### If `/mnt/wd1` is already ext4 (native)

Then it's already optimal — just point Solr straight at it. No reformatting needed.

---

## Once you have a native ext4 mount at `/mnt/wd1` (or `/mnt/solrdata`)

Everything reduces to three config lines:

bash

```bash
# Create directories
mkdir -p /mnt/wd1/solr/{data,logs}

# Update solr.in.sh
cat >> ~/opt/solr/bin/solr.in.sh << 'EOF'
SOLR_DATA_HOME="/mnt/wd1/solr/data"
SOLR_LOGS_DIR="/mnt/wd1/solr/logs"
SOLR_PID_DIR="/mnt/wd1/solr"
EOF
```

And update the indexer state file location in `fs_indexer.py`:

python

```python
STATE_FILE = Path("/mnt/wd1/solr/indexer_state.json")
```

That's genuinely all there is to it once the filesystem question is resolved. What does `mount | grep wd1` show?

/dev/sdd1 on /mnt/wd1 type ext4 (rw,relatime)

2:51 PM

Perfect. That's the best possible answer — bare ext4 block device, mounted directly, no 9P/drvfs in the path, `rw` and `relatime` (not `noatime` but close enough, and you can improve that). This is native Linux filesystem performance, essentially identical to bare metal.

A few quick optimizations before pointing Solr at it:

## 1. Add `noatime` to the mount options

`relatime` updates access timestamps on first read after a write, which still generates unnecessary writes for a search index. `noatime` eliminates that entirely:

bash

```bash
# Check current fstab / wsl.conf mount config
cat /etc/fstab
cat /etc/wsl.conf
```

WSL automounts are controlled via `/etc/wsl.conf`. Add or update:

ini

```ini
# /etc/wsl.conf
[automount]
options = "metadata,noatime"
```

Then restart WSL (`wsl --shutdown` from PowerShell, reopen). The `metadata` option preserves Linux permission bits on the mount; `noatime` kills the access-time write overhead.

---

## 2. Point Solr at it — just three lines

bash

```bash
mkdir -p /mnt/wd1/solr/{data,logs}
sudo chown -R $USER:$USER /mnt/wd1/solr   # in case root owns it

# Edit solr.in.sh — replace the previous ~/.solr paths
sed -i \
  -e 's|SOLR_DATA_HOME=.*|SOLR_DATA_HOME="/mnt/wd1/solr/data"|' \
  -e 's|SOLR_LOGS_DIR=.*|SOLR_LOGS_DIR="/mnt/wd1/solr/logs"|' \
  -e 's|SOLR_PID_DIR=.*|SOLR_PID_DIR="/mnt/wd1/solr"|' \
  ~/opt/solr/bin/solr.in.sh
```

Verify:

bash

```bash
grep -E 'SOLR_DATA_HOME|SOLR_LOGS_DIR|SOLR_PID_DIR' ~/opt/solr/bin/solr.in.sh
```

---

## 3. Update the indexer state file

In `fs_indexer.py`, one line change:

python

```python
STATE_FILE = Path("/mnt/wd1/solr/indexer_state.json")
```

---

## 4. Tika logs too

While you're at it, redirect Tika's output there as well for consolidated logging:

bash

```bash
# In ~/bin/wsl-startup.sh (or wherever you launch Tika)
nohup java -jar ~/opt/tika-server.jar --port 9998 \
    > /mnt/wd1/solr/logs/tika.log 2>&1 &
```

---

## 5. After starting Solr fresh, re-create the core

Since `SOLR_DATA_HOME` is now a new empty directory, Solr won't find the old core. Re-create it and re-run a full index:

bash

```bash
solr-start
solr create -c filesystem
# Post schema (same curl command as before)
# Then full initial index:
python fs_indexer.py /home/$USER /mnt/wd1/yourdata --full
```

---

## Summary of what you now have

| Layer             | Status                                          |
| ----------------- | ----------------------------------------------- |
| Block device      | `/dev/sdd1` — direct kernel access              |
| Filesystem        | ext4, native, no translation layer              |
| Mount             | `rw,relatime` → upgrade to `noatime`            |
| Solr index        | `/mnt/wd1/solr/data`                            |
| Solr logs         | `/mnt/wd1/solr/logs`                            |
| System disk (`~`) | Only Solr binaries and scripts — zero index I/O |

The WD disk absorbs all of Solr's write amplification (segment merges, commit flushes, WAL) while your system VHDX stays quiet. That's exactly the separation you were after.

One question: the two functions is_wsl_windows_path and wsl_to_windows_path are defined in fs_indexer but not used

3:21 PM

Correct observation — they were added speculatively when I thought the disk would be NTFS/drvfs and you might want Windows path display in `fsearch`. Since your setup is pure ext4 with no Windows-visible paths involved, they're dead code.

They'd only be genuinely useful if you were indexing files under `/mnt/c/` or `/mnt/d/` (drvfs mounts) and wanted `fsearch` to show the equivalent `C:\...` path for copy-pasting into Windows Explorer or a Windows application. Since your indexed paths will all be native Linux paths (`/home/...`, `/mnt/wd1/...`), there's no conversion needed.

Just delete both functions from `fs_indexer.py`. And the `--win-path` flag sketch in `fsearch.py` (which was only mentioned as a snippet, never fully wired in) can be ignored entirely.

If you ever do index `/mnt/c/Users/...` paths in the future, the right place for that conversion is in `fsearch.py`'s display layer — an optional flag that checks if a result path starts with `/mnt/` and has a single-letter component, then renders the Windows equivalent alongside it. But that's only worth adding if the need actually arises.

OK. I do the conversion automatically and even pwsh converts

3:23 PM

Right, `wslpath` makes it a non-issue:

bash

```bash
wslpath -w /mnt/wd1/some/file    # → W:\some\file
wslpath -u 'C:\Users\foo'        # → /mnt/c/Users/foo
```

And PowerShell/pwsh running inside WSL handles the UNC paths natively too, so there's really no case where you'd need to roll your own conversion. The two functions were solving a problem that WSL already solves at the OS level.

Dead code deleted, move on.

![1774467596533_image.png](https://claude.ai/api/be5231fe-ceb5-438d-a74e-270a02726fcb/files/019d2682-c684-7226-8834-1b5bbdfb63c5/preview)

Strange cron (crontab) error:
bad minute 
Looks fine to me but wondering about line continuation with highlights the /home slash oddly

3:40 PM

The line continuation backslashes in cron are the problem — **cron does not support `\` line continuation**. Unlike bash scripts, crontab entries must be a single unbroken line. The shell never sees the continuations; cron's parser chokes on them, hence "bad minute" (it's getting confused about the overall structure and misidentifying fields).

Write it as one long line:

cron

```cron
0 2 * * * /usr/bin/python3 /opt/fsearch/fs_indexer.py /home/youruser /mnt/c/Users/youruser/Documents /mnt/d/data --exclude /home/youruser/.cache >> /mnt/wd1/solr/logs/indexer.log 2>&1
```

If the length bothers you, the clean alternative is a wrapper script:

bash

```bash
# /opt/fsearch/run_index.sh
#!/bin/bash
/usr/bin/python3 /opt/fsearch/fs_indexer.py \
    /home/youruser \
    /mnt/c/Users/youruser/Documents \
    /mnt/d/data \
    --exclude /home/youruser/.cache \
    >> /mnt/wd1/solr/logs/indexer.log 2>&1
```

cron

```cron
0 2 * * * /opt/fsearch/run_index.sh
```

The wrapper script is bash, so `\` continuations work fine there. The crontab entry stays clean and short. This is generally the better pattern for any non-trivial cron job anyway — easier to test manually too (`bash /opt/fsearch/run_index.sh`).

Also note the log path — since you're now on `/mnt/wd1`, the log redirect should point there rather than `~/.solr/logs/`.

shouldn't the tika also be in the starup script / bashrc

4:20 PM

Yes, absolutely. Tika needs to be running before the indexer tries to extract content from PDFs/DOCX, so it belongs in the same startup path as Solr.

In `.bashrc` alongside the Solr autostart:

bash

```bash
# ~/.bashrc — start Solr and Tika if not already running
if ! pgrep -f "solr.jetty" > /dev/null 2>&1; then
    $SOLR_HOME/bin/solr start -force -q
fi

if ! pgrep -f "tika-server" > /dev/null 2>&1; then
    nohup java -jar ~/opt/tika-server.jar --port 9998 \
        >> /mnt/wd1/solr/logs/tika.log 2>&1 &
fi
```

The `pgrep` guards are important — `.bashrc` runs on every new terminal, so without them you'd accumulate multiple Tika JVM processes over time.

And in `run_index.sh`, add a safety check so the indexer doesn't silently produce empty content fields if Tika happens to be down:

bash

```bash
#!/bin/bash

# Ensure Tika is up before indexing
if ! pgrep -f "tika-server" > /dev/null 2>&1; then
    echo "Starting Tika..." >> /mnt/wd1/solr/logs/indexer.log
    nohup java -jar ~/opt/tika-server.jar --port 9998 \
        >> /mnt/wd1/solr/logs/tika.log 2>&1 &
    sleep 5   # give it time to bind the port
fi

/usr/bin/python3 /opt/fsearch/fs_indexer.py \
    /home/youruser \
    /mnt/c/Users/youruser/Documents \
    /mnt/d/data \
    --exclude /home/youruser/.cache \
    >> /mnt/wd1/solr/logs/indexer.log 2>&1
```

The `sleep 5` is crude but Tika's startup is slow (JVM + loading MIME detectors) — typically 3-8 seconds before it's actually accepting connections on 9998. If you want to be precise about it:

bash

```bash
# Wait for Tika to be ready rather than sleeping blindly
for i in $(seq 1 15); do
    curl -sf http://localhost:9998/tika > /dev/null 2>&1 && break
    sleep 2
done
```

other than pysolr (installed) what other dependencies are there? Just checking if I missed anything

4:26 PM

Looking at all the imports across both scripts:

## `fs_indexer.py`

python

```python
import pysolr       # pip install pysolr        ✓ installed
import requests     # pip install requests       — likely present, but check
import click        # pip install click
import rich         # pip install rich
from tika import parser  # pip install tika
```

## `fsearch.py`

python

```python
import pysolr       # already installed
import rich         # same as above
# everything else is stdlib: os, sys, re, datetime, argparse, json, pathlib
```

## Check what you have:

bash

```bash
python3 -c "import requests, click, rich, tika; print('all good')"
```

## Install anything missing:

bash

```bash
pip install requests click rich tika --break-system-packages
```

## Runtime dependencies (non-Python):

| What              | Why                               | Check                         |
| ----------------- | --------------------------------- | ----------------------------- |
| Java 21           | Solr 10 + Tika server             | `java -version`               |
| `tika-server.jar` | Binary extraction (PDF/DOCX)      | `ls ~/opt/tika-server.jar`    |
| `find`            | Incremental crawl (`find -newer`) | `which find` — always present |
| `curl`            | Schema posting                    | `which curl`                  |

`tika` (the Python package) is just a thin client — it talks to the running `tika-server.jar` over HTTP on port 9998. Both need to be present: the pip package and the JAR. Easy to have one without the other.

Right. all good

4:28 PM

Great. So the full stack is ready:

- Solr 10 on Java 21, data/logs on `/mnt/wd1`
- Tika server JAR + Python client for binary extraction
- `fs_indexer.py` with daily cron via `run_index.sh`
- `fsearch.py` as the CLI frontend
- Solr + Tika autostarted from `.bashrc` with `pgrep` guards
- snap-free Ubuntu, `noatime` on the ext4 mount

Next logical step is the initial full index run to populate Solr, then verify a few searches work as expected before trusting the incremental cron to take over. Something like:

bash

```bash
# Manually trigger first full index, watch it run
bash /opt/fsearch/run_index.sh --full   # or call fs_indexer.py directly with --full

# Then smoke test
fsearch "some known term"
fsearch --name "*.py" --since 2025-01-01
fsearch --content "/import\s+pandas/" --ext py
```

If anything surfaces — schema issues, Tika timeout on large PDFs, path encoding problems — now's the time to catch them before the cron takes over. Let me know how the first run goes.

wget was downloading html (redirect) instead of tika jar. It really should throw an error. Had to dig into the html to find the redirect URL

4:34 PM

Classic Apache mirror redirect trap — the `closer.lua` script returns an HTML page with a mirror redirect instead of the binary, and wget silently saves it as the target filename with no error. You end up with a "JAR" that's actually HTML until you try to run it and Java complains.

The fix is the same `?action=download` parameter we used for Solr:

bash

```bash
TIKA_VER=3.0.0
wget "https://www.apache.org/dyn/closer.lua/tika/${TIKA_VER}/tika-server-standard-${TIKA_VER}.jar?action=download" \
     -O ~/opt/tika-server.jar
```

Or use `curl -L` which actually follows redirects properly and is generally more honest about failures:

bash

```bash
curl -L --fail --progress-bar \
     "https://www.apache.org/dyn/closer.lua/tika/${TIKA_VER}/tika-server-standard-${TIKA_VER}.jar?action=download" \
     -o ~/opt/tika-server.jar
```

The `--fail` flag is the key difference from wget's default behavior — curl will exit with a non-zero status and not save the file if the server returns an HTTP error or unexpected content. wget's `--content-on-error` is off by default but it also doesn't detect the "got HTML instead of binary" case since the HTTP status is 200.

Worth verifying the download is actually a JAR regardless:

bash

```bash
file ~/opt/tika-server.jar
# Should show: Java archive data (JAR)
# Not: HTML document  ← the trap
```

Full reboot is required for wsl to find the ext4 disk again

4:51 PM

That's the expected WSL2 behavior for `wsl --mount` mounted disks — they don't survive `wsl --shutdown` + restart. But you said `/mnt/wd1` comes up automatically, which means it's being handled differently — likely via `/etc/wsl.conf` automount or `fstab`. Let's check what's actually mounting it:

bash

```bash
# Check all three possible sources
cat /etc/fstab
cat /etc/wsl.conf
systemctl list-units --type=mount 2>/dev/null | grep wd1
```

If it's in `/etc/fstab` with a UUID, that should survive without a full Windows reboot — just `wsl --shutdown` then reopen the terminal should remount it. If it's not there, adding it is the fix:

bash

```bash
# Get the UUID of /dev/sdd1
sudo blkid /dev/sdd1
```

Then add to `/etc/fstab`:

```
UUID=<your-uuid>  /mnt/wd1  ext4  defaults,noatime,nofail  0  2
```

The `nofail` is important — without it, if WSL can't find the disk on startup it will hang rather than boot with the mount absent.

The reason a full Windows reboot works but `wsl --shutdown` doesn't is that Windows itself needs to re-enumerate the block device and hand it to the WSL2 Hyper-V kernel. A `wsl --shutdown` tears down the VM but Windows doesn't always re-present the disk cleanly on next WSL start without the full hardware re-enumeration that a reboot triggers.

The `fstab` + UUID approach should solve it — WSL's init will mount it on VM start regardless. What does `blkid` show?

It is in /etc/fstab with the UUID but WSL is cantankerous and it required the reboot.

4:52 PM

That's a known WSL2 quirk — even with a correct `fstab` entry, WSL2 doesn't always re-present the block device to its kernel after `wsl --shutdown`. The device (`/dev/sdd1`) simply isn't there yet when `fstab` is processed, so the mount silently fails (or succeeds vacuously if `nofail` is set).

The root cause is that WSL2's Hyper-V lightweight VM needs Windows to re-enumerate and hand off the physical disk to the virtual kernel. A full reboot does that reliably; `wsl --shutdown` + restart often doesn't, especially for non-USB internal disks.

A few things worth trying to avoid needing full reboots:

bash

```bash
# After wsl --shutdown, before reopening WSL,
# try this from PowerShell to force disk re-enumeration:
wsl --shutdown
# wait a few seconds
wsl --mount \\.\PHYSICALDRIVE1 --partition 1 --type ext4 --name wd1
# then open WSL — fstab mount should succeed now since device exists
```

Or a manual remount script callable from inside WSL when the mount is missing:

bash

```bash
# ~/bin/remount-wd1.sh
#!/bin/bash
if ! mountpoint -q /mnt/wd1; then
    echo "wd1 not mounted, attempting remount..."
    sudo mount -a   # retry fstab
    if ! mountpoint -q /mnt/wd1; then
        echo "fstab remount failed — disk not yet visible to kernel"
        echo "Run 'wsl --mount \\\\.\PHYSICALDRIVEX --partition 1' from PowerShell first"
        exit 1
    fi
fi
echo "wd1 mounted OK"
```

And add a check to `.bashrc` so Solr doesn't start against a missing mount:

bash

```bash
# ~/.bashrc
if mountpoint -q /mnt/wd1; then
    if ! pgrep -f "solr.jetty" > /dev/null 2>&1; then
        $SOLR_HOME/bin/solr start -force -q
    fi
    if ! pgrep -f "tika-server" > /dev/null 2>&1; then
        nohup java -jar ~/opt/tika-server.jar --port 9998 \
            >> /mnt/wd1/solr/logs/tika.log 2>&1 &
    fi
else
    echo "WARNING: /mnt/wd1 not mounted — Solr/Tika not started"
fi
```

Realistically though, if a full reboot is what works reliably and you're not shutting down WSL frequently mid-session, that's not a terrible workflow — Windows machines reboot for updates regularly anyway. The cantankerousness is a genuine WSL2 limitation with physical disk passthrough that Microsoft hasn't fully resolved.

The new solr does not like the "-force", it must have the double hyphen

4:59 PM

Good catch. The CLI was refactored in Solr 10 — all options now use GNU-style double hyphens consistently:

bash

```bash
# Solr 10 — correct
solr start --force

# Solr 9 and earlier
solr start -force
```

Update everywhere it appears:

bash

```bash
# .bashrc autostart
$SOLR_HOME/bin/solr start --force -q

# aliases
alias solr-start="$SOLR_HOME/bin/solr start --force"

# wsl-startup.sh if you have one
/home/youruser/opt/solr/bin/solr start --force -q
```

Quick one-liner to fix all occurrences in those files at once:

bash

```bash
sed -i 's/solr start -force/solr start --force/g' \
    ~/.bashrc \
    ~/bin/wsl-startup.sh \
    ~/opt/solr/bin/solr.in.sh 2>/dev/null
```

Worth also checking what other single-hyphen flags you may have used — `solr create` and `solr stop` may have similar changes. Run `solr --help` or `solr start --help` to see the current accepted syntax for each subcommand.

Posting the schema failed error is "HTTP ERROR 405 HTTP method POST is not supported by this URL"

5:00 PM

Solr 10 changed the Schema API endpoint. The V1 API path still exists but schema modifications now prefer the V2 API:

bash

```bash
# Solr 9 — V1 (no longer works in 10)
curl -X POST http://localhost:8983/solr/filesystem/schema ...

# Solr 10 — V2 API
curl -X POST http://localhost:8983/api/cores/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{ ... }'
```

So the full schema post command becomes:

bash

```bash
curl -X POST http://localhost:8983/api/cores/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{
  "add-field": [
    {"name":"filepath",     "type":"string",       "stored":true,  "indexed":true},
    {"name":"filename",     "type":"text_general",  "stored":true,  "indexed":true},
    {"name":"filename_exact","type":"string",       "stored":false, "indexed":true},
    {"name":"extension",    "type":"string",       "stored":true,  "indexed":true},
    {"name":"directory",    "type":"string",       "stored":true,  "indexed":true},
    {"name":"size_bytes",   "type":"plong",         "stored":true,  "indexed":true},
    {"name":"mtime",        "type":"pdate",         "stored":true,  "indexed":true},
    {"name":"mimetype",     "type":"string",       "stored":true,  "indexed":true},
    {"name":"content",      "type":"text_general",  "stored":false, "indexed":true},
    {"name":"owner",        "type":"string",       "stored":true,  "indexed":false}
  ],
  "add-copy-field": [
    {"source":"filename", "dest":"filename_exact"},
    {"source":"filename", "dest":"_text_"},
    {"source":"content",  "dest":"_text_"}
  ]
}'
```

You can verify it worked:

bash

```bash
# Should return the schema as JSON
curl http://localhost:8983/api/cores/filesystem/schema/fields
```

The V2 API is the way forward for Solr 10+ — `http://localhost:8983/api/` rather than `http://localhost:8983/solr/` for admin operations, though query and indexing via `pysolr` against `/solr/filesystem/` still works fine.

{"responseHeader":{"status":404,"QTime":36},"error":{"metadata":{"error-class":"org.apache.solr.common.SolrException","root-error-class":"org.apache.solr.common.SolrException"},"code":404,"errorClass":"org.apache.solr.common.SolrException","msg":"Cannot find API for the path: /cores/filesystem/schema"}}

5:01 PM

The V2 path is slightly different — it's `collections` not `cores` for a standard non-cloud core:

bash

```bash
curl -X POST http://localhost:8983/api/collections/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{ ... }'
```

If that also 404s, let's just discover the correct path empirically:

bash

```bash
# See all registered V2 API paths
curl http://localhost:8983/api/ | python3 -m json.tool | grep -i schema

# Or check what paths exist under collections
curl http://localhost:8983/api/collections/filesystem | python3 -m json.tool
```

Alternatively the Solr Admin UI exposes the schema editor directly and sidesteps the API path question entirely — hit `http://localhost:8983/solr/` in a browser, select the `filesystem` core, then **Schema** in the left sidebar. You can add fields interactively there.

Or check the Solr 10 docs directly:

bash

```bash
curl http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{"add-field": [{"name":"test","type":"string","stored":true}]}'
```

It's possible the V1 endpoint still works for schema POSTs in 10.0.0 and the 405 you got originally was something else — wrong core name, Solr not fully started, or the core not yet created when the curl ran. Worth trying V1 again once you've confirmed the core exists with `curl http://localhost:8983/solr/filesystem/schema`.

Nope. The schema query fails.

curl -X POST http://localhost:8983/api/collections/filesystem/schema \
 -H 'Content-Type: application/json' \
 -d '{
 "add-field": [
 {"name":"filepath", "type":"string", "stored":true, "indexed":true},
 {"name":"filename", "type":"text_general", "stored":true, "indexed":true},
 {"name":"filename_exact","type":"string", "stored":false, "indexed":true},
 {"name":"extension", "type":"string", "stored":true, "indexed":true},
 {"name":"directory", "type":"string", "stored":true, "indexed":true},
 {"name":"size_bytes", "type":"plong", "stored":true, "indexed":true},
 {"name":"mtime", "type":"pdate", "stored":true, "indexed":true},
 {"name":"mimetype", "type":"string", "stored":true, "indexed":true},
 {"name":"content", "type":"text_general", "stored":false, "indexed":true},
 {"name":"owner", "type":"string", "stored":true, "indexed":false}
 ],
 "add-copy-field": [
 {"source":"filename", "dest":"filename_exact"},
 {"source":"filename", "dest":"_text_"},
 {"source":"content", "dest":"_text_"}
 ]
}'
{"responseHeader":{"status":404,"QTime":3},"error":{"metadata":{"error-class":"org.apache.solr.common.SolrException","root-error-class":"org.apache.solr.common.SolrException"},"code":404,"errorClass":"org.apache.solr.common.SolrException","msg":"Cannot find API for the path: /collections/filesystem/schema"}}

Show more

5:04 PM

Let's just discover what paths Solr 10 actually exposes rather than guessing:

bash

```bash
# What does the core look like?
curl http://localhost:8983/solr/admin/cores?action=STATUS | python3 -m json.tool

# Try the schema GET first — if this works, POST to same URL should too
curl http://localhost:8983/solr/filesystem/schema | python3 -m json.tool | head -20

# And the V2 root to see what's there
curl http://localhost:8983/api | python3 -m json.tool
```

The GET on `/solr/filesystem/schema` is the most informative — if that returns the schema, then the endpoint exists and the POST 405 earlier was something else. If it 404s, the core itself may not have been created correctly.

Also confirm the core actually exists:

bash

```bash
curl "http://localhost:8983/solr/admin/cores?action=STATUS&core=filesystem" \
  | python3 -m json.tool | grep -E 'name|instanceDir|dataDir|error'
```

If the core is missing that explains everything — schema posts to a non-existent core will 404 or 405 depending on how the router handles it. The `solr create -c filesystem` output would confirm whether it succeeded at the time.

curl [http://lcurl](http://lcurl/) http://localhost:8983/solr/admin/cores?action=STATUS | python3 -m json.tool
 % Total % Received % Xferd Average Speed Time Time Time Current
 Dload Upload Total Spent Left Speed
100 97 100 97 0 0 4469 0 --:--:-- --:--:-- --:--:-- 4619
{
 "responseHeader": {
 "status": 0,
 "QTime": 12
 },
 "initFailures": {},
 "status": {}
}
gerard@DESKTOP-MBTK08H:Gerard$ curl http://localhost:8983/solr/filesystem/schema | python3 -m json.tool | head -20
 % Total % Received % Xferd Average Speed Time Time Time Current
 Dload Upload Total Spent Left Speed
100 96 100 96 0 0 8772 0 --:--:-- --:--:-- --:--:-- 9600
Expecting value: line 1 column 1 (char 0)
gerard@DESKTOP-MBTK08H:Gerard$ curl http://localhost:8983/api | python3 -m json.tool
 % Total % Received % Xferd Average Speed Time Time Time Current
 Dload Upload Total Spent Left Speed
100 189 100 189 0 0 88483 0 --:--:-- --:--:-- --:--:-- 94500
{
 "responseHeader": {
 "status": 0,
 "QTime": 0
 },
 "documentation": "[v2 API :: Apache Solr Reference Guide](https://solr.apache.org/guide/solr/latest/configuration-guide/v2-api.html)",
 "description": "V2 API root path"
}

Show more

5:05 PM

The core doesn't exist — `"status": {}` is empty, meaning `solr create -c filesystem` either failed silently or the core was created in a different data directory that got lost when the mount changed.

Recreate it now that `/mnt/wd1` is properly mounted:

bash

```bash
solr create -c filesystem
```

Check the output carefully — in Solr 10 it should explicitly say something like `Created new core 'filesystem'`. Then verify:

bash

```bash
curl "http://localhost:8983/solr/admin/cores?action=STATUS" | python3 -m json.tool
# Should now show filesystem in "status": { "filesystem": { ... } }
```

Once the core exists, try the schema GET before attempting the POST:

bash

```bash
curl http://localhost:8983/solr/filesystem/schema | python3 -m json.tool | head -5
```

If that returns JSON (not empty), the V1 POST endpoint should work too — my bet is the original 405 was actually hitting a non-existent core and the error message was misleading. Then try the original V1 schema post:

bash

```bash
curl -X POST http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{ "add-field": [ ... ] }'
```

The likely sequence of events: the core was created pointing at `~/.solr/data` before you moved to `/mnt/wd1`, then when `SOLR_DATA_HOME` changed, Solr started fresh with no cores registered.

either --zk-host or --solr-url parameters, nor ZK_HOST env var provided, so assuming solr url is [http://localhost:8983](http://localhost:8983/).
Neither --zk-host or --solr-url parameters, nor ZK_HOST env var provided, so assuming solr url is [http://localhost:8983](http://localhost:8983/).
WARN - 2026-03-25 17:05:58.291; org.apache.solr.common.cloud.SolrZkClient; Using default ZkCredentialsInjector. ZkCredentialsInjector is not secure, it creates an empty list of credentials which leads to 'OPEN_ACL_UNSAFE' ACLs to Zookeeper nodes
WARN - 2026-03-25 17:05:58.294; org.apache.solr.common.cloud.SolrZkClient; Using default ZkACLProvider. DefaultZkACLProvider is not secure, it creates 'OPEN_ACL_UNSAFE' ACLs to Zookeeper nodes
WARNING: Using _default configset. Data driven schema functionality is enabled by default, which is
 NOT RECOMMENDED for production use.
 To turn it off:
 curl http://localhost:8983/solr/filesystem/config -d '{"set-user-property": {"update.autoCreateFields":"false"}}'
 Or:
 bin/solr config -c filesystem -s [http://localhost:8983](http://localhost:8983/) --action set-user-property --property update.autoCreateFields --value false
Created collection 'filesystem' with 1 shard(s), 1 replica(s) with config-set 'filesystem'

Show more

5:06 PM

Core created successfully — ignore all those warnings, they're irrelevant for a single-node local installation (ZooKeeper ACLs are a SolrCloud cluster concern, and the `_default` configset warning is for production multi-node deployments).

Now verify and post the schema:

bash

```bash
# Confirm core is registered
curl -s http://localhost:8983/solr/admin/cores?action=STATUS | python3 -m json.tool | grep -A3 filesystem

# Try schema GET — should return JSON now
curl -s http://localhost:8983/solr/filesystem/schema | python3 -m json.tool | head -10
```

If that returns JSON, post the schema using the V1 endpoint:

bash

```bash
curl -X POST http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{
  "add-field": [
    {"name":"filepath",      "type":"string",      "stored":true,  "indexed":true},
    {"name":"filename",      "type":"text_general", "stored":true,  "indexed":true},
    {"name":"filename_exact","type":"string",      "stored":false, "indexed":true},
    {"name":"extension",     "type":"string",      "stored":true,  "indexed":true},
    {"name":"directory",     "type":"string",      "stored":true,  "indexed":true},
    {"name":"size_bytes",    "type":"plong",        "stored":true,  "indexed":true},
    {"name":"mtime",         "type":"pdate",        "stored":true,  "indexed":true},
    {"name":"mimetype",      "type":"string",      "stored":true,  "indexed":true},
    {"name":"content",       "type":"text_general", "stored":false, "indexed":true},
    {"name":"owner",         "type":"string",      "stored":true,  "indexed":false}
  ],
  "add-copy-field": [
    {"source":"filename", "dest":"filename_exact"},
    {"source":"filename", "dest":"_text_"},
    {"source":"content",  "dest":"_text_"}
  ]
}'
```

Also worth disabling the auto-create fields feature as suggested in the warning — it would otherwise silently add fields from indexed documents with guessed types, which could conflict with our explicit schema:

bash

```bash
curl http://localhost:8983/solr/filesystem/config \
  -d '{"set-user-property": {"update.autoCreateFields":"false"}}'
```

curl http://localhost:8983/solr/filesystem/config \
 -d '{"set-user-property": {"update.autoCreateFields":"false"}}'
{
 "responseHeader":{
 "status":0,
 "QTime":133
 },
 "WARNING":"This response format is experimental. It is likely to change in the future."
}

5:07 PM

That's a success — `status:0` means it applied correctly. The "experimental" warning is just Solr hedging about the V1 config API format, it's been there for years and works fine.

Now post the schema and you should be good to go:

bash

```bash
curl -X POST http://localhost:8983/solr/filesystem/schema \
  -H 'Content-Type: application/json' \
  -d '{ ... }'
```

If that returns `{"responseHeader":{"status":0,...}}` you're done with setup and can run the first full index:

bash

```bash
python3 /opt/fsearch/fs_indexer.py \
    /home/youruser /mnt/wd1/yourdata \
    --full \
    --solr-url http://localhost:8983/solr/filesystem
```

Chugging away. 

Perhaps rolling up all the experience into single zip file of scripts would be useful.

5:10 PM

Good idea — let me assemble everything cleanly with all the corrections applied (Solr 10 syntax, double hyphens, correct API endpoints, WSL paths, no dead code, etc.).

Created 6 files, ran 3 commands, viewed a file

Created 6 files, ran 3 commands, viewed a file

15KB zip containing:

```
fsearch/
├── README.md                  full reference: usage, flags, WSL notes, gotchas
├── bin/
│   ├── fs_indexer.py          crawler / incremental indexer
│   ├── fsearch.py             CLI search tool
│   └── run_index.sh           cron wrapper with mount + Tika guards
└── setup/
    ├── install.sh             one-shot install script (edit CONFIG section first)
    └── setup_schema.sh        posts schema to Solr, verifies fields
```

All the corrections from this session are baked in — double-hyphen `--force`, `curl -L` with `?action=download` for both Solr and Tika, single-line cron, no dead WSL path conversion functions, `/mnt/wd1` paths throughout, and the JAR verification step to catch the wget HTML redirect trap.

Fsearch

ZIP 

Indexed count_wordsfile.py 129999/?[17:15:00] ERROR Failed to connect to server at http://localhost:8983/solr/filesystem/update/?commitWithin=10000: ('Connection aborted.', pysolr.py:348
 TimeoutError('timed out'))
 Traceback (most recent call last):
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connectionpool.py", line 787, in urlopen
 response = self._make_request(
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connectionpool.py", line 493, in _make_request
 conn.request(
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connection.py", line 459, in request
 self.send(chunk)
 File "/usr/lib/python3.10/http/client.py", line 1019, in send
 self.sock.sendall(data)
 TimeoutError: timed out

Show more

5:17 PM

Solr timed out receiving a batch — almost certainly because a large file (or several) in that batch took too long to extract via Tika, and by the time the batch was ready to send, the connection had stalled. The indexer was at ~130k files so it had been running a while.

A few things to tune:

## 1. Increase the pysolr timeout

The default 60s we set may not be enough if a single batch contains several large PDFs being Tika-processed synchronously before the POST fires.

python

```python
# fs_indexer.py — increase timeout
solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
```

## 2. Reduce batch size

Smaller batches mean less time accumulating before each POST, reducing the chance of the connection going stale:

python

```python
BATCH_SIZE = 200   # down from 500
```

## 3. Cap Tika extraction time per file

The real fix — add a per-file timeout so one massive PDF can't stall the whole batch:

python

```python
def extract_via_tika(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CONTENT)
        resp = requests.put(
            TIKA_URL, data=data,
            headers={"Accept": "text/plain",
                     "Content-Type": "application/octet-stream"},
            timeout=15,          # was 30 — fail fast on slow extractions
        )
        return resp.text if resp.ok else ""
    except requests.exceptions.Timeout:
        log.debug(f"Tika timeout for {path} — skipping content")
        return ""
    except Exception as e:
        log.debug(f"Tika failed for {path}: {e}")
        return ""
```

## 4. Resume without re-indexing everything

The good news — since `indexer_state.json` only gets written on clean completion, `last_run` still reflects the previous successful run. So re-running without `--full` will pick up from where it left off via `find -newer`, though it will re-process files modified since the last successful run rather than exactly from file 130k.

If you want to restart cleanly from the point of failure rather than from `last_run`, the simplest approach is to just re-run with `--full --no-purge` — the purge pass is the slow part to skip, and Solr will upsert (overwrite by `id`) any already-indexed docs harmlessly:

bash

```bash
python3 /opt/fsearch/fs_indexer.py /home/$USER /mnt/wd1/data \
    --full --no-purge
```

The three config changes (timeout 120, batch 200, Tika timeout 15) should prevent the stall from recurring.

just rerunning without full
Is there a way to quickly query the count of indexed files, i.e., what is the syntax?

5:31 PM

Rerunning without `--full` is fine — it'll pick up anything newer than `last_run`.

Quick count query:

bash

```bash
curl -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" \
  | python3 -m json.tool | grep numFound
```

`rows=0` means no documents are returned — just the header with `numFound`. Fast regardless of index size.

Or wrap it as a handy alias:

bash

```bash
alias solr-count='curl -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" | python3 -c "import sys,json; print(json.load(sys.stdin)[\"response\"][\"numFound\"], \"docs indexed\")"'
```

Which gives clean output like:

```
129847 docs indexed
```

count seems constant even though indexing is proceeding -- although the indexer does time out every now and then

5:38 PM

That's expected — `commitWithin=10000` means Solr batches commits and only makes documents visible to queries after the commit fires. During active indexing the count will appear frozen, then jump in chunks.

The periodic timeouts are the more pressing issue. Since it's happening repeatedly, the likely culprit is a specific file type or directory with many large files hitting the Tika 15s timeout and stalling the batch accumulation. Worth checking the indexer log for patterns:

bash

```bash
# What files are triggering debug-level Tika failures
python3 /opt/fsearch/fs_indexer.py /home/$USER --dry-run 2>&1 | grep -i "tika\|timeout\|skip"

# Or check if timeouts cluster around a particular extension or directory
grep -i "timeout\|timed out" /mnt/wd1/solr/logs/indexer.log | head -20
```

A quick mitigation — skip Tika entirely for files over a certain size, since large PDFs/DOCX are the usual culprit:

python

```python
def extract_via_tika(path: Path) -> str:
    # Skip Tika for very large files — not worth the timeout risk
    if path.stat().st_size > 50 * 1024 * 1024:   # 50MB
        log.debug(f"Skipping Tika for large file: {path}")
        return ""
    try:
        ...
```

Also worth checking if Tika itself is accumulating stuck threads over a long run:

bash

```bash
# Tika memory/thread health
curl -s http://localhost:9998/tika | head -5   # just checks it's alive

# If Tika seems sluggish, restart it
pkill -f tika-server
nohup java -jar ~/opt/tika-server.jar --port 9998 \
    >> /mnt/wd1/solr/logs/tika.log 2>&1 &
```

A long-running Tika JVM can accumulate memory pressure from parsing malformed PDFs, and restarting it mid-index is harmless.

The indexer log file is not in the directory and I don't find it anywhere. Should it be provided to the script as an argument

5:46 PM

The log isn't being written to a file at all — the indexer uses `RichHandler` which writes to stderr, and the cron job redirects that to the log file. But when you're running the indexer manually from the terminal, it just goes to the console with no file output.

Two options:

**Redirect manually when running interactively:**

bash

```bash
python3 /opt/fsearch/fs_indexer.py /home/$USER /mnt/wd1/data \
    2>&1 | tee /mnt/wd1/solr/logs/indexer.log
```

`tee` gives you both console output and the file simultaneously.

**Or add file logging to the script itself** so it always writes to the log regardless of how it's invoked — cleaner long term. Add this near the top of `fs_indexer.py` after the existing logging setup:

python

```python
# Add file handler alongside RichHandler
LOG_FILE = Path("/mnt/wd1/solr/logs/indexer.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(file_handler)
```

That way both manual runs and cron runs always append to the same log file, and the cron redirect in `run_index.sh` becomes redundant (harmless to keep though as a belt-and-suspenders capture of any stdout).

Log is not providing filename details but it does show no previous run found

5:51 PM

"No previous run found" means `indexer_state.json` either doesn't exist yet or isn't being found at the path the script expects. That would cause a full crawl every run rather than incremental, which explains the repeated timeouts — it's re-processing everything each time.

Check:

bash

```bash
# Does the state file exist?
ls -la /mnt/wd1/solr/indexer_state.json

# What path is the script actually looking at?
grep STATE_FILE /opt/fsearch/fs_indexer.py

# If it exists, what's in it?
cat /mnt/wd1/solr/indexer_state.json
```

The state file only gets written on **clean completion** — if the run times out or is interrupted, `save_state()` never fires. So every interrupted run leaves no record and the next run starts fresh.

Two fixes:

**1. Save state more frequently** — write `last_run` at the start of the run rather than the end, so even an interrupted run leaves a checkpoint:

python

```python
def run_index(...):
    state = load_state()
    solr  = pysolr.Solr(solr_url, always_commit=False, timeout=120)

    run_start = datetime.datetime.utcnow()

    # Write checkpoint immediately so interruptions don't lose progress
    if not dry_run:
        state["last_run"] = run_start.isoformat()
        save_state(state)

    # ... rest of function unchanged
```

**2. Add debug logging of the state file path and its contents** at startup:

python

```python
def run_index(...):
    state = load_state()
    log.info(f"State file: {STATE_FILE} — last_run: {state.get('last_run', 'never')}")
```

The filename-level debug logging not appearing is because the default log level is `INFO` and file-level messages are logged at `DEBUG`. To see them during a run:

python

```python
# Change in fs_indexer.py
logging.basicConfig(
    handlers=[RichHandler(markup=True)],
    level=logging.DEBUG,    # was INFO
    ...
)
```

Though that will be very verbose — better to just add a specific `log.info` for files that hit Tika timeout rather than promoting everything to DEBUG.

The point at which if fails is always 
Indexed check_gpu_mem.py 129899/?

5:53 PM

That's a very specific failure point — always the same file at the same count. That strongly suggests `check_gpu_mem.py` itself isn't the problem, but rather the **next file in the crawl order** after it is what's causing the timeout — likely a large binary or PDF that Tika is choking on.

bash

```bash
# Find what file comes immediately after check_gpu_mem.py in crawl order
# The indexer uses os.walk which follows filesystem order
# Find the directory it's in first
find / -name "check_gpu_mem.py" 2>/dev/null

# Then list what's alongside it sorted as the filesystem sees it
ls -la $(dirname $(find / -name "check_gpu_mem.py" 2>/dev/null | head -1))
```

Also check if it's always exactly 129899 or just approximately:

bash

```bash
# If it's always exactly 129899, the batch boundary is the issue
# BATCH_SIZE=200 means batch 650 (129899 / 200) is where it dies
# One file in that batch is toxic
python3 -c "print(129899 % 200)"   # position within the batch
```

The quickest diagnostic — temporarily skip Tika entirely to confirm it's a content extraction issue:

python

```python
# In fs_indexer.py, temporarily comment out Tika routing
def extract_content(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            return raw.decode("utf-8", errors="replace")
        # elif ext in TIKA_EXTS:        # temporarily disabled
        #     return extract_via_tika(path)
    except Exception as e:
        log.debug(f"Content extraction error {path}: {e}")
    return ""
```

If the indexer sails past 129899 with Tika disabled, you've confirmed it's a specific PDF/DOCX in that batch. Then re-enable Tika and add logging to identify exactly which file:

python

```python
def extract_via_tika(path: Path) -> str:
    log.info(f"Tika extracting: {path}")   # add this line
    try:
        ...
```

That will show you the last file Tika attempted before the timeout.

Does not appear tika related. No tika message after prior solr log line
 INFO Tika extracting: /mnt/wd1/GT/NLM_CDE/work/R_analyses/kmers/PriorFigs/kmer_hist_questions-bw25_40-2000.pdf fs_indexer.py:79
⠦ Indexed redness-alternative_7b6b0f6fedcb45c2b102 129599/?[18:06:20] INFO Finished 'http://localhost:8983/solr/filesystem/update/?commitWithin=10000' (post) with body '[{"id": "/' in 4.545 seconds, with status 200 pysolr.py:356
⠇ Indexed check_gpu_mem.py 129899/?[18:08:28] ERROR Failed to connect to server at http://localhost:8983/solr/filesystem/update/?commitWithin=10000: ('Connection aborted.', TimeoutError('timed out'))

6:08 PM

The timing tells the story clearly:

- `18:06:20` — successful batch POST (4.5s)
- `18:08:28` — timeout failure — that's **2 minutes 8 seconds** later

So the indexer spent 2+ minutes building the batch between those two POSTs. Something in files 129600–129899 is taking extremely long to process — not Tika (no Tika message in that window), so it's a **text file being read directly** that's either huge or causing a decode hang.

The `MAX_CONTENT` cap should prevent this but the read itself could be slow if the file is on a slow path or is being decompressed on the fly. Add timing to the content extraction:

python

```python
def extract_content(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            if sz > MAX_CONTENT:
                log.info(f"Large text file ({sz/1024/1024:.1f}MB): {path}")
            start = time.monotonic()
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            elapsed = time.monotonic() - start
            if elapsed > 5:
                log.warning(f"Slow read ({elapsed:.1f}s): {path}")
            return raw.decode("utf-8", errors="replace")
```

Add `import time` at the top.

But more immediately — find what large text files exist in that region of the crawl:

bash

```bash
# Find the directory containing check_gpu_mem.py
find / -name "check_gpu_mem.py" 2>/dev/null

# Then find large text/code files nearby in the same tree
find $(dirname $(find / -name "check_gpu_mem.py" 2>/dev/null | head -1)) \
    -type f \( -name "*.py" -o -name "*.r" -o -name "*.R" -o -name "*.log" \
              -o -name "*.csv" -o -name "*.tsv" -o -name "*.json" \) \
    -size +50M 2>/dev/null
```

Large `.log`, `.csv`, `.tsv` or `.json` files are the usual culprits — they're in `TEXT_EXTS` so they get read directly, and a 500MB log file still reads 10MB which can be slow depending on where it lives. Also worth checking for `.sam` files — those can be enormous.

bash

```bash
# Any massive files in that vicinity
find $(dirname $(find / -name "check_gpu_mem.py" 2>/dev/null | head -1)) \
    -type f -size +100M 2>/dev/null | head -20
```

There are fair number of these files on the system (in the vicinity of check_gpu_mem, but not in the directory itself)

6:12 PM

So the crawl is hitting a directory full of large files right after `check_gpu_mem.py`. The 2-minute gap is the batch accumulating slowly as each large file gets read up to the 10MB cap — 300 files × even modest read time adds up.

The fix is to add a per-file size threshold above which text content extraction is skipped entirely — metadata (filename, path, size, date) still gets indexed, just no content:

python

```python
MAX_TEXT_SIZE = 50 * 1024 * 1024   # 50MB — skip content extraction above this

def extract_content(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            if sz > MAX_TEXT_SIZE:
                log.info(f"Skipping content (too large {sz/1024/1024:.0f}MB): {path}")
                return ""
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            return raw.decode("utf-8", errors="replace")
        elif ext in TIKA_EXTS:
            return extract_via_tika(path)
    except Exception as e:
        log.debug(f"Content extraction error {path}: {e}")
    return ""
```

50MB is a reasonable threshold — a 50MB `.log` or `.csv` is unlikely to contain anything you'd search for by content that isn't also findable by filename. You can tune it down to 20MB or 10MB if you want to be more aggressive.

Alternatively, explicitly exclude the offending directory from indexing if content-searching those large files isn't useful:

bash

```bash
python3 /opt/fsearch/fs_indexer.py /home/$USER /mnt/wd1/data \
    --exclude /path/to/large/files/dir \
    --exclude /another/large/dir
```

Both approaches together is probably the right answer — size threshold as a general safety net, plus explicit excludes for directories you know are bulk data you'd never search by content (raw sequencing output, large intermediate files etc.).

20M seems a good limit (almost no difference in count of 20M vs 50M). Might want to create specific flag that allows indexing of large files with extra long timeout if indexing is desired, i.e., handle large files on a per case basis.

6:17 PM

Good design — keep the default fast and safe, with an opt-in for large file content extraction when specifically needed. Here's the updated section:

python

```python
# ── Configuration ─────────────────────────────────────────────────────────────

MAX_CONTENT      = 10 * 1024 * 1024   # 10MB read cap (applied after size check)
MAX_TEXT_SIZE    = 20 * 1024 * 1024   # 20MB — skip content extraction above this by default
LARGE_FILE_LIMIT = 500 * 1024 * 1024  # 500MB — hard cap even with --large-files
LARGE_TIKA_TIMEOUT = 120              # seconds — Tika timeout for large files
```

python

```python
def extract_content(path: Path, large_files: bool = False) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            sz = path.stat().st_size
            if sz > LARGE_FILE_LIMIT:
                log.info(f"Skipping content (exceeds hard cap {sz/1024/1024:.0f}MB): {path}")
                return ""
            if sz > MAX_TEXT_SIZE:
                if not large_files:
                    log.debug(f"Skipping content (>{MAX_TEXT_SIZE//1024//1024}MB, use --large-files): {path}")
                    return ""
                log.info(f"Large file content extraction ({sz/1024/1024:.0f}MB): {path}")
            with open(path, "rb") as f:
                raw = f.read(min(sz, MAX_CONTENT))
            return raw.decode("utf-8", errors="replace")
        elif ext in TIKA_EXTS:
            sz = path.stat().st_size
            if sz > LARGE_FILE_LIMIT:
                return ""
            if sz > MAX_TEXT_SIZE and not large_files:
                return ""
            return extract_via_tika(path, large=sz > MAX_TEXT_SIZE)
    except Exception as e:
        log.debug(f"Content extraction error {path}: {e}")
    return ""


def extract_via_tika(path: Path, large: bool = False) -> str:
    timeout = LARGE_TIKA_TIMEOUT if large else 15
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CONTENT)
        resp = requests.put(
            TIKA_URL, data=data,
            headers={"Accept": "text/plain",
                     "Content-Type": "application/octet-stream"},
            timeout=timeout,
        )
        return resp.text if resp.ok else ""
    except requests.exceptions.Timeout:
        log.warning(f"Tika timeout ({timeout}s) for {path} — skipping content")
        return ""
    except Exception as e:
        log.debug(f"Tika failed for {path}: {e}")
        return ""
```

Then thread `large_files` through `file_to_doc` and the CLI:

python

```python
def file_to_doc(path: Path, large_files: bool = False) -> dict | None:
    try:
        ...
        return {
            ...
            "content": extract_content(path, large_files=large_files),
            ...
        }
    except ...


def run_index(roots, exclude_paths, incremental, no_purge,
              solr_url, dry_run, large_files):   # add large_files parameter
    ...
    for path in crawler:
        doc = file_to_doc(path, large_files=large_files)
        ...


@click.command()
...
@click.option("--large-files", is_flag=True, default=False,
              help=f"Extract content from files >{MAX_TEXT_SIZE//1024//1024}MB "
                   f"(slower, longer Tika timeout of {LARGE_TIKA_TIMEOUT}s)")
def main(roots, exclude, full, no_purge, dry_run, solr_url, large_files):
    run_index(
        roots, exclude,
        incremental=not full,
        no_purge=no_purge,
        solr_url=solr_url,
        dry_run=dry_run,
        large_files=large_files,
    )
```

Usage then becomes:

bash

```bash
# Normal daily run — skips anything >20MB
python3 fs_indexer.py /home/$USER /mnt/wd1/data

# Targeted large-file index of a specific directory
python3 fs_indexer.py /mnt/wd1/GT/NLM_CDE/work/R_analyses \
    --large-files --full --no-purge

# Combine with exclude to index large files everywhere except bulk data dirs
python3 fs_indexer.py /home/$USER \
    --large-files \
    --exclude /home/$USER/rawdata
```

The design keeps the two concerns clean — the daily cron run stays fast and predictable, while `--large-files` is a deliberate one-off invocation for specific trees where content-searching large files is actually useful.

grep -n large_files ~/opt/fsearch/fs_indexer.py 101:def extract_content(path: Path, large_files: bool = False) -> str: 110: if not large_files: 121: if sz > MAX_TEXT_SIZE and not large_files: 134:def file_to_doc(path: Path, large_files: bool = False) -> dict | None: 15

pasted

Traceback (most recent call last):
 File "/home/gerard/opt/fsearch/fs_indexer.py", line 324, in <module>
 main()
 File "/home/gerard/.local/lib/python3.10/site-packages/click/core.py", line 1485, in __call__
 return self.main(*args, **kwargs)
 File "/home/gerard/.local/lib/python3.10/site-packages/click/core.py", line 1406, in main
 rv = self.invoke(ctx)
 File "/home/gerard/.local/lib/python3.10/site-packages/click/core.py", line 1269, in invoke
 return ctx.invoke(self.callback, **ctx.params)
 File "/home/gerard/.local/lib/python3.10/site-packages/click/core.py", line 824, in invoke
 return callback(*args, **kwargs)
TypeError: main() got an unexpected keyword argument 'large_files'

Show more

6:34 PM

The `--large-files` option was added to `run_index()` but the `@click.command` decorator and `main()` function signature are missing it. Looking at your grep output, lines 315-321 show `main()` calling `run_index()` with `large_files=large_files` but the `main()` function itself never receives it from click.

Find the `@click.command` block and add the missing option and parameter:

python

```python
@click.command()
@click.argument("roots", nargs=-1, required=True)
@click.option("-x", "--exclude",    multiple=True, help="Paths to exclude")
@click.option("--full",             is_flag=True,  help="Force full re-index (ignore last_run)")
@click.option("--no-purge",         is_flag=True,  help="Skip the deleted-files purge pass")
@click.option("--dry-run",          is_flag=True,  help="Parse and extract but don't write to Solr")
@click.option("--solr-url",         default=SOLR_URL, show_default=True)
@click.option("--large-files",      is_flag=True,  default=False,
              help=f"Extract content from files >{MAX_TEXT_SIZE//1024//1024}MB "
                   f"(slower, Tika timeout {LARGE_TIKA_TIMEOUT}s)")
def main(roots, exclude, full, no_purge, dry_run, solr_url, large_files):
    """Crawl ROOT paths and index (or incrementally update) Solr."""
    run_index(
        roots, exclude,
        incremental=not full,
        no_purge=no_purge,
        solr_url=solr_url,
        dry_run=dry_run,
        large_files=large_files,
    )
```

Also I can see lines 236-240 in your file still contain the dead `wsl_to_windows_path` function — worth removing that while you're editing:

bash

```bash
# Check what's around line 236
grep -n -A6 "def wsl_to_windows_path" ~/opt/fsearch/fs_indexer.py
```

# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("roots", nargs=-1, required=True)
@click.option("-x","--exclude", multiple=True, help="Paths to exclude")
@click.option("--full", is_flag=True, help="Force full re-index (ignore last_run)")
@click.option("--no-purge", is_flag=True, help="Skip the deleted-files purge pass")
@click.option("--dry-run", is_flag=True, help="Parse and extract but don't write to Solr")
@click.option("--solr-url", default=SOLR_URL, show_default=True)
@click.option("--large-files", is_flag=True, default=False,
 help=f"Extract content from files >{MAX_TEXT_SIZE//1024//1024}MB "
 f"(slower, longer Tika timeout of {LARGE_TIKA_TIMEOUT}s)")

Show more

6:35 PM

The decorator is there but the `def main(...)` line and body beneath it is missing — the file was likely truncated when you edited it. Check:

bash

```bash
# See what's after the last @click.option
grep -n "def main" ~/opt/fsearch/fs_indexer.py
tail -20 ~/opt/fsearch/fs_indexer.py
```

If `def main` is absent, append it:

bash

```bash
cat >> ~/opt/fsearch/fs_indexer.py << 'EOF'
def main(roots, exclude, full, no_purge, dry_run, solr_url, large_files):
    """Crawl ROOT paths and index (or incrementally update) Solr."""
    run_index(
        roots, exclude,
        incremental=not full,
        no_purge=no_purge,
        solr_url=solr_url,
        dry_run=dry_run,
        large_files=large_files,
    )

if __name__ == "__main__":
    main()
EOF
```

If `def main` exists but has the old signature without `large_files`, use `str_replace` to fix just that line:

bash

```bash
# Check current signature
grep -n "^def main" ~/opt/fsearch/fs_indexer.py
```

Share what `tail -20` shows and I can give the exact fix.

It was the the main arguments (all but that one)

6:37 PM

Classic — the `def main(...)` line had the old parameter list without `large_files`. Quick fix:

bash

```bash
sed -i 's/def main(roots, exclude, full, no_purge, dry_run, solr_url):/def main(roots, exclude, full, no_purge, dry_run, solr_url, large_files):/' \
    ~/opt/fsearch/fs_indexer.py
```

Verify:

bash

```bash
grep -n "^def main" ~/opt/fsearch/fs_indexer.py
```

Should now show all seven parameters including `large_files`.

Fixed it in my standby: vim.

6:37 PM

Good — vim is the right tool for surgical edits like that.

Don't forget to also update the zip/backup copy of the scripts once the indexer finishes its current run successfully. At that point you'll have a known-good version with all fixes applied:

bash

```bash
cd ~
zip -r fsearch_$(date +%Y%m%d).zip opt/fsearch/
```

Datestamping the zip is useful since there have been several iterations — easy to diff or roll back if needed.

This was the problem file Skipping content (exceeds hard cap 958MB): /mnt/wd1/GT/NLM_CDE/cde_query/data/direct/test/cde_all_lemmas_4w10id_verbatim_test3_wstop.json

6:40 PM

A 958MB JSON file — that would have taken forever to read even just the 10MB cap, and likely caused the connection to go stale while the batch accumulated. The hard cap is doing exactly what it should now.

If you ever do want to content-index that file specifically, it's a good candidate for a targeted `--large-files` run scoped to just that directory rather than the whole tree:

bash

```bash
python3 ~/opt/fsearch/fs_indexer.py \
    /mnt/wd1/GT/NLM_CDE/cde_query/data/direct/test \
    --large-files --full --no-purge
```

Though for a nearly 1GB JSON file the first 10MB of content is probably just array structure and not particularly searchable anyway — filename + path indexing is likely sufficient for that one.

My work tends to have a fair number of large files.

6:41 PM

Makes sense for bioinformatics/NLP work — large JSON corpora, embedding matrices, lemma files, VCF/SAM, large CSVs are all routine. Worth reviewing `TEXT_EXTS` and `TIKA_EXTS` with that in mind and being more selective about what gets content-extracted at all.

Some candidates to move out of content extraction entirely — index by metadata only:

python

```python
# These are almost never usefully searchable by content at scale
# Add a METADATA_ONLY_EXTS set
METADATA_ONLY_EXTS = {
    ".json",      # often large corpora, embeddings, model outputs
    ".jsonl",     # even worse — one JSON object per line, can be enormous  
    ".ndjson",
    ".csv",       # large tables — filename + size more useful than content
    ".tsv",
    ".sam",       # always huge
    ".fasta", ".fa", ".fna", ".faa", ".ffn",  # sequence data
    ".fastq", ".fq",                           # raw reads
    ".bed", ".bedgraph",                       # genomic intervals
    ".vcf",                                    # variant calls
}
```

Then in `extract_content`:

python

```python
def extract_content(path: Path, large_files: bool = False) -> str:
    ext = path.suffix.lower()

    # Metadata-only types — not worth content indexing regardless of size
    if ext in METADATA_ONLY_EXTS:
        return ""

    try:
        if ext in TEXT_EXTS:
        ...
```

The rationale being — for a VCF or FASTA you'd search by filename, directory, date, and size. You'd never `fsearch --content "ATCGATCG"` across your whole filesystem. Whereas `.py`, `.r`, `.md`, `.log`, `.conf` files are exactly what you want content-indexed.

For the JSON/NLP corpus files specifically, if content search is occasionally useful, `--large-files` targeted at a specific project directory is the right lever rather than trying to content-index them globally.

What's your typical breakdown — mostly code/scripts, or a mix with substantial corpus/data files?

The mix varies by the project. I try to separate data from scripts and other files, but sometimes small and large files necessarily cohabit to reduce complexity of directory trees.

6:44 PM

That's a common and pragmatic tradeoff — forcing strict separation just to satisfy an indexer would be worse than the occasional timeout.

The right approach is probably a per-directory override mechanism rather than global type exclusions. Something like a small config file the indexer looks for:

python

```python
# .fsearch_config in any directory — overrides global settings for that subtree
# Examples:
# metadata_only = true          index filenames/dates/sizes but no content
# exclude = true                skip this directory entirely  
# large_files = true            enable large file extraction for this subtree
# max_size_mb = 100             custom size cap for this subtree
```

The indexer checks for `.fsearch_config` as it descends the tree, inheriting the nearest ancestor's settings:

python

```python
import configparser

def load_dir_config(dirpath: Path, parent_config: dict) -> dict:
    """
    Load .fsearch_config from dirpath if present, inheriting parent settings.
    Returns merged config dict.
    """
    config = parent_config.copy()
    cfg_file = dirpath / ".fsearch_config"
    if cfg_file.exists():
        cp = configparser.ConfigParser()
        cp.read(cfg_file)
        s = cp["fsearch"] if "fsearch" in cp else {}
        if "metadata_only" in s:
            config["metadata_only"] = s.getboolean("metadata_only")
        if "exclude" in s:
            config["exclude"] = s.getboolean("exclude")
        if "large_files" in s:
            config["large_files"] = s.getboolean("large_files")
        if "max_size_mb" in s:
            config["max_size_mb"] = int(s["max_size_mb"])
        if cfg_file.exists():
            log.info(f"Dir config loaded: {cfg_file} → {config}")
    return config
```

Then in `crawl_full`, thread the config through:

python

```python
def crawl_full(
    roots: list[Path], exclude: set[Path]
) -> Generator[tuple[Path, dict], None, None]:
    """Yields (path, dir_config) tuples."""
    default_config = {
        "metadata_only": False,
        "exclude":       False,
        "large_files":   False,
        "max_size_mb":   MAX_TEXT_SIZE // 1024 // 1024,
    }
    for root in roots:
        for dirpath, dirs, files in os.walk(str(root), followlinks=False):
            dp = Path(dirpath)
            dir_config = load_dir_config(dp, default_config)

            if dir_config["exclude"]:
                dirs[:] = []    # prune entire subtree
                continue

            dirs[:] = sorted([
                d for d in dirs
                if d not in SKIP_DIRS
                and not d.startswith(".")
                and (dp / d).resolve() not in exclude
            ])
            for f in files:
                yield dp / f, dir_config
```

And `file_to_doc` respects it:

python

```python
def file_to_doc(path: Path, large_files: bool = False,
                dir_config: dict | None = None) -> dict | None:
    cfg = dir_config or {}
    effective_large = large_files or cfg.get("large_files", False)
    effective_max   = cfg.get("max_size_mb", MAX_TEXT_SIZE // 1024 // 1024) * 1024 * 1024
    metadata_only   = cfg.get("metadata_only", False)
    ...
    return {
        ...
        "content": "" if metadata_only else extract_content(
            path,
            large_files=effective_large,
            max_text_size=effective_max,
        ),
    }
```

Then for your NLP corpus directories you just drop in a `.fsearch_config`:

ini

```ini
# /mnt/wd1/GT/NLM_CDE/cde_query/data/.fsearch_config
[fsearch]
metadata_only = true
```

And for a project where you want large files indexed:

ini

```ini
# /mnt/wd1/projects/active_project/.fsearch_config
[fsearch]
large_files = true
max_size_mb = 200
```

No command-line flags needed, no global settings to change — the policy lives next to the data where it makes sense, and you can adjust it per project as your work evolves.

New timeout for a different file 

⠏ Indexed 2014-12-25.csv 351599/?[18:48:30] ERROR Failed to connect to server at http://localhost:8983/solr/filesystem/update/?commitWithin=10000: ('Connection aborted.', TimeoutError('timed out')) pysolr.py:348
 Traceback (most recent call last):
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connectionpool.py", line 787, in urlopen
 response = self._make_request(
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connectionpool.py", line 493, in _make_request
 conn.request(
 File "/home/gerard/.local/lib/python3.10/site-packages/urllib3/connection.py", line 459, in request
 self.send(chunk)
 File "/usr/lib/python3.10/http/client.py", line 1019, in send
 self.sock.sendall(data)
 TimeoutError: timed out

Could one wrap the calls in a way to register an error for a file in the log file while allowing the process to continue?

Show more

6:54 PM

Yes — the timeout is happening at the `solr.add(batch)` call, not during file processing. The fix is to catch the exception at the batch level and either retry individual docs or log and skip the offending batch. Since we don't know which doc in the batch caused the stall, the safest approach is to retry one doc at a time on batch failure:

python

```python
def safe_add(solr: pysolr.Solr, batch: list[dict], dry_run: bool):
    """
    Try to add batch as a whole. On failure, retry one doc at a time
    so a single problematic file doesn't lose the whole batch.
    """
    if dry_run or not batch:
        return
    try:
        solr.add(batch, commitWithin=10000)
    except Exception as e:
        log.warning(f"Batch POST failed ({len(batch)} docs): {e} — retrying individually")
        ok = failed = 0
        for doc in batch:
            try:
                solr.add([doc], commitWithin=10000)
                ok += 1
            except Exception as e2:
                log.error(f"Failed to index {doc.get('filepath', '?')}: {e2}")
                failed += 1
        log.info(f"Batch retry complete: {ok} succeeded, {failed} failed")
```

Then replace all `solr.add(batch, ...)` calls in `run_index` with `safe_add`:

python

```python
        if len(batch) >= BATCH_SIZE:
            safe_add(solr, batch, dry_run)
            batch.clear()

    # final batch
    safe_add(solr, batch, dry_run)
```

This way a single toxic file gets logged by path and skipped, but the other 199 docs in the batch are still indexed. The indexer never aborts.

You might also want a persistent error log separate from the main log for easy post-run review:

python

```python
ERROR_LOG = Path("/mnt/wd1/solr/logs/index_errors.log")

def log_error(filepath: str, reason: str):
    with open(ERROR_LOG, "a") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts}\t{reason}\t{filepath}\n")
```

python

```python
            except Exception as e2:
                log.error(f"Failed to index {doc.get('filepath', '?')}: {e2}")
                log_error(doc.get('filepath', '?'), str(e2))
```

Then after a run:

bash

```bash
# See all files that failed to index
cat /mnt/wd1/solr/logs/index_errors.log

# Just the paths, for targeted retry
awk '{print $3}' /mnt/wd1/solr/logs/index_errors.log
```

And you could add a `--retry-errors` flag later that reads `index_errors.log` and attempts to re-index just those files, but that's a future enhancement — the immediate need is just not losing a full run to one bad file.

we should try the --retry-errors. The concept would have to be read in the log file then move it  (tmp-string, datetime) and allow writing to a new file (same name) thereby catching any new errors. Then deleting the tmp file.

7:04 PM

Clean design — rotate the error log before the run, process from the rotated copy, then let new errors accumulate in the fresh file. Here's the full implementation:

python

```python
# ── Error log ─────────────────────────────────────────────────────────────────

ERROR_LOG = Path(os.environ.get("FSEARCH_ERRORS", "/mnt/wd1/solr/logs/index_errors.log"))

def log_error(filepath: str, reason: str):
    with open(ERROR_LOG, "a") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts}\t{reason}\t{filepath}\n")

def rotate_error_log() -> Path | None:
    """
    Move current error log to a timestamped temp path.
    Returns the rotated path, or None if no error log exists.
    """
    if not ERROR_LOG.exists():
        log.info("No error log found — nothing to retry")
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rotated = ERROR_LOG.with_name(f"{ERROR_LOG.stem}_{ts}.tmp")
    ERROR_LOG.rename(rotated)
    log.info(f"Rotated error log to {rotated} ({sum(1 for _ in open(rotated))} entries)")
    return rotated

def read_error_log(rotated: Path) -> list[str]:
    """Read filepaths from a rotated error log. Skips malformed lines."""
    paths = []
    with open(rotated) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                paths.append(parts[2])   # filepath is third field
            else:
                log.warning(f"Malformed error log line: {line!r}")
    return paths

def cleanup_rotated_log(rotated: Path, had_errors: bool):
    """
    Delete the rotated log if retry was clean.
    If new errors occurred, append the rotated log to the new error log
    so nothing is lost, then delete the rotated copy.
    """
    if had_errors:
        log.info(f"New errors occurred — merging {rotated.name} into {ERROR_LOG.name}")
        with open(ERROR_LOG, "a") as out, open(rotated) as src:
            out.write(f"# --- Carried over from {rotated.name} ---\n")
            out.write(src.read())
    rotated.unlink()
    log.info(f"Rotated log {rotated.name} cleaned up")
```

Then the retry runner in `run_index`:

python

```python
def run_retry(solr_url: str, large_files: bool, dry_run: bool):
    """Re-index files listed in the error log."""
    rotated = rotate_error_log()
    if rotated is None:
        return

    paths = read_error_log(rotated)
    log.info(f"Retrying {len(paths)} previously failed files...")

    solr = pysolr.Solr(solr_url, always_commit=False, timeout=120)
    batch, ok, failed = [], 0, 0

    for filepath in paths:
        path = Path(filepath)
        if not path.exists():
            log.warning(f"No longer exists, skipping: {filepath}")
            log_error(filepath, "file no longer exists at retry time")
            failed += 1
            continue
        doc = file_to_doc(path, large_files=large_files)
        if doc:
            batch.append(doc)
        if len(batch) >= BATCH_SIZE:
            ok_n, fail_n = safe_add(solr, batch, dry_run)
            ok += ok_n
            failed += fail_n
            batch.clear()

    if batch:
        ok_n, fail_n = safe_add(solr, batch, dry_run)
        ok += ok_n
        failed += fail_n

    if not dry_run:
        solr.commit()

    log.info(f"Retry complete: {ok} succeeded, {failed} still failing")
    cleanup_rotated_log(rotated, had_errors=failed > 0)
```

Note `safe_add` needs to return counts now:

python

```python
def safe_add(solr: pysolr.Solr, batch: list[dict], dry_run: bool) -> tuple[int, int]:
    if dry_run or not batch:
        return len(batch), 0
    try:
        solr.add(batch, commitWithin=10000)
        return len(batch), 0
    except Exception as e:
        log.warning(f"Batch POST failed ({len(batch)} docs): {e} — retrying individually")
        ok = failed = 0
        for doc in batch:
            try:
                solr.add([doc], commitWithin=10000)
                ok += 1
            except Exception as e2:
                fp = doc.get("filepath", "?")
                log.error(f"Failed to index {fp}: {e2}")
                log_error(fp, str(e2))
                failed += 1
        log.info(f"Batch retry complete: {ok} succeeded, {failed} failed")
        return ok, failed
```

And the CLI:

python

```python
@click.command()
@click.argument("roots", nargs=-1, required=False)  # optional with --retry-errors
@click.option("--retry-errors", is_flag=True, default=False,
              help="Re-index files from the error log, then clear it")
@click.option("--large-files",  is_flag=True, default=False, ...)
# ... other options unchanged ...
def main(roots, exclude, full, no_purge, dry_run, solr_url, large_files, retry_errors):
    """Crawl ROOT paths and index (or incrementally update) Solr."""
    if retry_errors:
        run_retry(solr_url=solr_url, large_files=large_files, dry_run=dry_run)
        if not roots:
            return   # retry-only mode, no crawl
    if roots:
        run_index(
            roots, exclude,
            incremental=not full,
            no_purge=no_purge,
            solr_url=solr_url,
            dry_run=dry_run,
            large_files=large_files,
        )
```

Making `roots` optional means you can use `--retry-errors` standalone or combined with a normal crawl in one invocation:

bash

```bash
# Retry only
python3 fs_indexer.py --retry-errors

# Retry + incremental crawl in one pass
python3 fs_indexer.py /home/$USER /mnt/wd1/data --retry-errors

# Retry with large file extraction for previously failed large files
python3 fs_indexer.py --retry-errors --large-files

# Check what would be retried without writing
python3 fs_indexer.py --retry-errors --dry-run
```

The error log after a mixed run with some persistent failures will look like:

```
2026-03-25 19:00:01    timed out    /mnt/wd1/GT/data/huge.json
# --- Carried over from index_errors_20260325_190000.tmp ---
2026-03-25 18:48:30    timed out    /mnt/wd1/GT/data/huge.json
```

So you can see if a file is a persistent repeater vs a transient failure.

I see that the find process is in wait-state. Might be useful to write a temp file with the find output (with timestamp) and process any files not yet processed before doing a new file system scan (removing the file if it is older than x hours).

7:10 PM

Good idea — it also gives you crash recovery for free. If the indexer dies mid-run, the next invocation picks up from the existing find output rather than re-scanning the filesystem.

python

```python
FIND_CACHE     = Path(os.environ.get("FSEARCH_FIND_CACHE", "/mnt/wd1/solr/find_cache.txt"))
FIND_CACHE_MAX = int(os.environ.get("FSEARCH_FIND_CACHE_MAX_HOURS", "12"))  # hours

def find_cache_valid() -> bool:
    """True if cache exists and is younger than FIND_CACHE_MAX hours."""
    if not FIND_CACHE.exists():
        return False
    age_hours = (time.time() - FIND_CACHE.stat().st_mtime) / 3600
    if age_hours > FIND_CACHE_MAX:
        log.info(f"Find cache expired ({age_hours:.1f}h old, max {FIND_CACHE_MAX}h) — will rescan")
        return False
    log.info(f"Find cache valid ({age_hours:.1f}h old) — resuming from {FIND_CACHE}")
    return True

def write_find_cache(roots: list[Path], since_ts: float | None,
                     exclude: set[Path]):
    """
    Run find and write results to cache file, one path per line.
    Uses find -newer for incremental, plain find for full.
    """
    import subprocess
    import tempfile

    log.info(f"Running filesystem scan → {FIND_CACHE}")
    FIND_CACHE.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file first — avoid partial cache on interruption
    tmp = FIND_CACHE.with_suffix(".tmp")

    with open(tmp, "w") as out:
        # Write header: timestamp + mode
        out.write(f"# fsearch find cache\n")
        out.write(f"# written: {datetime.datetime.utcnow().isoformat()}\n")
        out.write(f"# mode: {'incremental' if since_ts else 'full'}\n")

        if since_ts:
            # Incremental — use find -newer reference file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ts") as tf:
                tf_path = tf.name
            os.utime(tf_path, (since_ts, since_ts))
            try:
                for root in roots:
                    exclude_args = []
                    for ex in exclude:
                        exclude_args += ["-path", str(ex), "-prune", "-o"]
                    cmd = ["find", str(root)] + exclude_args + \
                          ["-newer", tf_path, "-type", "f", "-print0"]
                    result = subprocess.run(cmd, capture_output=True)
                    for fp in result.stdout.split(b"\0"):
                        if fp:
                            out.write(fp.decode("utf-8", errors="replace") + "\n")
            finally:
                os.unlink(tf_path)
        else:
            # Full crawl — os.walk for SKIP_DIRS handling
            for root in roots:
                for dirpath, dirs, files in os.walk(str(root), followlinks=False):
                    dp = Path(dirpath)
                    dirs[:] = sorted([
                        d for d in dirs
                        if d not in SKIP_DIRS
                        and not d.startswith(".")
                        and (dp / d).resolve() not in exclude
                    ])
                    for f in files:
                        out.write(str(dp / f) + "\n")

    tmp.rename(FIND_CACHE)   # atomic replace
    count = sum(1 for l in open(FIND_CACHE) if not l.startswith("#"))
    log.info(f"Find cache written: {count} files → {FIND_CACHE}")


def read_find_cache() -> Generator[Path, None, None]:
    """
    Yield paths from cache. Skips comment lines and already-processed
    marker lines (prefixed with #done:).
    """
    with open(FIND_CACHE) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            yield Path(line)


def mark_done_in_cache(filepath: str):
    """
    Not practical to rewrite per-file — instead track a done-count
    in a sidecar file for progress recovery.
    """
    pass   # see checkpoint approach below
```

The per-line rewrite on every file is too expensive. Better to use a sidecar checkpoint file tracking how many lines have been processed — on resume, skip that many:

python

```python
FIND_CACHE_CHECKPOINT = FIND_CACHE.with_suffix(".checkpoint")

def read_checkpoint() -> int:
    """Return number of files already processed in current cache."""
    if FIND_CACHE_CHECKPOINT.exists():
        try:
            return int(FIND_CACHE_CHECKPOINT.read_text().strip())
        except ValueError:
            pass
    return 0

def write_checkpoint(n: int):
    FIND_CACHE_CHECKPOINT.write_text(str(n))

def clear_checkpoint():
    if FIND_CACHE_CHECKPOINT.exists():
        FIND_CACHE_CHECKPOINT.unlink()
```

Then the crawler in `run_index` becomes:

python

```python
def crawl_from_cache() -> Generator[Path, None, None]:
    """Yield paths from cache, skipping already-processed files."""
    skip = read_checkpoint()
    if skip > 0:
        log.info(f"Resuming from checkpoint: skipping first {skip} files")
    count = 0
    for path in read_find_cache():
        if count < skip:
            count += 1
            continue
        yield path
        count += 1


def run_index(roots, exclude_paths, incremental, no_purge,
              solr_url, dry_run, large_files):

    state    = load_state()
    solr     = pysolr.Solr(solr_url, always_commit=False, timeout=120)
    since_ts = None

    if incremental:
        last = state.get("last_run")
        if last:
            since_ts = datetime.datetime.fromisoformat(last).timestamp()
            log.info(f"Incremental mode: files newer than {last}")
        else:
            log.info("No previous run found — falling back to full index")

    exclude = {Path(e).resolve() for e in exclude_paths}
    roots_p = [Path(r) for r in roots]

    # ── Find phase ────────────────────────────────────────────────────────────
    if find_cache_valid():
        log.info("Using existing find cache")
    else:
        clear_checkpoint()   # new scan = reset checkpoint
        write_find_cache(roots_p, since_ts, exclude)

    # ── Save state at start so interruptions leave a checkpoint ───────────────
    run_start = datetime.datetime.utcnow()
    if not dry_run:
        state["last_run"] = run_start.isoformat()
        save_state(state)

    # ── Index phase ───────────────────────────────────────────────────────────
    batch, total, skipped, ok_total, fail_total = [], 0, 0, 0, 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        MofNCompleteColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Indexing files...", total=None)
        for path in crawl_from_cache():
            doc = file_to_doc(path, large_files=large_files)
            if doc:
                batch.append(doc)
                total += 1
            else:
                skipped += 1

            if len(batch) >= BATCH_SIZE:
                ok_n, fail_n = safe_add(solr, batch, dry_run)
                ok_total += ok_n
                fail_total += fail_n
                batch.clear()
                # Checkpoint after each successful batch
                if not dry_run:
                    write_checkpoint(total + skipped)

            prog.update(
                task, completed=total,
                description=f"[cyan]Indexed[/cyan] {path.name[:50]}",
            )

    if batch:
        ok_n, fail_n = safe_add(solr, batch, dry_run)
        ok_total += ok_n
        fail_total += fail_n

    if not dry_run:
        solr.commit()
        if not no_purge:
            purge_deleted(solr)
        clear_checkpoint()        # clean run — remove checkpoint
        FIND_CACHE.unlink(missing_ok=True)   # remove cache after success

    state["indexed_count"] = state.get("indexed_count", 0) + ok_total
    if not dry_run:
        save_state(state)

    log.info(
        f"[green]Done.[/green] Indexed: {ok_total}, "
        f"Skipped: {skipped}, Errors: {fail_total}"
    )
```

The failure/resume flow is now:

```
Run 1:  find scan → cache written (150k paths)
        index proceeds → timeout at 130k
        checkpoint written: 129899
        cache still exists, < 12h old

Run 2:  cache valid → skip find scan entirely
        resume from checkpoint 129899
        continue from where it left off
        clean finish → cache + checkpoint deleted
```

And the cache files on disk:

```
/mnt/wd1/solr/find_cache.txt          # one path per line
/mnt/wd1/solr/find_cache.checkpoint   # integer: lines processed so far
```

bash

```bash
# Check cache status
wc -l /mnt/wd1/solr/find_cache.txt
cat /mnt/wd1/solr/find_cache.checkpoint

# Force a fresh scan (ignore valid cache)
rm /mnt/wd1/solr/find_cache.txt
python3 fs_indexer.py /home/$USER /mnt/wd1/data
```

Python error in fsearch

if op == ">": return f"size_bytes:{{{bytes_val} TO *]}"
 if op == ">=": return f"size_bytes:[{bytes_val} TO *]"
 if op == "<": return f"size_bytes:[* TO {bytes_val}}}}"
 if op == "<=": return f"size_bytes:[* TO {bytes_val}]"
 return f"size_bytes:{bytes_val}"

File "/home/gerard/opt/fsearch/fsearch.py", line 40
 if op == ">": return f"size_bytes:{{{bytes_val} TO *]}"
 ^
SyntaxError: f-string: single '}' is not allowed

8:29 PM

The Solr range query syntax uses mixed brackets (`{` for exclusive, `[` for inclusive) which collides with f-string escaping. The exclusive end needs `}}` to produce a literal `}` in the f-string:

python

```python
def parse_size(s: str) -> str:
    m = re.match(r'([><]=?)\s*(\d+\.?\d*)\s*(B|KB|MB|GB|TB)?', s, re.I)
    if not m:
        raise ValueError(f"Invalid size expression: {s!r}  (examples: >10MB, <=500KB, >1GB)")
    op, val, unit = m.groups()
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}.get(
        (unit or "b").lower(), 1
    )
    bv = int(float(val) * mult)
    if op == ">":  return f"size_bytes:{{{bv} TO *]"
    if op == ">=": return f"size_bytes:[{bv} TO *]"
    if op == "<":  return f"size_bytes:[* TO {bv}}}"
    if op == "<=": return f"size_bytes:[* TO {bv}]"
    return f"size_bytes:{bv}"
```

The escaping rules for Solr range brackets in f-strings:

| Solr syntax           | f-string encoding | Produces |
| --------------------- | ----------------- | -------- |
| `{` (exclusive lower) | `{{`              | `{`      |
| `}` (exclusive upper) | `}}`              | `}`      |
| `[` (inclusive lower) | `[`               | `[`      |
| `]` (inclusive upper) | `]`               | `]`      |

So `>{bv} TO *]` needs `{{{bv} TO *]` — three braces: `{{` for literal `{`, then `{bv}` for the value. And `[* TO {bv}}` needs `}}` at the end for the literal `}`.

Not disagreeing but python is not happy:
fsearch.py --name "*.vcf" --size ">100MB"
 File "/home/gerard/opt/fsearch/fsearch.py", line 40
 if op == ">": return f"size_bytes:{{{bytes_val} TO *]}"
 ^
SyntaxError: f-string: single '}' is not allowed

8:30 PM

The issue is the trailing `}"` — Python sees the `}` after `*]` as an unmatched f-string brace. The closing `}` of the f-string delimiter is being confused with a literal. Simplest fix is to avoid f-strings entirely for these and use `.format()` or concatenation:

python

```python
def parse_size(s: str) -> str:
    m = re.match(r'([><]=?)\s*(\d+\.?\d*)\s*(B|KB|MB|GB|TB)?', s, re.I)
    if not m:
        raise ValueError(f"Invalid size expression: {s!r}")
    op, val, unit = m.groups()
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}.get(
        (unit or "b").lower(), 1
    )
    bv = int(float(val) * mult)
    if op == ">":  return "size_bytes:{" + str(bv) + " TO *]"
    if op == ">=": return "size_bytes:[" + str(bv) + " TO *]"
    if op == "<":  return "size_bytes:[* TO " + str(bv) + "}"
    if op == "<=": return "size_bytes:[* TO " + str(bv) + "]"
    return "size_bytes:" + str(bv)
```

String concatenation sidesteps the f-string brace escaping problem entirely — much cleaner when the target string itself contains braces.

search without argument:
...
 /mnt/wd1/GT/NLM_CDE/cde_python/py310_cluster/lib/python3.10/site-packages/pandas/tests/io/excel/test_writers.py py 53.7KB 2025-12-15
 /mnt/wd1/GT/NLM_CDE/cde_python/py310_cluster/lib/python3.10/site-packages/pandas/tests/io/excel/test_openpyxl.py py 14.9KB 2025-12-15
Found 351551 total results (showing 50)

Odd that 351551 is far fewer than the 550k+ in one of the directories indexed

8:37 PM

Two separate issues:

**1. 351551 is the current indexed count** — the indexer hasn't finished yet (it timed out at ~130k, resumed, and is still running). The count will grow as indexing proceeds and commits fire.

**2. The default sort is `score desc`** — with no query text, all docs score equally, so Solr returns an arbitrary 50. The results you're seeing aren't necessarily representative of what's indexed. To get a true count:

bash

```bash
curl -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'], 'docs indexed')"
```

**3. The 550k discrepancy** — a few possible reasons:

bash

```bash
# How many files does find actually see?
find /mnt/wd1/GT -type f | wc -l

# How many are in Solr for that directory?
curl -s "http://localhost:8983/solr/filesystem/select?q=directory:/mnt/wd1/GT*&rows=0" \
  | python3 -m json.tool | grep numFound

# Are virtual env files dominating the count?
find /mnt/wd1 -path "*/site-packages/*" -type f | wc -l
```

That pandas test file path suggests virtualenvs are being indexed — `py310_cluster/lib/python3.10/site-packages` alone could contain tens of thousands of files that aren't useful to search. Worth adding to excludes:

bash

```bash
python3 fs_indexer.py /mnt/wd1 \
    --exclude /mnt/wd1/GT/NLM_CDE/cde_python/py310_cluster \
    --exclude /home/$USER/.local/lib
```

Or more generally, add `site-packages` to `SKIP_DIRS` in the indexer:

python

```python
SKIP_DIRS = {
    ...
    "site-packages",    # Python virtualenvs
    "lib",              # too broad alone, but combined below
}
```

Though `lib` is too broad — better to add the full virtualenv pattern as an exclude argument or use the `.fsearch_config` mechanism in the venv root:

ini

```ini
# /mnt/wd1/GT/NLM_CDE/cde_python/py310_cluster/.fsearch_config
[fsearch]
exclude = true
```

Indexer completed 
 INFO Finished 'http://localhost:8983/solr/filesystem/update/?commit=true' (post) with body '<commit />' in 0.006 seconds, with status 200 pysolr.py:356
 INFO Delete pass complete. Total purged: 0 fs_indexer.py:235
 INFO Done. Indexed: 0, Skipped: 0

8:38 PM

`Indexed: 0, Skipped: 0` with a successful commit means the indexer ran but found no files to process — which makes sense because it's running in incremental mode and `last_run` was saved at the **start** of the previous run (the fix we made earlier). So `find -newer` finds nothing newer than that timestamp.

The delete pass showing 0 purged is fine — nothing was deleted from the filesystem.

Check the state file:

bash

```bash
cat /mnt/wd1/solr/indexer_state.json
```

It will show a `last_run` timestamp from the start of the previous run. And check the find cache:

bash

```bash
# Was cache still present and empty?
wc -l /mnt/wd1/solr/find_cache.txt 2>/dev/null || echo "cache gone"
cat /mnt/wd1/solr/find_cache.checkpoint 2>/dev/null || echo "no checkpoint"
```

The likely sequence:

1. Previous run saved `last_run` at start → timestamp T
2. Previous run completed → cache deleted, checkpoint cleared
3. This run: `find -newer T` finds nothing because T is recent
4. Empty crawl → 0 indexed

This is actually correct behavior — the incremental is working as designed. The total indexed count in Solr is whatever accumulated across all the previous runs. Confirm:

bash

```bash
curl -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'], 'docs indexed')"
```

That number is your true indexed count. What does it show?

url -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" \
 | python3 -c "import sys,json; print(json.load(sys.stdin)['response']['numFound'], 'docs indexed')"
351551 docs indexed

8:40 PM

351,551 docs — that's the count from when the aindexer last timed out and didn't complete the full crawl. The find cache approach means the next run will resume from the checkpoint rather than rescan, but since the run completed cleanly (just with 0 new files), the cache was deleted.

You need a full re-index to pick up the remaining files:

bash

```bash
python3 ~/opt/fsearch/fs_indexer.py \a
    /home/$USER /mnt/wd1 \
    --full --no-purge \
    --exclude /home/$USER/.cache \
    --exclude /mnt/wd1/GT/NLM_CDE/cde_python/py310_cluster
```

`--no-purge` skips the delete pass since nothing has been deleted — saves time on a large corpus.

The underlying issue is that when the run timed out mid-crawl, `last_run` had already been saved to the start timestamp. So the subsequent incremental run correctly found nothing new — but the files between the timeout point (~130k) and the end of the filesystem (~550k+) were never indexed.

Going forward, once you have a complete full index, the daily incremental will work correctly — it only needs to find files modified since the previous day, which will be a small set and unlikely to hit timeouts. The full index is the painful one-time cost.

Also worth adding the virtualenv excludes before the full run to avoid re-indexing thousands of `site-packages` files:

bash

```bash
# Find all virtualenv roots on the system first
find /mnt/wd1 -name "site-packages" -type d 2>/dev/null | head -20
find /home/$USER -name "site-packages" -type d 2>/dev/null | head -20
```

Then add each as `--exclude` to keep the index lean and the run faster.

#!/usr/bin/env python3
"""
fsearch — Query Solr filesystem index

Usage examples:
  fsearch "BRCA1"
  fsearch --name "*.vcf" --since 2024-01-01
  fsearch --content "p.value < 0.05" --ext py,r,R
  fsearch --size ">10MB" --before 2023-06-01
  fsearch --path NLM_CDE --ext py
  fsearch --query 'content:GATK AND filename:*hg38* AND size_bytes:[1000 TO *]'

Boolean combinations:
  fsearch --name "*.py" --name "*.r"               # OR: .py or .r files
  fsearch --path NLM_CDE --not-ext log,out         # path match, exclude extensions
  fsearch --content pandas --not-path site-packages # content match, exclude venvs
  fsearch --name "*.csv" --or --name "*.tsv" --path data  # (csv OR tsv) with 'data' in path
  fsearch --ext py --not-name "test_*"             # Python files, not tests
  fsearch --show-query --name "*.py" --path work   # show generated Solr query
"""

import os
import re
import sys
import csv
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


def parse_date(s: str) -> str:
    """Return Solr datetime string from YYYY-MM-DD."""
    return datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")


def glob_to_solr(pattern: str) -> str:
    """Convert shell glob to Solr wildcard query fragment."""
    # Solr supports * and ? wildcards natively
    return pattern.replace(" ", "\\ ")


def _text_clause(field: str, value: str) -> str:
    """Build a clause for a text_general field (supports /regex/ and Boolean)."""
    if value.startswith("/") and value.endswith("/") and len(value) > 2:
        return f"{field}:/{value[1:-1]}/"
    return f"{field}:({value})"


def _path_clause(value: str) -> str:
    """Build a filepath clause (supports /regex/, glob, or auto-wrapped substring)."""
    if value.startswith("/") and value.endswith("/") and len(value) > 2:
        return f"filepath:/{value[1:-1]}/"
    if "*" not in value and "?" not in value:
        value = f"*{value}*"
    return f"filepath:{glob_to_solr(value)}"


def _name_clause(value: str) -> str:
    """Build a filename clause."""
    return f"filename:{glob_to_solr(value)}"


def _dir_clause(value: str) -> str:
    """Build a directory clause. Trailing / means prefix match."""
    if value.endswith("/"):
        return f"filepath:{glob_to_solr(value)}*"
    return f'directory:"{value}"'


def _or_group(clauses: list[str]) -> str:
    """Wrap multiple clauses in an OR group, or return single clause as-is."""
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " OR ".join(clauses) + ")"


def build_query(args) -> tuple[str, dict]:
    clauses = []    # positive (must match)
    negations = []  # negative (must NOT match)
    params = {"fl": "filepath,filename,size_bytes,mtime,extension,directory,content_sha256",
              "rows": args.limit,
              "sort": args.sort}

    if args.query:
        # Raw Solr/Lucene query passthrough
        clauses.append(args.query)
    else:
        # ── Text (positional arg) ────────────────────────────────────────
        if args.text:
            clauses.append(_text_clause("_text_", args.text))

        # ── Name (repeatable, OR within) ─────────────────────────────────
        if args.name:
            clauses.append(_or_group([_name_clause(n) for n in args.name]))

        if args.not_name:
            for n in args.not_name:
                negations.append(_name_clause(n))

        # ── Extension (comma-separated within each value, OR across) ─────
        if args.ext:
            ext_parts = []
            for e_arg in args.ext:
                for e in e_arg.split(","):
                    ext_parts.append(f"extension:{e.lstrip('.')}")
            clauses.append(_or_group(ext_parts))

        if args.not_ext:
            for e_arg in args.not_ext:
                for e in e_arg.split(","):
                    negations.append(f"extension:{e.lstrip('.')}")

        # ── Directory (repeatable, OR within) ────────────────────────────
        if args.dir:
            clauses.append(_or_group([_dir_clause(d) for d in args.dir]))

        if args.not_dir:
            for d in args.not_dir:
                negations.append(_dir_clause(d))

        # ── Path (repeatable, OR within) ─────────────────────────────────
        if args.path:
            clauses.append(_or_group([_path_clause(p) for p in args.path]))

        if args.not_path:
            for p in args.not_path:
                negations.append(_path_clause(p))

        # ── Content (repeatable, OR within) ──────────────────────────────
        if args.content:
            clauses.append(
                _or_group([_text_clause("content", c) for c in args.content]))

        if args.not_content:
            for c in args.not_content:
                negations.append(_text_clause("content", c))

        # ── Size ─────────────────────────────────────────────────────────
        if args.size:
            clauses.append(parse_size(args.size))

        # ── Date range ───────────────────────────────────────────────────
        if args.since:
            dt = parse_date(args.since)
            clauses.append(f"mtime:[{dt} TO *]")

        if args.before:
            dt = parse_date(args.before)
            clauses.append(f"mtime:[* TO {dt}]")

    # ── Assemble query ───────────────────────────────────────────────────
    joiner = " OR " if args.use_or else " AND "
    q = joiner.join(clauses) if clauses else "*:*"

    # Append negations (always ANDed, regardless of --or)
    for neg in negations:
        q = f"({q}) AND NOT {neg}" if q != "*:*" else f"NOT {neg}"

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


EXPORT_COLUMNS = ["filepath", "filename", "extension", "size_bytes",
                  "mtime", "directory", "content_sha256"]


def _resolve_export_format(args) -> str:
    """Return explicit --format or infer from --export file extension."""
    if args.format:
        return args.format.lower()
    ext = Path(args.export).suffix.lower().lstrip(".")
    if ext in ("csv", "txt", "json"):
        return ext
    raise ValueError(
        f"Cannot infer export format from {args.export!r}; "
        f"use --format csv|txt|json")


def export_results(results, args) -> None:
    """Write results to a file in csv, txt, or json format."""
    fmt = _resolve_export_format(args)
    out_path = Path(args.export).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    docs = list(results)

    if fmt == "txt":
        with out_path.open("w", encoding="utf-8") as f:
            for doc in docs:
                f.write(doc.get("filepath", "") + "\n")

    elif fmt == "csv":
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS,
                                    extrasaction="ignore")
            writer.writeheader()
            for doc in docs:
                row = {c: doc.get(c, "") for c in EXPORT_COLUMNS}
                # Solr multi-valued fields come back as lists
                for k, v in row.items():
                    if isinstance(v, list):
                        row[k] = v[0] if v else ""
                writer.writerow(row)

    elif fmt == "json":
        import json
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(docs, f, indent=2, default=str)

    else:
        raise ValueError(f"Unknown export format: {fmt}")

    if not args.quiet:
        console.print(
            f"[green]Exported[/green] {len(docs)} of {results.hits} "
            f"results to [cyan]{out_path}[/cyan] ([yellow]{fmt}[/yellow])")
        if results.hits > len(docs):
            console.print(
                f"[yellow]Note:[/yellow] result limit is {args.limit}; "
                f"raise with --limit to export more.")


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
    # ── Positional ───────────────────────────────────────────────────────
    ap.add_argument("text", nargs="?",
                    help="Free text / regex (/pattern/) / Boolean query")

    # ── Repeatable match filters (multiple = OR within same field) ───────
    ap.add_argument("-n", "--name",    action="append",
                    help="Filename glob (repeatable, OR'd): -n '*.py' -n '*.r'")
    ap.add_argument("-e", "--ext",     action="append",
                    help="Extensions (comma-sep within, repeatable): -e py,r -e sh")
    ap.add_argument("-d", "--dir",     action="append",
                    help="Directory (exact, or trailing / for prefix; repeatable)")
    ap.add_argument("-p", "--path",    action="append",
                    help="Full filepath (glob/*auto*, /regex/; repeatable)")
    ap.add_argument("-c", "--content", action="append",
                    help="File content (supports /regex/; repeatable)")

    # ── Negation filters (always AND NOT) ────────────────────────────────
    ap.add_argument("-N", "--not-name",    action="append", default=[],
                    help="Exclude filenames matching glob")
    ap.add_argument("-E", "--not-ext",     action="append", default=[],
                    help="Exclude extensions (comma-sep within, repeatable)")
    ap.add_argument("-D", "--not-dir",     action="append", default=[],
                    help="Exclude directory")
    ap.add_argument("-P", "--not-path",    action="append", default=[],
                    help="Exclude filepath matching glob/*auto*")
    ap.add_argument("-C", "--not-content", action="append", default=[],
                    help="Exclude files with content matching term")

    # ── Boolean control ──────────────────────────────────────────────────
    ap.add_argument("--or", dest="use_or", action="store_true", default=False,
                    help="Join clause groups with OR instead of AND")

    # ── Size / date ──────────────────────────────────────────────────────
    ap.add_argument("-s", "--size",    help="Size filter: >10MB, <1GB, >=500KB")
    ap.add_argument("--since",        help="Modified after YYYY-MM-DD")
    ap.add_argument("--before",       help="Modified before YYYY-MM-DD")

    # ── Raw / output ─────────────────────────────────────────────────────
    ap.add_argument("-q", "--query",  help="Raw Solr/Lucene query string (expert)")
    ap.add_argument("-l", "--limit",  type=int, default=50, help="Max results [50]")
    ap.add_argument("--sort",         default="score desc",
                    help="Sort: score desc | mtime desc | size_bytes asc | filename asc")
    ap.add_argument("--highlight",    action="store_true", default=True,
                    help="Show content snippets (default: on)")
    ap.add_argument("--no-highlight", dest="highlight", action="store_false")
    ap.add_argument("--quiet",  "-Q", action="store_true",
                    help="Print paths only (for piping)")
    ap.add_argument("--json",         dest="jsonout", action="store_true",
                    help="JSON output")
    ap.add_argument("-o", "--export", metavar="FILE",
                    help="Export results to FILE (format inferred from "
                         "extension: .csv, .txt, .json)")
    ap.add_argument("--format",       choices=["csv", "txt", "json"],
                    help="Explicit export format (overrides extension). "
                         "csv=all columns, txt=filepath only")
    ap.add_argument("--show-query",   action="store_true", default=False,
                    help="Print the generated Solr query (debug)")
    ap.add_argument("--solr-url",     default=SOLR_URL, help="Solr URL")

    args = ap.parse_args()

    solr = pysolr.Solr(args.solr_url, timeout=15)
    q, params = build_query(args)

    if args.show_query:
        console.print(f"[dim]Query:[/dim] {q}")

    try:
        results = solr.search(q, **params)
    except pysolr.SolrError as e:
        console.print(f"[red]Solr error:[/red] {e}")
        sys.exit(1)

    if args.export:
        try:
            export_results(results, args)
        except ValueError as e:
            console.print(f"[red]Export error:[/red] {e}")
            sys.exit(2)
        return

    highlights = getattr(results, "highlighting", {})
    display_results(results, highlights, args)


if __name__ == "__main__":
    main()

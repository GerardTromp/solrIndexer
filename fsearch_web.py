#!/usr/bin/env python3
"""
fsearch_web.py — Web GUI for Solr filesystem search

Launch:
    python fsearch_web.py [--port 8080] [--solr-url http://localhost:8983/solr/filesystem]

Provides:
    GET  /              — search UI
    POST /api/search    — execute search, return JSON results
    POST /api/content   — fetch first paragraph of a file's indexed content
"""

import os, re, csv, io, datetime, json, argparse
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, Response
import pysolr

EXPORT_COLUMNS = ["filepath", "filename", "extension", "size_bytes",
                  "mtime", "directory", "content_sha256",
                  "language", "mimetype_detected"]
EXPORT_MAX_ROWS = 100_000

SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr/filesystem")
HERE = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)

# ── Shared query-building helpers (mirrors fsearch.py logic) ─────────────────

def parse_size(s: str) -> str:
    m = re.match(r'([><]=?)\s*(\d+\.?\d*)\s*(B|KB|MB|GB|TB)?', s, re.I)
    if not m:
        raise ValueError(f"Invalid size expression: {s!r}")
    op, val, unit = m.groups()
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}.get(
        (unit or "b").lower(), 1)
    bv = int(float(val) * mult)
    if op == ">":  return "size_bytes:{" + str(bv) + " TO *]"
    if op == ">=": return "size_bytes:[" + str(bv) + " TO *]"
    if op == "<":  return "size_bytes:[* TO " + str(bv) + "}"
    if op == "<=": return "size_bytes:[* TO " + str(bv) + "]"
    return "size_bytes:" + str(bv)


def parse_date(s: str) -> str:
    return datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")


def glob_to_solr(pattern: str) -> str:
    return pattern.replace(" ", "\\ ")


def _text_clause(field: str, value: str) -> str:
    if value.startswith("/") and value.endswith("/") and len(value) > 2:
        return f"{field}:/{value[1:-1]}/"
    return f"{field}:({value})"


def _path_clause(value: str) -> str:
    if value.startswith("/") and value.endswith("/") and len(value) > 2:
        return f"filepath:/{value[1:-1]}/"
    if "*" not in value and "?" not in value:
        value = f"*{value}*"
    return f"filepath:{glob_to_solr(value)}"


def _name_clause(value: str) -> str:
    return f"filename:{glob_to_solr(value)}"


def _dir_clause(value: str) -> str:
    if value.endswith("/"):
        return f"filepath:{glob_to_solr(value)}*"
    return f'directory:"{value}"'


def _or_group(clauses: list[str]) -> str:
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " OR ".join(clauses) + ")"


def _clause_for_row(field: str, value: str) -> str:
    """Build a single Solr clause from a field name and value."""
    if field == "text":
        return _text_clause("_text_", value)
    elif field == "name":
        return _name_clause(value)
    elif field == "ext":
        parts = [f"extension:{e.strip().lstrip('.')}" for e in value.split(",")]
        return _or_group(parts)
    elif field == "dir":
        return _dir_clause(value)
    elif field == "path":
        return _path_clause(value)
    elif field == "content":
        return _text_clause("content", value)
    elif field == "size":
        return parse_size(value)
    elif field == "since":
        dt = parse_date(value)
        return f"mtime:[{dt} TO *]"
    elif field == "before":
        dt = parse_date(value)
        return f"mtime:[* TO {dt}]"
    elif field == "raw":
        return value
    else:
        raise ValueError(f"Unknown field: {field}")


def build_query_from_rows(rows: list[dict], default_join: str = "AND") -> str:
    """
    Build a Solr query from a list of row dicts.
    Each row: {field, value, negate, join}
      - join: "AND" | "OR" | "NOT" (the operator BEFORE this row)
      - negate: bool (legacy, also supported — wraps as NOT)

    Rows are processed in order. NOT rows are collected and appended at the end
    as AND NOT blocks (regardless of their position — the UI lets users reorder
    to visualise this, but the query builder enforces it).
    """
    positives = []   # (join, clause)
    negations = []   # clause strings

    for row in rows:
        field = row.get("field", "").strip()
        value = row.get("value", "").strip()
        if not field or not value:
            continue

        clause = _clause_for_row(field, value)
        is_negated = row.get("negate", False) or row.get("join") == "NOT"

        if is_negated:
            negations.append(clause)
        else:
            join = row.get("join", default_join).upper()
            if join == "NOT":
                join = "AND"
            positives.append((join, clause))

    # Assemble positives
    if not positives and not negations:
        return "*:*"

    parts = []
    for i, (join, clause) in enumerate(positives):
        if i == 0:
            parts.append(clause)
        else:
            parts.append(f" {join} {clause}")
    q = "".join(parts) if parts else "*:*"

    # Append negations
    for neg in negations:
        if q == "*:*":
            q = f"NOT {neg}"
        else:
            q = f"({q}) AND NOT {neg}"

    return q


# ── API endpoints ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(HERE / "static"), "search.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json(force=True)
    rows = body.get("rows", [])
    limit = min(int(body.get("limit", 50)), 500)
    sort = body.get("sort", "score desc")
    highlight = body.get("highlight", True)

    try:
        q = build_query_from_rows(rows)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    solr = pysolr.Solr(body.get("solr_url", SOLR_URL), timeout=15)

    params = {
        "fl": "filepath,filename,size_bytes,mtime,extension,directory,content_preview,content_sha256,language,mimetype_detected",
        "rows": limit,
        "sort": sort,
    }
    if highlight:
        params.update({
            "hl": "true",
            "hl.fl": "content,filename",
            "hl.snippets": 3,
            "hl.fragsize": 200,
            "hl.simple.pre": "<mark>",
            "hl.simple.post": "</mark>",
        })

    try:
        results = solr.search(q, **params)
    except pysolr.SolrError as e:
        return jsonify({"error": f"Solr error: {e}"}), 502

    docs = []
    highlights = getattr(results, "highlighting", {}) or {}
    for doc in results:
        fp = doc.get("filepath", "")
        d = dict(doc)
        d["highlights"] = highlights.get(fp, {})
        docs.append(d)

    return jsonify({
        "query": q,
        "total": results.hits,
        "docs": docs,
    })


@app.route("/api/export", methods=["POST"])
def api_export():
    """
    Export filtered file listing as csv, txt, or json.

    Body: {rows, format, limit, sort, solr_url}
      - format: "csv" | "txt" | "json"
      - limit:  max docs to export (capped at EXPORT_MAX_ROWS)
    """
    body = request.get_json(force=True)
    rows = body.get("rows", [])
    fmt = (body.get("format") or "csv").lower()
    if fmt not in ("csv", "txt", "json"):
        return jsonify({"error": f"invalid format: {fmt}"}), 400

    limit = min(int(body.get("limit", 10_000)), EXPORT_MAX_ROWS)
    sort = body.get("sort", "score desc")

    try:
        q = build_query_from_rows(rows)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    solr = pysolr.Solr(body.get("solr_url", SOLR_URL), timeout=60)

    try:
        results = solr.search(q, fl=",".join(EXPORT_COLUMNS),
                              rows=limit, sort=sort)
    except pysolr.SolrError as e:
        return jsonify({"error": f"Solr error: {e}"}), 502

    docs = list(results)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "txt":
        body_text = "\n".join(d.get("filepath", "") for d in docs) + "\n"
        return Response(
            body_text, mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="fsearch_{ts}.txt"'})

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for doc in docs:
            row = {c: doc.get(c, "") for c in EXPORT_COLUMNS}
            for k, v in row.items():
                if isinstance(v, list):
                    row[k] = v[0] if v else ""
            writer.writerow(row)
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="fsearch_{ts}.csv"'})

    # json
    return Response(
        json.dumps(docs, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="fsearch_{ts}.json"'})


@app.route("/api/duplicates", methods=["POST"])
def api_duplicates():
    """
    Find duplicate files by content_sha256.

    Body (either form):
      {"hash": "<sha256 hex>"}         → return all docs with that hash
      {"min_count": 2, "limit": 100}   → return groups sharing a hash
    """
    body = request.get_json(force=True)
    solr = pysolr.Solr(body.get("solr_url", SOLR_URL), timeout=30)

    # Mode 1: lookup by specific hash
    if body.get("hash"):
        h = body["hash"].strip().lower()
        try:
            results = solr.search(
                f'content_sha256:"{h}"',
                fl="filepath,filename,size_bytes,mtime,extension,directory,content_sha256",
                rows=body.get("limit", 200),
                sort="filepath asc")
        except pysolr.SolrError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"hash": h, "total": results.hits, "docs": list(results)})

    # Mode 2: enumerate duplicate groups via facet
    min_count = int(body.get("min_count", 2))
    limit = int(body.get("limit", 100))
    try:
        results = solr.search(
            "content_sha256:[* TO *]",
            rows=0,
            **{
                "facet": "true",
                "facet.field": "content_sha256",
                "facet.mincount": min_count,
                "facet.limit": limit,
                "facet.sort": "count",
            })
    except pysolr.SolrError as e:
        return jsonify({"error": str(e)}), 502

    facet = results.facets.get("facet_fields", {}).get("content_sha256", [])
    # Solr returns [h1, count1, h2, count2, ...]
    groups = [{"hash": facet[i], "count": facet[i + 1]}
              for i in range(0, len(facet), 2)]
    return jsonify({"groups": groups, "total_groups": len(groups)})


@app.route("/api/content", methods=["POST"])
def api_content():
    """Fetch the stored 1KB content preview for a file."""
    body = request.get_json(force=True)
    filepath = body.get("filepath", "")
    if not filepath:
        return jsonify({"error": "filepath required"}), 400

    solr = pysolr.Solr(body.get("solr_url", SOLR_URL), timeout=15)

    try:
        results = solr.search(f'id:"{filepath}"',
                              fl="content_preview", rows=1)
    except pysolr.SolrError as e:
        return jsonify({"error": str(e)}), 502

    if not results:
        return jsonify({"content": "(not indexed or no content extracted)"})

    content = list(results)[0].get("content_preview", "")
    return jsonify({"content": content or "(no content extracted)"})


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="fsearch web GUI")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--solr-url", default=SOLR_URL)
    args = ap.parse_args()
    SOLR_URL = args.solr_url
    print(f"fsearch web UI → http://{args.host}:{args.port}")
    print(f"Solr backend   → {SOLR_URL}")
    app.run(host=args.host, port=args.port, debug=True)

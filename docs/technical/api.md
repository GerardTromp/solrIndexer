# HTTP API

`fsearch_web.py` exposes a small JSON API backing the web GUI. All
endpoints are POST with JSON bodies; `Content-Type: application/json`.

By default the server binds to `127.0.0.1:8080`. Configure via
`--host` and `--port` or run behind a reverse proxy.

---

## `POST /api/search`

Run a query built from row-based filters (the GUI's internal format)
and return matching documents.

### Request

```json
{
  "rows": [
    {"field": "name",    "value": "*.py", "join": "AND", "negate": false},
    {"field": "content", "value": "pandas", "join": "AND", "negate": false},
    {"field": "path",    "value": "site-packages", "join": "NOT", "negate": true}
  ],
  "limit": 50,
  "sort": "score desc",
  "highlight": true
}
```

Row `field` values:

| Field | Behavior |
|---|---|
| `text` | Full-text across `_text_` |
| `name` | Filename glob |
| `ext` | Extension (comma-sep within) |
| `path` | Full filepath (auto-wildcarded) |
| `dir` | Directory (trailing / = prefix match) |
| `content` | File content |
| `size` | Size filter: `>10MB`, `<1GB`, etc. |
| `since` | Modified after YYYY-MM-DD |
| `before` | Modified before YYYY-MM-DD |
| `raw` | Raw Solr/Lucene query passthrough |

`join` can be `AND`, `OR`, or `NOT`. Regex in `text`/`content`/`path`:
wrap the value in slashes (`/pattern/`).

### Response

```json
{
  "query": "filename:*.py AND content:pandas AND NOT filepath:*site-packages*",
  "total": 1234,
  "docs": [
    {
      "filepath": "/mnt/d/proj/main.py",
      "filename": "main.py",
      "extension": "py",
      "directory": "/mnt/d/proj",
      "size_bytes": 4567,
      "mtime": "2026-03-01T12:34:56Z",
      "content_preview": "import pandas as pd\n...",
      "content_sha256": "e8e4...",
      "language": "en",
      "mimetype_detected": "text/x-python",
      "highlights": {
        "content": ["... <mark>pandas</mark>.DataFrame ..."]
      }
    }
  ]
}
```

`highlights` is keyed by the filepath and contains Solr's highlight
snippets for `content` and `filename` fields.

---

## `POST /api/content`

Fetch the stored 1KB content preview for one file. Used by the GUI's
"click to expand" row behavior.

### Request

```json
{"filepath": "/mnt/d/proj/main.py"}
```

### Response

```json
{"content": "import pandas as pd\nimport numpy as np\n\ndef main():\n    ..."}
```

Returns `"(not indexed or no content extracted)"` if the file isn't in
Solr, and `"(no content extracted)"` if Solr has the doc but content
was skipped (e.g., binary file Tika couldn't parse).

---

## `POST /api/export`

Export filtered results to a downloadable file. Same row format as
`/api/search` plus a `format` field.

### Request

```json
{
  "rows": [{"field": "ext", "value": "py", "join": "AND", "negate": false}],
  "format": "csv",
  "limit": 10000,
  "sort": "filepath asc"
}
```

`format` is one of `csv`, `txt`, `json`. `limit` is capped server-side
at `EXPORT_MAX_ROWS = 100000` to prevent accidental full-index pulls.

### Response

A downloadable attachment (`Content-Disposition: attachment; filename=...`)
with the appropriate MIME type:

- `csv` → `text/csv; charset=utf-8`, all columns with header row
- `txt` → `text/plain; charset=utf-8`, filepath per line
- `json` → `application/json`, full doc list

Columns in CSV mode:
`filepath, filename, extension, size_bytes, mtime, directory,
content_sha256, language, mimetype_detected`.

---

## `POST /api/duplicates`

Find files that share a content hash, either by looking up a specific
hash or by enumerating all duplicate groups.

### Mode 1 — lookup by hash

```json
{"hash": "e8e493a1674976d5cbc4ae84baeb5f732dc846d8c6dea0e479003ba94a9fc3f4"}
```

Returns every doc that shares the hash:

```json
{
  "hash": "e8e4...",
  "total": 3,
  "docs": [
    {"filepath": "/path/a.py", "size_bytes": 1234, ...},
    {"filepath": "/path/b.py", "size_bytes": 1234, ...},
    {"filepath": "/path/c.py", "size_bytes": 1234, ...}
  ]
}
```

### Mode 2 — enumerate duplicate groups

```json
{"min_count": 2, "limit": 100}
```

Returns the top N hash values that have at least `min_count` docs,
sorted by group size descending:

```json
{
  "total_groups": 42,
  "groups": [
    {"hash": "e8e4...", "count": 17},
    {"hash": "a1b2...", "count": 9},
    ...
  ]
}
```

To drill into one group, follow up with a Mode-1 call using that hash.

---

## Error responses

All endpoints return `application/json` error bodies with a non-2xx
status:

```json
{"error": "Solr error: SolrError(...)"}
```

Status codes:

- `400` — malformed request body or invalid query clause
- `502` — upstream Solr error (connection refused, query syntax)
- `500` — internal server error

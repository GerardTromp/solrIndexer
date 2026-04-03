# Data Models

## Solr Schema (`filesystem` core)

### Fields

| Field | Solr Type | Stored | Indexed | Purpose |
|---|---|---|---|---|
| `id` (= filepath) | string | yes | yes | Unique key, full path |
| `filepath` | string | yes | yes | Full filesystem path |
| `filename` | text_general | yes | yes | Filename only |
| `filename_exact` | string | yes | yes | Exact filename for sorting |
| `extension` | string | yes | yes | File extension (no dot) |
| `directory` | string | yes | yes | Parent directory path |
| `size_bytes` | plong | yes | yes | File size in bytes |
| `mtime` | pdate | yes | yes | Last modified time |
| `mimetype` | string | yes | yes | MIME type from Tika |
| `content` | text_general | yes | yes | Full extracted text |
| `content_preview` | string | yes | no | First 1KB of content |
| `owner` | string | yes | yes | Unix UID |
| `_text_` | text_general | no | yes | Catch-all copy field |

### Copy Fields
- `filename` → `_text_`
- `content` → `_text_`

## Indexer State File

`~/.solr/indexer_state.json`:
```json
{
  "last_run": "2026-04-02T02:00:00",
  "roots_indexed": ["/home/user", "/mnt/wd1/GT"],
  "files_indexed": 623564,
  "errors": 142
}
```

## Error Log Format

`/mnt/wd1/solr/logs/index_errors.log`:
```
<filepath>\t<error_message>\t<timestamp>
```

## Permanent Skip List

`/mnt/wd1/solr/logs/permanent_skip.json`:
- Files classified as permanently unprocessable (corrupt, encrypted, etc.)
- Keyed by filepath with error reason and timestamp

## Web API Payloads

### POST `/api/search`
```json
{
  "rows": [{"field": "text", "value": "query", "join": "AND", "negate": false}],
  "limit": 50,
  "sort": "score desc",
  "highlight": true
}
```

### POST `/api/content`
```json
{"filepath": "/path/to/file"}
```

---
*Last Updated: 2026-04-03*

# Solr schema

The `filesystem` core schema is defined in
[`setup/setup_schema.sh`](https://github.com/GerardTromp/solrIndexer/blob/master/setup/setup_schema.sh)
and applied via Solr's `/schema` API. It's also safe to apply field
additions to a live Solr instance without restart.

## Fields

| Field | Type | Stored | Indexed | Purpose |
|---|---|:---:|:---:|---|
| `id` (= `filepath`) | string | ✓ | ✓ | Unique key |
| `filepath` | string | ✓ | ✓ | Full filesystem path |
| `filename` | text_general | ✓ | ✓ | Filename only (tokenized) |
| `filename_exact` | string | ✓ | ✓ | Exact filename for sort |
| `extension` | string | ✓ | ✓ | Lowercase, no dot |
| `directory` | string | ✓ | ✓ | Parent directory |
| `size_bytes` | plong | ✓ | ✓ | File size |
| `mtime` | pdate | ✓ | ✓ | Filesystem mtime |
| `mimetype` | string | ✓ | ✓ | Filename-guessed MIME |
| `mimetype_detected` | string | ✓ | ✓ | Tika-detected MIME (Phase 0.3) |
| `language` | string | ✓ | ✓ | ISO-639 language (Phase 0.3) |
| `content` | text_general |   | ✓ | Full extracted text (not stored) |
| `content_preview` | string | ✓ |   | First 1KB for GUI row expand |
| `content_sha256` | string | ✓ | ✓ | Content hash for dedup (Phase 0.2) |
| `owner` | string | ✓ |   | Unix UID |
| `source_name` | string | ✓ | ✓ | Source identifier (Phase 1) |
| `source_kind` | string | ✓ | ✓ | Source coarse type (Phase 1) |
| `source_timestamp` | pdate | ✓ | ✓ | Source-native timestamp (Phase 1) |
| `source_metadata` | string | ✓ |   | Opaque JSON blob (Phase 1) |
| `_text_` | text_general |   | ✓ | Catch-all for full-text |

## Copy fields

| Source | Destination | Why |
|---|---|---|
| `filename` | `filename_exact` | String copy for sorting |
| `filename` | `_text_` | Full-text match on filenames |
| `content` | `_text_` | Full-text match on content |

## Design notes

**Why `content` is `stored=false`**  
The full extracted text can be large (hundreds of KB per PDF). Storing
it in Solr doubles the core's disk footprint for no benefit — we only
need it for indexing and for highlight snippet generation, both of
which work without a stored copy. The 1KB `content_preview` (stored
but not indexed) gives the GUI enough to show a preview on row
expand.

**Why `source_metadata` is `indexed=false`**  
It's an opaque JSON blob defined by each source, and we don't want
Solr to tokenize the inner structure. Queries that want to filter by
sender or subject should use the raw `source_metadata` field with a
literal JSON substring query, OR (better) the source should promote
those fields into dedicated schema fields in a future iteration. For
now, metadata is retrieve-only.

**Why two MIME fields (`mimetype` + `mimetype_detected`)**  
`mimetype` is a fast, filename-based guess (Python's `mimetypes`
module). It's correct ~95% of the time for well-named files and is
available without running Tika. `mimetype_detected` is the ground
truth from Tika's content sniffer, available only after the file has
been processed. Both are useful: filter by `mimetype:application/pdf`
for a fast first-pass and by `mimetype_detected:application/pdf` to
include mis-named files.

**Why `content_sha256` exists separately from size**  
Size+mtime catches the common "file hasn't changed" case cheaply. The
hash catches content-level duplicates across the filesystem (same
content at different paths, or same content with different mtimes
after a copy operation). The hash is ALSO what makes the
`_file_unchanged` incremental short-circuit safe to skip Tika
entirely: if size+mtime+hash all match, we know the doc is fully
up-to-date with no missing enrichment fields.

## Applying schema changes

Live updates work via the `/schema` API without a Solr restart:

```bash
curl -s -X POST "http://localhost:8983/solr/filesystem/schema" \
  -H "Content-Type: application/json" \
  -d '{
    "add-field": {
      "name": "my_new_field",
      "type": "string",
      "stored": true,
      "indexed": true
    }
  }'
```

For bulk changes, pass an array to `add-field`. Always update
`setup/setup_schema.sh` in the same commit so fresh installs get
the same schema.

To verify the current schema:

```bash
curl -s "http://localhost:8983/solr/filesystem/schema/fields" \
  | python3 -m json.tool \
  | grep '"name"'
```

## Indexer state

`~/.solr/indexer_state.json` — tracks per-source incremental cursors:

```json
{
  "last_run": "2026-04-03T02:00:00",
  "indexed_count": 623564,
  "sources": {
    "filesystem": {"last_run": "2026-04-11T02:00:00"},
    "pst-archive": {"last_run": "2026-04-11T02:15:00"},
    "gmail": {"last_run": "2026-04-11T02:05:00"}
  }
}
```

The global `last_run` is preserved for legacy single-root back-compat;
per-source keys under `sources` are what the indexer actually reads
when running under the multi-source loop.

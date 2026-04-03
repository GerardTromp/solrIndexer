# Architecture Decision Records

## ADR Index
1. [ADR-001: Solr over Elasticsearch] - 2026-03-24 - Accepted
2. [ADR-002: Tika server mode over embedded] - 2026-03-24 - Accepted
3. [ADR-003: WSL2 native over Docker] - 2026-03-24 - Accepted
4. [ADR-004: Incremental indexing via mtime] - 2026-03-24 - Accepted
5. [ADR-005: Content preview field] - 2026-03-30 - Accepted

---

## ADR-001: Solr over Elasticsearch

**Date**: 2026-03-24
**Status**: Accepted

### Context
Need a full-text search engine for filesystem indexing with regex support.

### Decision
Use Apache Solr 10 with SolrCloud mode.

### Consequences
- Solr has native regex support in queries
- Simpler single-node setup than Elasticsearch
- `pysolr` is a lightweight, mature Python client

---

## ADR-002: Tika server mode over embedded

**Date**: 2026-03-24
**Status**: Accepted

### Context
Content extraction needed for PDFs, Office docs, etc.

### Decision
Run Tika as a standalone HTTP server rather than embedded in Python.

### Consequences
- Tika process can be restarted independently
- Memory isolation (Tika JVM separate from Python)
- Can tune heap separately (512m default, 4g for large files)
- HTTP overhead is negligible vs extraction time

---

## ADR-003: WSL2 native over Docker

**Date**: 2026-03-24
**Status**: Accepted

### Context
Need to index Windows filesystem from Linux tools.

### Decision
Run Solr and Tika natively in WSL2, accessing Windows drives via `/mnt/`.

### Consequences
- Direct filesystem access without Docker volume mounts
- Simpler networking (localhost)
- WSL2 port forwarding to Windows can be fragile (wslrelay issues)

---

## ADR-004: Incremental indexing via mtime

**Date**: 2026-03-24
**Status**: Accepted

### Context
Full re-indexing of 600K+ files is slow (hours).

### Decision
Compare file mtime against Solr's stored mtime to skip unchanged files.

### Consequences
- Incremental runs complete in minutes
- Find cache accelerates file discovery
- Requires purge step to remove deleted files from index

---

## ADR-005: Content preview field

**Date**: 2026-03-30
**Status**: Accepted

### Context
Web GUI needs to show content snippets without fetching full content.

### Decision
Store first 1KB of extracted content in `content_preview` field at index time.

### Consequences
- No extra Solr query needed for preview
- Slightly increases index size
- Preview available even when Tika is offline

---
*Last Updated: 2026-04-03*

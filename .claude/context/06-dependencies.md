# Dependencies

## External Dependencies

| Package | Purpose | Required By |
|---|---|---|
| pysolr | Solr client | fs_indexer, fsearch, fsearch_web |
| requests | HTTP client (Tika) | fs_indexer, triage_errors |
| click | CLI framework | fs_indexer |
| rich | Terminal formatting | fs_indexer, fsearch |
| flask | Web framework | fsearch_web |
| tika (pip) | Tika client (legacy, may not be used) | install.sh |

## Infrastructure Dependencies

| Component | Version | Location | Purpose |
|---|---|---|---|
| Apache Solr | 10.0.0 | `~/opt/solr/` | Search engine |
| Apache Tika | 3.0.0 | `~/opt/tika-server.jar` | Content extraction |
| Java | 21 (OpenJDK) | System | Solr + Tika runtime |
| Python | 3.x | System | All scripts |

## Data Dependencies

| Path | Purpose |
|---|---|
| `/mnt/wd1/solr/data/` | Solr index data |
| `/mnt/wd1/solr/logs/` | Logs (indexer, Tika, errors) |
| `/mnt/wd1/solr/find_cache.txt` | File list cache |
| `~/.solr/indexer_state.json` | Indexer state |

## Index Roots

Configured in `run_index.sh`:
- `/home/$USER`
- `/mnt/wd1/GT`
- `/mnt/d/GT`

---
*Last Updated: 2026-04-03*

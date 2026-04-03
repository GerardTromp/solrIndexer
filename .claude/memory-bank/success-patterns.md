# Success Patterns

## Error Triage Pipeline
- **Pattern**: Separate retryable from permanent failures, maintain a skip list
- **Why it works**: Prevents wasting time re-processing corrupt/encrypted files on every indexing run
- **Applied in**: v0.0.3-v0.0.4

## Content Preview at Index Time
- **Pattern**: Store first 1KB of content in a separate Solr field during indexing
- **Why it works**: Web GUI can show previews without an extra Tika call or large content fetch
- **Applied in**: v0.0.1+

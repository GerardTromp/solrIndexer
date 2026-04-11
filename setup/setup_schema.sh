#!/bin/bash
# setup_schema.sh — Post the filesystem schema to Solr 10
# Run once after: solr create -c filesystem
# Solr must be running: solr start --force

set -euo pipefail

SOLR_BASE="http://localhost:8983/solr/filesystem"

echo "Checking Solr is up..."
curl -sf "${SOLR_BASE}/admin/ping" | grep -q '"status":"OK"' || {
    echo "ERROR: Solr not responding at ${SOLR_BASE}"
    exit 1
}

echo "Disabling auto field creation..."
curl -s -X POST "${SOLR_BASE}/config" \
    -H 'Content-Type: application/json' \
    -d '{"set-user-property": {"update.autoCreateFields":"false"}}' \
    | python3 -m json.tool

echo ""
echo "Posting schema fields..."
curl -s -X POST "${SOLR_BASE}/schema" \
    -H 'Content-Type: application/json' \
    -d '{
  "add-field": [
    {"name":"filepath",       "type":"string",       "stored":true,  "indexed":true},
    {"name":"filename",       "type":"text_general",  "stored":true,  "indexed":true},
    {"name":"filename_exact", "type":"string",       "stored":false, "indexed":true},
    {"name":"extension",      "type":"string",       "stored":true,  "indexed":true},
    {"name":"directory",      "type":"string",       "stored":true,  "indexed":true},
    {"name":"size_bytes",     "type":"plong",         "stored":true,  "indexed":true},
    {"name":"mtime",          "type":"pdate",         "stored":true,  "indexed":true},
    {"name":"mimetype",       "type":"string",       "stored":true,  "indexed":true},
    {"name":"content",        "type":"text_general",  "stored":false, "indexed":true},
    {"name":"content_preview", "type":"text_general",   "stored":true,  "indexed":true},
    {"name":"owner",          "type":"string",       "stored":true,  "indexed":false},
    {"name":"content_sha256", "type":"string",       "stored":true,  "indexed":true},
    {"name":"language",       "type":"string",       "stored":true,  "indexed":true},
    {"name":"mimetype_detected","type":"string",     "stored":true,  "indexed":true}
  ],
  "add-copy-field": [
    {"source":"filename", "dest":"filename_exact"},
    {"source":"filename", "dest":"_text_"},
    {"source":"content",  "dest":"_text_"}
  ]
}' | python3 -m json.tool

echo ""
echo "Verifying fields..."
curl -s "${SOLR_BASE}/schema/fields" \
    | python3 -m json.tool \
    | grep '"name"' \
    | grep -v '"_"'   # hide internal _text_, _version_ etc.

echo ""
echo "Schema setup complete."

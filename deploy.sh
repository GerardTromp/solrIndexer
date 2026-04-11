#!/bin/bash
# deploy.sh — Copy fsearch scripts + static assets to FSEARCH_DIR.
# Use after editing any of the scripts or static files. No restart needed;
# Flask picks up Python changes via debug reload, and static/ is re-read
# on every request.
#
# Usage:
#   ./deploy.sh                    # deploy everything
#   ./deploy.sh static             # deploy only static/ (HTML/CSS/JS)
#   ./deploy.sh scripts            # deploy only *.py and run_index.sh
#   FSEARCH_DIR=/other ./deploy.sh # override destination

set -euo pipefail

FSEARCH_DIR="${FSEARCH_DIR:-/opt/fsearch}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WHAT="${1:-all}"

SCRIPTS=(fs_indexer.py fsearch.py fsearch_web.py fsearch_hash.py fs_sources.py run_index.sh triage_errors.py)
# Non-script assets copied only when missing (never overwritten)
PRESERVE_IF_EXISTS=(sources.yaml.example)

if [[ ! -d "$FSEARCH_DIR" ]]; then
    echo "ERROR: $FSEARCH_DIR does not exist. Run install.sh first." >&2
    exit 1
fi

deploy_scripts() {
    echo "==> Deploying scripts to ${FSEARCH_DIR}/"
    for f in "${SCRIPTS[@]}"; do
        if [[ -f "$SCRIPT_DIR/$f" ]]; then
            sudo cp "$SCRIPT_DIR/$f" "$FSEARCH_DIR/$f"
            echo "    $f"
        fi
    done
    # Non-script assets: copy only if not already present (never clobber a
    # live config with the example).
    for f in "${PRESERVE_IF_EXISTS[@]}"; do
        if [[ -f "$SCRIPT_DIR/$f" && ! -f "$FSEARCH_DIR/$f" ]]; then
            sudo cp "$SCRIPT_DIR/$f" "$FSEARCH_DIR/$f"
            echo "    $f (new)"
        fi
    done
    sudo chmod +x "$FSEARCH_DIR"/*.py "$FSEARCH_DIR"/*.sh 2>/dev/null || true
}

deploy_static() {
    echo "==> Deploying static/ to ${FSEARCH_DIR}/static/"
    sudo mkdir -p "$FSEARCH_DIR/static"
    sudo cp -r "$SCRIPT_DIR/static/." "$FSEARCH_DIR/static/"
    ls "$FSEARCH_DIR/static/" | sed 's/^/    /'
}

case "$WHAT" in
    all)     deploy_scripts; deploy_static ;;
    scripts) deploy_scripts ;;
    static)  deploy_static ;;
    *)       echo "Usage: $0 [all|scripts|static]" >&2; exit 2 ;;
esac

echo "==> Done."

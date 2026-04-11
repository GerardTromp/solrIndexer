#!/bin/bash
# install.sh — One-shot setup for fsearch on WSL2 Ubuntu + Solr 10
# Run as your normal user (not root). Requires sudo for apt steps.
# Edit the CONFIG section below before running.

set -euo pipefail

# ── CONFIG — edit these ───────────────────────────────────────────────────────

SOLR_VER="10.0.0"
TIKA_VER="3.0.0"
INSTALL_DIR="$HOME/opt"
DATA_MOUNT="/mnt/wd1"          # your ext4 data disk
FSEARCH_DIR="/opt/fsearch"     # where scripts live (needs sudo to create)
INDEX_ROOTS="/home/$USER"      # space-separated list of paths to index

# ── Derived paths ─────────────────────────────────────────────────────────────

SOLR_HOME="$INSTALL_DIR/solr"
TIKA_JAR="$INSTALL_DIR/tika-server.jar"
SOLR_DATA="$DATA_MOUNT/solr/data"
SOLR_LOGS="$DATA_MOUNT/solr/logs"

# ── 1. System dependencies ────────────────────────────────────────────────────

echo "==> Installing Java 21..."
sudo apt update -q
sudo apt install -y openjdk-21-jdk-headless curl python3-pip
java -version

echo "==> Installing Python dependencies..."
pip install pysolr requests click rich tika pyyaml psutil --break-system-packages

# ── 2. Solr ───────────────────────────────────────────────────────────────────

echo "==> Downloading Solr ${SOLR_VER}..."
mkdir -p "$INSTALL_DIR"
curl -L --fail --progress-bar \
    "https://www.apache.org/dyn/closer.lua/solr/solr/${SOLR_VER}/solr-${SOLR_VER}.tgz?action=download" \
    -o /tmp/solr-${SOLR_VER}.tgz

tar xzf /tmp/solr-${SOLR_VER}.tgz -C /tmp
mv /tmp/solr-${SOLR_VER} "$SOLR_HOME"
rm /tmp/solr-${SOLR_VER}.tgz

# ── 3. Tika ───────────────────────────────────────────────────────────────────

echo "==> Downloading Tika ${TIKA_VER}..."
curl -L --fail --progress-bar \
    "https://www.apache.org/dyn/closer.lua/tika/${TIKA_VER}/tika-server-standard-${TIKA_VER}.jar?action=download" \
    -o "$TIKA_JAR"

# Verify it's actually a JAR, not an HTML redirect page
file "$TIKA_JAR" | grep -q "Java archive" || {
    echo "ERROR: Tika download looks wrong (got HTML instead of JAR?)"
    file "$TIKA_JAR"
    exit 1
}

# ── 4. Solr configuration ─────────────────────────────────────────────────────

echo "==> Configuring Solr..."
mkdir -p "$SOLR_DATA" "$SOLR_LOGS"

cat >> "$SOLR_HOME/bin/solr.in.sh" << EOF

# fsearch custom config
SOLR_JAVA_MEM="-Xms512m -Xmx2g"
SOLR_PORT=8983
SOLR_DATA_HOME="${SOLR_DATA}"
SOLR_LOGS_DIR="${SOLR_LOGS}"
SOLR_PID_DIR="${DATA_MOUNT}/solr"
EOF

# ── 5. Install scripts ────────────────────────────────────────────────────────

echo "==> Installing fsearch scripts to ${FSEARCH_DIR}..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
sudo mkdir -p "$FSEARCH_DIR"
sudo cp "$SCRIPT_DIR/fs_indexer.py" "$SCRIPT_DIR/fsearch.py" "$SCRIPT_DIR/fsearch_web.py" "$SCRIPT_DIR/fsearch_hash.py" "$SCRIPT_DIR/fs_sources.py" "$SCRIPT_DIR/run_index.sh" "$FSEARCH_DIR/"
# Drop the example config if a live one doesn't already exist
if [[ ! -f "$FSEARCH_DIR/sources.yaml" ]]; then
    sudo cp "$SCRIPT_DIR/sources.yaml.example" "$FSEARCH_DIR/sources.yaml.example"
fi
sudo mkdir -p "$FSEARCH_DIR/static"
sudo cp "$SCRIPT_DIR/static/search.html" "$FSEARCH_DIR/static/"
sudo chmod +x "$FSEARCH_DIR/run_index.sh" "$FSEARCH_DIR/fsearch.py" "$FSEARCH_DIR/fs_indexer.py" "$FSEARCH_DIR/fsearch_web.py"

# Symlink fsearch to PATH
sudo ln -sf "$FSEARCH_DIR/fsearch.py" /usr/local/bin/fsearch

# ── 6. Shell environment ──────────────────────────────────────────────────────

echo "==> Adding shell config to ~/.bashrc..."
cat >> ~/.bashrc << BASHRC

# ── fsearch / Solr ────────────────────────────────────────────────
export SOLR_HOME="${SOLR_HOME}"
export PATH="\$SOLR_HOME/bin:\$PATH"
export SOLR_URL="http://localhost:8983/solr/filesystem"

alias solr-start="\$SOLR_HOME/bin/solr start --force"
alias solr-stop="\$SOLR_HOME/bin/solr stop"
alias solr-status="\$SOLR_HOME/bin/solr status"
alias solr-count='curl -s "http://localhost:8983/solr/filesystem/select?q=*:*&rows=0" | python3 -c "import sys,json; print(json.load(sys.stdin)[\"response\"][\"numFound\"], \"docs indexed\")"'

# Find cache settings (used by fs_indexer.py)
export FSEARCH_FIND_CACHE="${DATA_MOUNT}/solr/find_cache.txt"
export FSEARCH_FIND_CACHE_MAX_HOURS=12

# Auto-start Solr and Tika if /mnt/wd1 is mounted
if mountpoint -q ${DATA_MOUNT} 2>/dev/null; then
    if ! pgrep -f "solr.jetty" > /dev/null 2>&1; then
        \$SOLR_HOME/bin/solr start --force -q
    fi
    if ! pgrep -f "tika-server" > /dev/null 2>&1; then
        nohup java -jar ${TIKA_JAR} --port 9998 \
            >> ${SOLR_LOGS}/tika.log 2>&1 &
    fi
else
    echo "WARNING: ${DATA_MOUNT} not mounted — Solr/Tika not started"
fi
BASHRC

# ── 7. Cron job ───────────────────────────────────────────────────────────────

echo "==> Installing cron job (daily 2am)..."
# Remove any existing fsearch cron entry then add fresh
(crontab -l 2>/dev/null | grep -v fsearch; \
 echo "0 2 * * * ${FSEARCH_DIR}/run_index.sh >> ${SOLR_LOGS}/indexer.log 2>&1") \
 | crontab -

# ── 8. Create core and schema ─────────────────────────────────────────────────

echo "==> Starting Solr and creating core..."
source ~/.bashrc 2>/dev/null || true
"$SOLR_HOME/bin/solr" start --force
sleep 3
"$SOLR_HOME/bin/solr" create -c filesystem

echo "==> Posting schema..."
bash setup/setup_schema.sh

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "==> Installation complete."
echo ""
echo "Next steps:"
echo "  1. source ~/.bashrc"
echo "  2. Run first full index:"
echo "     python3 ${FSEARCH_DIR}/fs_indexer.py ${INDEX_ROOTS} --full"
echo "  3. Search:"
echo "     fsearch 'your query'"
echo "     fsearch --name '*.vcf' --since 2024-01-01"
echo "     fsearch --content '/p[._]?adj\s*<\s*0\.05/' --ext py,r"
echo "  4. Retry failed files:"
echo "     python3 ${FSEARCH_DIR}/fs_indexer.py --retry-errors"
echo "  5. Check index count:"
echo "     solr-count"

#!/bin/bash
# run_index.sh — Cron wrapper for fs_indexer.py
# Ensures Tika is running, then runs the indexer.
# Also retries previously failed files before the main crawl.
#
# Usage:
#   run_index.sh                    # normal run (Tika at default 512m heap)
#   run_index.sh --tika-heap 4g     # temporarily boost Tika heap for large files

DATA_MOUNT="/mnt/wd1"
SOLR_LOGS="$DATA_MOUNT/solr/logs"
TIKA_JAR="$HOME/opt/tika-server.jar"
FSEARCH_DIR="/opt/fsearch"
INDEX_ROOTS="/home/$USER /mnt/wd1/GT /mnt/d/GT"
EXCLUDE_PATHS="/home/gerard/.cache /mnt/d/GT/Professional/NLM_CDE/work2/test_clustering /mnt/wd1/GT/NLM_CDE/cde_python"

TIKA_DEFAULT_HEAP="512m"
TIKA_HEAP="$TIKA_DEFAULT_HEAP"
TIKA_HEAP_OVERRIDE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --tika-heap) TIKA_HEAP="$2"; TIKA_HEAP_OVERRIDE=true; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ────────────────────────────────────────────────────────────────

# Convert heap spec (512m, 2g, etc.) to bytes for comparison
heap_to_bytes() {
    local val="${1%[gmGM]}"
    local unit="${1: -1}"
    case "$unit" in
        g|G) echo $(( val * 1024 * 1024 * 1024 )) ;;
        m|M) echo $(( val * 1024 * 1024 )) ;;
        *)   echo $(( val )) ;;
    esac
}

get_running_tika_heap() {
    # Extract -Xmx from the running Tika JVM's command line
    local pid
    pid=$(pgrep -f "tika-server" | head -1)
    [ -z "$pid" ] && return 1
    local xmx
    xmx=$(tr '\0' ' ' < /proc/"$pid"/cmdline 2>/dev/null | grep -oP '\-Xmx\K[^ ]+')
    echo "${xmx:-${TIKA_DEFAULT_HEAP}}"
}

start_tika() {
    local heap="$1"
    # Rotate the Tika log to mark the boundary
    if [ -f "$SOLR_LOGS/tika.log" ] && [ -s "$SOLR_LOGS/tika.log" ]; then
        mv "$SOLR_LOGS/tika.log" "$SOLR_LOGS/tika.log.$(date +%Y%m%d_%H%M%S)"
    fi

    echo "$(date): Starting Tika with -Xmx${heap}" | tee -a "$SOLR_LOGS/indexer.log"
    nohup java -Xmx${heap} -jar "$TIKA_JAR" --port 9998 \
        >> "$SOLR_LOGS/tika.log" 2>&1 &

    echo "Waiting for Tika to be ready..."
    for i in $(seq 1 20); do
        curl -sf http://localhost:9998/tika > /dev/null 2>&1 && return 0
        sleep 2
    done
    echo "$(date): ERROR — Tika failed to start after 40s" | tee -a "$SOLR_LOGS/indexer.log"
    return 1
}

stop_tika() {
    echo "$(date): Stopping Tika..." | tee -a "$SOLR_LOGS/indexer.log"
    pkill -f "tika-server" 2>/dev/null
    # Wait for it to actually exit
    for i in $(seq 1 10); do
        pgrep -f "tika-server" > /dev/null 2>&1 || return 0
        sleep 1
    done
    # Force kill if still alive
    pkill -9 -f "tika-server" 2>/dev/null
    sleep 1
}

# ── Check data mount ────────────────────────────────────────────────────────

if ! mountpoint -q "$DATA_MOUNT" 2>/dev/null; then
    echo "$(date): ERROR — $DATA_MOUNT not mounted, aborting" >> "$SOLR_LOGS/indexer.log"
    exit 1
fi

# ── Rotate logs ────────────────────────────────────────────────────────────

rotate_log() {
    local logfile="$1"
    local max_bytes="${2:-10485760}"
    local keep="${3:-5}"

    [ -f "$logfile" ] || return 0
    local sz
    sz=$(stat -c%s "$logfile" 2>/dev/null || echo 0)
    [ "$sz" -lt "$max_bytes" ] && return 0

    local i=$keep
    while [ "$i" -gt 1 ]; do
        local prev=$((i - 1))
        [ -f "${logfile}.${prev}" ] && mv -f "${logfile}.${prev}" "${logfile}.${i}"
        i=$prev
    done
    mv -f "$logfile" "${logfile}.1"
    gzip -f "${logfile}.1" 2>/dev/null &
}

rotate_log "$SOLR_LOGS/tika.log"     10485760 5
rotate_log "$SOLR_LOGS/indexer.log"  10485760 5

# ── Ensure Tika is up (with heap management) ──────────────────────────────

TIKA_RESTARTED=false

if pgrep -f "tika-server" > /dev/null 2>&1; then
    if $TIKA_HEAP_OVERRIDE; then
        running_heap=$(get_running_tika_heap)
        running_bytes=$(heap_to_bytes "$running_heap")
        requested_bytes=$(heap_to_bytes "$TIKA_HEAP")

        if [ "$requested_bytes" -gt "$running_bytes" ]; then
            echo "Tika running with -Xmx${running_heap}, requested -Xmx${TIKA_HEAP} — restarting with larger heap"
            stop_tika
            start_tika "$TIKA_HEAP" || exit 1
            TIKA_RESTARTED=true
        else
            echo "Tika already running with -Xmx${running_heap} (>= requested ${TIKA_HEAP})"
        fi
    fi
    # else: Tika is running, no override requested — leave it alone
else
    echo "$(date): Tika not running" >> "$SOLR_LOGS/indexer.log"
    start_tika "$TIKA_HEAP" || exit 1
    if $TIKA_HEAP_OVERRIDE; then
        TIKA_RESTARTED=true
    fi
fi

# ── Build exclude args ──────────────────────────────────────────────────────

EXCLUDE_ARGS=""
for p in $EXCLUDE_PATHS; do
    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude $p"
done

# ── Retry previously failed files, then run incremental index ────────────────

/usr/bin/python3 "$FSEARCH_DIR/fs_indexer.py" \
    $INDEX_ROOTS \
    $EXCLUDE_ARGS \
    --retry-errors \
    >> "$SOLR_LOGS/indexer.log" 2>&1

# ── Teardown: restore default Tika heap if we upgraded it ────────────────────

if $TIKA_RESTARTED; then
    echo "Indexing complete — restoring Tika to default heap (-Xmx${TIKA_DEFAULT_HEAP})"
    stop_tika
    start_tika "$TIKA_DEFAULT_HEAP"
fi

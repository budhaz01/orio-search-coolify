#!/bin/bash
# Cloudflared + Railway env auto-sync.
#
# WHY
#   `cloudflared tunnel --url` issues an ephemeral *.trycloudflare.com URL
#   that changes every time the daemon restarts (Mac reboot, cloudflared
#   crash, ...). The Railway app reads ORIOSEARCH_URL at process start, so
#   when the URL changes we need to:
#     1) detect the new URL from cloudflared's stderr
#     2) push it to Railway via the CLI
#     3) let Railway redeploy (default behavior of `variable set`)
#
# Designed to be run by launchd (com.aile.oriosearch-tunnel.plist) and
# kept alive forever. Logs to ~/Library/Logs/oriosearch-tunnel.log so
# debugging from outside the launchd context is straightforward.

set -uo pipefail

REPO_ROOT="/Users/jeremycohen/Downloads/apprentissage/escape-game outdoor"
LOG_FILE="$HOME/Library/Logs/oriosearch-tunnel.log"
URL_STATE_FILE="$HOME/Library/Application Support/oriosearch-tunnel.url"
LOCAL_TARGET="http://localhost:8000"

# Ensure dirs exist
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$URL_STATE_FILE")"

log() {
    local ts
    ts=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$ts] $*" | tee -a "$LOG_FILE" >&2
}

# Path containing `railway` and `cloudflared` (brew bin) — launchd doesn't
# inherit the interactive shell PATH, so we force it.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

log "=== Tunnel supervisor starting ==="

if ! command -v cloudflared >/dev/null 2>&1; then
    log "FATAL: cloudflared not on PATH. brew install cloudflared."
    exit 1
fi
if ! command -v railway >/dev/null 2>&1; then
    log "FATAL: railway CLI not on PATH. Install it (npm i -g @railway/cli) and run 'railway link' once in $REPO_ROOT."
    exit 1
fi

push_url_to_railway() {
    local url="$1"
    log "Detected tunnel URL: $url"
    # Skip if unchanged (Mac may have rerun the script with the same URL after a process recycle)
    if [ -f "$URL_STATE_FILE" ] && [ "$(cat "$URL_STATE_FILE")" = "$url" ]; then
        log "URL unchanged, skipping Railway sync."
        return 0
    fi
    log "Pushing new URL to Railway (this triggers a redeploy)..."
    (cd "$REPO_ROOT" && railway variable set "ORIOSEARCH_URL=$url" --json) >>"$LOG_FILE" 2>&1
    local rc=$?
    if [ $rc -eq 0 ]; then
        echo "$url" >"$URL_STATE_FILE"
        log "Railway sync OK."
    else
        log "Railway sync FAILED (exit $rc). Will retry on next URL event."
    fi
}

# `cloudflared tunnel --url` prints the URL once to stderr. We tee its
# stream into a pipeline that watches for the URL line, captures it, then
# calls the sync function.
while :; do
    log "Launching cloudflared tunnel for $LOCAL_TARGET ..."
    cloudflared tunnel --url "$LOCAL_TARGET" --no-autoupdate 2>&1 | \
    while IFS= read -r line; do
        echo "$line" >>"$LOG_FILE"
        if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
            push_url_to_railway "${BASH_REMATCH[1]}"
        fi
    done
    log "cloudflared exited. Restarting in 5s..."
    sleep 5
done

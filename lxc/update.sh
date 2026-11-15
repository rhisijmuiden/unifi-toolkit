#!/usr/bin/env bash
# UniFi Toolkit - in-app updater (runs as root via unifi-toolkit-update.service).
#
# The web UI writes the channel (main or test) to $STATE_DIR/update-request.
# unifi-toolkit-update.path notices the file and starts this script, which:
#   1. checks out the newest commit of that channel,
#   2. installs the Python dependencies,
#   3. restarts the toolkit (database migrations run on start),
#   4. rolls back to the previous commit if the toolkit doesn't come back.
# Progress goes to $STATE_DIR/update-status.json and update.log for the UI.
set -uo pipefail

APP_DIR="/opt/unifi-toolkit"
STATE_DIR="/var/lib/unifi-toolkit"
REQUEST="$STATE_DIR/update-request"
STATUS="$STATE_DIR/update-status.json"
LOG="$STATE_DIR/update.log"
SERVICE_USER="toolkit"

[ -f "$REQUEST" ] || exit 0
CHANNEL=$(head -n 1 "$REQUEST" | tr -cd 'a-z')
rm -f "$REQUEST"

STARTED=$(date -Iseconds)
FROM=$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
TO=""

status() {  # status <state> <message>
    local tmp="$STATUS.tmp"
    printf '{"state":"%s","channel":"%s","from":"%s","to":"%s","started":"%s","finished":"%s","message":"%s"}\n' \
        "$1" "$CHANNEL" "$FROM" "$TO" "$STARTED" \
        "$([ "$1" = running ] && echo "" || date -Iseconds)" \
        "$(printf '%s' "$2" | tr '"\\' "''")" > "$tmp"
    chown "$SERVICE_USER":"$SERVICE_USER" "$tmp"
    mv "$tmp" "$STATUS"
}
log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

port() {
    local p
    p=$(grep -E '^APP_PORT=' "$APP_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2 | tr -d "'\"")
    echo "${p:-8000}"
}

healthy() {
    # Healthy = the service is active and /health answers three times in a
    # row, so a version that crashes a few seconds after start still fails.
    local ok=0
    sleep 3
    for _ in $(seq 1 60); do
        if systemctl is-active --quiet unifi-toolkit \
                && curl -fsS "http://127.0.0.1:$(port)/health" >/dev/null 2>&1; then
            ok=$((ok + 1))
            [ "$ok" -ge 3 ] && return 0
        else
            ok=0
        fi
        sleep 2
    done
    return 1
}

deploy() {  # deploy <git ref>
    git -C "$APP_DIR" checkout --quiet -B "$CHANNEL" "$1" >> "$LOG" 2>&1 || return 1
    log "Installing Python dependencies"
    "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" >> "$LOG" 2>&1 || return 1
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
    chown root:"$SERVICE_USER" "$APP_DIR/.env"
    chmod 640 "$APP_DIR/.env"
    log "Restarting unifi-toolkit"
    systemctl restart unifi-toolkit
    healthy
}

: > "$LOG"
chown "$SERVICE_USER":"$SERVICE_USER" "$LOG"

case "$CHANNEL" in
    main|test) ;;
    *) status failed "Unknown channel"; log "Refused unknown channel '$CHANNEL'"; exit 1 ;;
esac

git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
PREVIOUS=$(git -C "$APP_DIR" rev-parse HEAD)
PREVIOUS_BRANCH=$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)

status running "Downloading the newest version of $CHANNEL"
log "Update to channel '$CHANNEL' from $FROM ($PREVIOUS_BRANCH)"
if ! git -C "$APP_DIR" fetch --quiet origin "+refs/heads/$CHANNEL:refs/remotes/origin/$CHANNEL" >> "$LOG" 2>&1; then
    status failed "Could not download from GitHub"
    log "git fetch failed"
    exit 1
fi
TO=$(git -C "$APP_DIR" rev-parse --short "origin/$CHANNEL")

status running "Installing $TO"
if deploy "origin/$CHANNEL"; then
    status success "Updated to $TO on $CHANNEL"
    log "Done: running $TO"
    exit 0
fi

log "The new version did not start. Rolling back to $FROM"
status running "The new version did not start, rolling back"
CHANNEL="$PREVIOUS_BRANCH"
if deploy "$PREVIOUS"; then
    status rolled_back "The update failed and was rolled back to $FROM. See the log."
else
    status failed "The update and the rollback failed. Check: journalctl -u unifi-toolkit"
fi
exit 1

#!/usr/bin/env bash
# UniFi Toolkit - move an existing Docker install into the LXC install.
#
# 1. On the old Docker host, stop the toolkit and pack its data and settings:
#      cd /path/to/unifi-toolkit && docker compose down
#      tar czf toolkit-docker.tgz data .env
# 2. Copy toolkit-docker.tgz into the container (for example with
#    `pct push <ctid> toolkit-docker.tgz /root/toolkit-docker.tgz` on Proxmox).
# 3. Inside the container, as root:
#      ./migrate-from-docker.sh /root/toolkit-docker.tgz
#
# The database is copied as is, and ENCRYPTION_KEY, login and mode settings are
# taken from the old .env. The key must match, otherwise the stored UniFi
# credentials can't be decrypted. The current LXC database and .env are kept
# as *.before-migration backups.
set -euo pipefail

APP_DIR="/opt/unifi-toolkit"
DATA_DIR="/var/lib/unifi-toolkit"
ENV_FILE="$APP_DIR/.env"
SRC="${1:-}"

die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "Run this script as root."
[ -n "$SRC" ] || die "Usage: $0 <toolkit-docker.tgz | old install directory>"
[ -f "$ENV_FILE" ] || die "No LXC install found. Run lxc/install.sh first."

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
if [ -d "$SRC" ]; then
    cp -a "$SRC"/. "$WORK"/
else
    tar xzf "$SRC" -C "$WORK"
fi
[ -f "$WORK/data/unifi_toolkit.db" ] || die "data/unifi_toolkit.db not found in $SRC"
[ -f "$WORK/.env" ] || die ".env not found in $SRC"

info "Stopping unifi-toolkit"
systemctl stop unifi-toolkit

STAMP=$(date +%Y%m%d-%H%M%S)
[ -f "$DATA_DIR/unifi_toolkit.db" ] && mv "$DATA_DIR/unifi_toolkit.db" "$DATA_DIR/unifi_toolkit.db.before-migration-$STAMP"
cp "$ENV_FILE" "$ENV_FILE.before-migration-$STAMP"

info "Copying the database"
install -o toolkit -g toolkit -m 640 "$WORK/data/unifi_toolkit.db" "$DATA_DIR/unifi_toolkit.db"

info "Taking settings from the old .env"
# Docker Compose needs $$ for a literal $, so undo that for bcrypt hashes.
for VAR in ENCRYPTION_KEY DEPLOYMENT_TYPE DOMAIN ALLOWED_HOSTS AUTH_USERNAME AUTH_PASSWORD_HASH \
           LOG_LEVEL STALKER_REFRESH_INTERVAL WEBHOOK_ALLOW_PRIVATE_IPS; do
    LINE=$(grep -E "^${VAR}=" "$WORK/.env" | tail -n 1 || true)
    [ -n "$LINE" ] || continue
    VALUE=${LINE#*=}
    VALUE=${VALUE//\$\$/\$}
    VALUE=${VALUE#\'}; VALUE=${VALUE%\'}; VALUE=${VALUE#\"}; VALUE=${VALUE%\"}
    grep -vE "^${VAR}=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
    if [ "$VAR" = "AUTH_PASSWORD_HASH" ]; then
        # Single quotes keep python-dotenv from expanding the $ signs.
        printf "%s='%s'\n" "$VAR" "$VALUE" >> "$ENV_FILE.tmp"
    else
        printf "%s=%s\n" "$VAR" "$VALUE" >> "$ENV_FILE.tmp"
    fi
    mv "$ENV_FILE.tmp" "$ENV_FILE"
done
chown root:toolkit "$ENV_FILE"
chmod 640 "$ENV_FILE"

info "Starting unifi-toolkit (runs database migrations)"
systemctl start unifi-toolkit
PORT=$(grep -E "^APP_PORT=" "$ENV_FILE" | tail -n 1 | cut -d= -f2 | tr -d "'\"")
PORT=${PORT:-8000}
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        info "Migration done. Your tracked devices, events and settings are in the LXC install now."
        exit 0
    fi
    sleep 2
done
die "The toolkit did not start. See: journalctl -u unifi-toolkit -n 50"

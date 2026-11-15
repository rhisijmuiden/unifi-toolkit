#!/usr/bin/env bash
# UniFi Toolkit - install or update inside a Debian LXC container (or any Debian/Ubuntu host).
#
# Usage (as root, inside the container):
#   ./install.sh                         # local mode, http://<ip>:8000, no login
#   ./install.sh --domain toolkit.example.com
#                                        # production mode: login + HTTPS via Caddy
#   ./install.sh --branch test           # install the test branch instead of main
#
# Running it again updates an existing install in place and keeps the
# configuration (/opt/unifi-toolkit/.env) and data (/var/lib/unifi-toolkit).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rhisijmuiden/unifi-toolkit.git}"
BRANCH="${BRANCH:-main}"
DOMAIN="${DOMAIN:-}"
APP_DIR="/opt/unifi-toolkit"
DATA_DIR="/var/lib/unifi-toolkit"
SERVICE_USER="toolkit"
APP_PORT="${APP_PORT:-8000}"

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --repo)   REPO_URL="$2"; shift 2 ;;
        --port)   APP_PORT="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this script as root."
command -v apt-get >/dev/null || die "This installer supports Debian and Ubuntu only."

export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C.UTF-8 LANG=C.UTF-8

info "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
    git curl ca-certificates sqlite3 >/dev/null

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    info "Creating service user '$SERVICE_USER'"
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA_DIR"

if [ -d "$APP_DIR/.git" ]; then
    info "Updating $APP_DIR to branch '$BRANCH'"
    git config --global --add safe.directory "$APP_DIR"
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
else
    info "Cloning $REPO_URL ($BRANCH) to $APP_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    git config --global --add safe.directory "$APP_DIR"
fi

info "Installing Python dependencies (this can take a few minutes)"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

ENV_FILE="$APP_DIR/.env"
NEW_PASSWORD=""
if [ ! -f "$ENV_FILE" ]; then
    info "Writing configuration to $ENV_FILE"
    KEY=$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
    if [ -n "$DOMAIN" ]; then
        MODE="production"
        NEW_PASSWORD=$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(16))')
        HASH=$(NEW_PASSWORD="$NEW_PASSWORD" "$APP_DIR/.venv/bin/python" -c 'import bcrypt, os; print(bcrypt.hashpw(os.environ["NEW_PASSWORD"].encode(), bcrypt.gensalt()).decode())')
        info "Created login admin / $NEW_PASSWORD (shown once)"
    else
        MODE="local"
        HASH=""
    fi
    cat > "$ENV_FILE" <<EOF
# UniFi Toolkit configuration (written by lxc/install.sh)
DEPLOYMENT_TYPE=$MODE
DOMAIN=${DOMAIN:-unifi-toolkit.local}
AUTH_USERNAME=admin
AUTH_PASSWORD_HASH='$HASH'
ENCRYPTION_KEY=$KEY
DATABASE_URL=sqlite+aiosqlite:///$DATA_DIR/unifi_toolkit.db
LOG_LEVEL=INFO
STALKER_REFRESH_INTERVAL=60
APP_PORT=$APP_PORT
EOF
else
    info "Keeping existing configuration in $ENV_FILE"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

info "Installing systemd service"
install -m 644 "$APP_DIR/lxc/unifi-toolkit.service" /etc/systemd/system/unifi-toolkit.service
install -m 644 "$APP_DIR/lxc/unifi-toolkit-update.service" /etc/systemd/system/unifi-toolkit-update.service
install -m 644 "$APP_DIR/lxc/unifi-toolkit-update.path" /etc/systemd/system/unifi-toolkit-update.path
systemctl daemon-reload
systemctl enable --quiet unifi-toolkit
systemctl enable --quiet --now unifi-toolkit-update.path
systemctl restart unifi-toolkit

if [ -n "$DOMAIN" ]; then
    if ! command -v caddy >/dev/null; then
        info "Installing Caddy for HTTPS"
        # Debian 13 and Ubuntu 24.04 ship Caddy; Debian 12 needs Caddy's own repo.
        if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
            apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
            curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
                | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
            curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
                > /etc/apt/sources.list.d/caddy-stable.list
            apt-get update -qq
            apt-get install -y -qq caddy >/dev/null
        fi
    fi
    install -d /var/log/caddy
    sed -e "s/{\$DOMAIN}/$DOMAIN/" -e "s/{\$APP_PORT}/$APP_PORT/" \
        "$APP_DIR/lxc/Caddyfile" > /etc/caddy/Caddyfile
    systemctl reload-or-restart caddy
fi

info "Waiting for the toolkit to start"
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; then
        IP=$(hostname -I | awk '{print $1}')
        echo
        info "UniFi Toolkit is running."
        if [ -n "$DOMAIN" ]; then
            echo "    URL:      https://$DOMAIN"
        else
            echo "    URL:      http://$IP:$APP_PORT"
        fi
        if [ -n "$NEW_PASSWORD" ]; then
            echo "    Login:    admin / $NEW_PASSWORD   (shown once, change it with reset_password.sh)"
        fi
        echo "    Logs:     journalctl -u unifi-toolkit -f"
        exit 0
    fi
    sleep 2
done
warn "The service did not answer on /health within 2 minutes."
journalctl -u unifi-toolkit -n 40 --no-pager || true
exit 1

#!/usr/bin/env bash
# UniFi Toolkit - create a Debian LXC container on a Proxmox VE host and install the toolkit in it.
#
# Run as root on the Proxmox host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/unifi-toolkit/main/lxc/create-ct.sh)"
#
# Every setting can be overridden with an environment variable, for example:
#   CTID=120 CT_HOSTNAME=unifi-toolkit STORAGE=local-zfs IP=192.168.1.50/24 GATEWAY=192.168.1.1 ./create-ct.sh
#
#   CTID              container id                (default: next free id)
#   CT_HOSTNAME       container hostname          (default: unifi-toolkit)
#   STORAGE           storage for the root disk   (default: local-lvm)
#   TEMPLATE_STORAGE  storage for the template    (default: local)
#   DISK_GB           root disk size in GB        (default: 8)
#   CORES / MEMORY    CPU cores / RAM in MB       (default: 2 / 1024)
#   BRIDGE / VLAN     network bridge / VLAN tag   (default: vmbr0 / none)
#   IP / GATEWAY      "dhcp" or CIDR + gateway    (default: dhcp)
#   BRANCH            git branch to install       (default: main)
#   DOMAIN            set for production mode with login + HTTPS via Caddy
set -euo pipefail

CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-unifi-toolkit}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
DISK_GB="${DISK_GB:-8}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-1024}"
BRIDGE="${BRIDGE:-vmbr0}"
VLAN="${VLAN:-}"
IP="${IP:-dhcp}"
GATEWAY="${GATEWAY:-}"
BRANCH="${BRANCH:-main}"
DOMAIN="${DOMAIN:-}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/rhisijmuiden/unifi-toolkit}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this script as root on the Proxmox host."
command -v pct >/dev/null || die "pct not found - run this on a Proxmox VE host."

[ -n "$CTID" ] || CTID=$(pvesh get /cluster/nextid)
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

info "Looking for the newest Debian template"
pveam update >/dev/null || true
TEMPLATE=$(pveam available --section system | awk '{print $2}' \
    | grep -E '^debian-(13|12)-standard_' | sort -V | tail -n 1)
[ -n "$TEMPLATE" ] || die "No Debian 12/13 template found in 'pveam available'."
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
    info "Downloading $TEMPLATE to $TEMPLATE_STORAGE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GATEWAY" ] && NET="$NET,gw=$GATEWAY"
[ -n "$VLAN" ] && NET="$NET,tag=$VLAN"

info "Creating container $CTID ($CT_HOSTNAME) from $TEMPLATE"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --swap 512 \
    --rootfs "$STORAGE:$DISK_GB" \
    --net0 "$NET" \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --start 1 \
    --tags unifi-toolkit \
    --description "UniFi Toolkit - https://github.com/rhisijmuiden/unifi-toolkit"

info "Waiting for the container network"
for _ in $(seq 1 30); do
    if pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1; then break; fi
    sleep 2
done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 \
    || die "Container $CTID has no working network/DNS. Check BRIDGE/IP settings."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/install.sh" ]; then
    pct push "$CTID" "$SCRIPT_DIR/install.sh" /root/install.sh --perms 755
else
    curl -fsSL "$REPO_RAW/$BRANCH/lxc/install.sh" -o /tmp/unifi-toolkit-install.sh
    pct push "$CTID" /tmp/unifi-toolkit-install.sh /root/install.sh --perms 755
    rm -f /tmp/unifi-toolkit-install.sh
fi

info "Installing UniFi Toolkit inside container $CTID"
pct exec "$CTID" -- apt-get update -qq
pct exec "$CTID" -- apt-get install -y -qq curl ca-certificates >/dev/null
ARGS=(--branch "$BRANCH")
[ -n "$DOMAIN" ] && ARGS+=(--domain "$DOMAIN")
pct exec "$CTID" -- bash /root/install.sh "${ARGS[@]}"

info "Done. Container $CTID starts automatically with the host."

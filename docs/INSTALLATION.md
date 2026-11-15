# UI Toolkit Installation Guide (LXC)

UI Toolkit runs as a native service in a Debian LXC container on Proxmox VE. There is no Docker image any more: the app runs from a git checkout in a Python virtual environment, managed by systemd.

| What | Where |
|------|-------|
| Application (git checkout) | `/opt/unifi-toolkit` |
| Python virtual environment | `/opt/unifi-toolkit/.venv` |
| Configuration | `/opt/unifi-toolkit/.env` |
| Database | `/var/lib/unifi-toolkit/unifi_toolkit.db` |
| Service | `unifi-toolkit.service` (runs as user `toolkit`) |
| HTTPS (production mode) | Caddy, `/etc/caddy/Caddyfile` |

---

## 1. Create the container on Proxmox

Run this as root in the shell of your Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/unifi-toolkit/main/lxc/create-ct.sh)"
```

The script downloads the newest Debian 13 (or 12) template, creates an unprivileged container with 2 cores, 1 GB RAM and an 8 GB disk on `vmbr0` with DHCP, starts it, and installs the toolkit inside it. At the end it prints the URL.

Override any setting with an environment variable:

```bash
CTID=120 CT_HOSTNAME=unifi-toolkit STORAGE=local-zfs \
IP=192.168.1.50/24 GATEWAY=192.168.1.1 VLAN=10 \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/unifi-toolkit/main/lxc/create-ct.sh)"
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `CTID` | next free id | Container id |
| `CT_HOSTNAME` | `unifi-toolkit` | Container hostname |
| `STORAGE` | `local-lvm` | Storage for the root disk |
| `TEMPLATE_STORAGE` | `local` | Storage for the Debian template |
| `DISK_GB` / `CORES` / `MEMORY` | `8` / `2` / `1024` | Disk (GB), CPU cores, RAM (MB) |
| `BRIDGE` / `VLAN` | `vmbr0` / none | Network bridge and VLAN tag |
| `IP` / `GATEWAY` | `dhcp` / none | `dhcp`, or an address in CIDR form plus gateway |
| `BRANCH` | `main` | `main` is production, `test` is the test channel |
| `DOMAIN` | none | Set it for production mode: login plus HTTPS via Caddy |

## 2. Or install into an existing Debian/Ubuntu container

```bash
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/unifi-toolkit/main/lxc/install.sh -o install.sh
bash install.sh                                 # local mode: http://<ip>:8000, no login
bash install.sh --domain toolkit.example.com    # production mode: login + HTTPS
```

In production mode the installer creates the user `admin` with a random password and prints it once. Change it with `./reset_password.sh` in `/opt/unifi-toolkit`, then `systemctl restart unifi-toolkit`.

For production mode the domain must point at the container, and ports 80 and 443 must reach it, so Let's Encrypt can issue the certificate.

## 3. Moving over from a Docker install

On the old Docker host:

```bash
cd /path/to/unifi-toolkit
docker compose down
tar czf toolkit-docker.tgz data .env
```

Copy `toolkit-docker.tgz` to the Proxmox host, then:

```bash
pct push <ctid> toolkit-docker.tgz /root/toolkit-docker.tgz
pct exec <ctid> -- /opt/unifi-toolkit/lxc/migrate-from-docker.sh /root/toolkit-docker.tgz
```

This copies the database and takes `ENCRYPTION_KEY`, the login and the deployment mode from the old `.env`. The key has to come along, otherwise the stored UniFi credentials can't be decrypted. The LXC install's own database and `.env` are kept as `*.before-migration-*` files.

---

## Configure UniFi Controller

1. Open UI Toolkit in your browser
2. Click the **Settings cog** in the dashboard header
3. Enter your UniFi controller details:

**Controller URL** (must be a local IP or hostname; `unifi.ui.com` cloud access is not supported):
   - `https://192.168.1.1` (no port needed for UniFi OS devices)

> **Note:** UI Toolkit requires **UniFi OS** (UDM, UCG, Cloud Key Gen2+). Standalone/self-hosted controllers are not supported.

**Authentication:**
   - **API Key (recommended)**: Generate in UniFi OS Settings → Admins
   - **Username/Password**: Supported as a fallback, but API key is preferred

**Other Settings:**
   - **Site ID**: Usually `default`. For multi-site, use the code from the URL (e.g. `7jnove4a` from `/network/7jnove4a/...`)
   - **Verify SSL**: Disable for self-signed certificates (most UniFi deployments)

Click **Test Connection** to check it.

---

## Managing the service

| Action | Command (inside the container) |
|--------|-------------------------------|
| Status | `systemctl status unifi-toolkit` |
| Logs | `journalctl -u unifi-toolkit -f` |
| Restart | `systemctl restart unifi-toolkit` |
| Stop | `systemctl stop unifi-toolkit` |
| Update | `bash /opt/unifi-toolkit/lxc/install.sh` |
| Switch to the test channel | `bash /opt/unifi-toolkit/lxc/install.sh --branch test` |
| Reset admin password | `cd /opt/unifi-toolkit && ./reset_password.sh && systemctl restart unifi-toolkit` |

Running `install.sh` again updates the checkout, the Python packages and the database schema, and keeps `.env` and the database. From the Proxmox host you can run any of these with `pct exec <ctid> -- <command>`.

Back up the container with Proxmox (`vzdump`) or at least copy `/opt/unifi-toolkit/.env` and `/var/lib/unifi-toolkit/`.

---

## Troubleshooting

- **The service doesn't start:** `journalctl -u unifi-toolkit -n 50`. A missing `ENCRYPTION_KEY` in `.env` is the most common cause.
- **"Host header not allowed":** the toolkit only answers an IP, `localhost`, a `.local`-style name or the `DOMAIN` from `.env`. Add other names to `ALLOWED_HOSTS` in `.env` (the exact hostname you type in the browser) and restart.
- **Can't connect to the UniFi controller:** test from the container with `curl -k https://<controller-ip>`. Check the VLAN and firewall between the container and the console.
- **Let's Encrypt fails:** check that the domain resolves to the container and that ports 80 and 443 are forwarded to it. Caddy logs: `journalctl -u caddy -f`.
- **Reset the database (last resort):** `systemctl stop unifi-toolkit && rm /var/lib/unifi-toolkit/unifi_toolkit.db && systemctl start unifi-toolkit`.

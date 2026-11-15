"""
Info, update check and in-app updates.

The LXC install runs the app from a git checkout. Releases are branches:
`main` is production and `test` is the test channel. The app itself runs as
an unprivileged user, so it never updates itself: it writes a request file
that the root-owned `unifi-toolkit-update.path` unit picks up, and
`lxc/update.sh` does the fetch, dependency install, restart and rollback.
This router only reads git state and that script's status file.
"""
import asyncio
import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])

APP_DIR = Path(__file__).resolve().parent.parent.parent
CHANNELS = {"main": "Production", "test": "Test"}
# Shared with lxc/update.sh and the systemd path unit.
STATE_DIR = Path(os.getenv("TOOLKIT_STATE_DIR", "/var/lib/unifi-toolkit"))
REQUEST_FILE = STATE_DIR / "update-request"
STATUS_FILE = STATE_DIR / "update-status.json"
LOG_FILE = STATE_DIR / "update.log"
UPDATER_UNIT = Path("/etc/systemd/system/unifi-toolkit-update.path")

FETCH_TTL = 300  # seconds between real `git fetch` calls
_last_fetch: dict = {}


async def _git(*args: str, timeout: float = 30) -> Optional[str]:
    """Run git in the app directory. Returns stdout, or None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(APP_DIR), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode().strip()


def _versions() -> dict:
    from app import __version__ as app_version
    from tools.wifi_stalker import __version__ as stalker
    from tools.threat_watch import __version__ as threats
    from tools.network_pulse import __version__ as pulse
    from tools.house_arrest import __version__ as arrest
    return {
        "app": app_version,
        "tools": {
            "Wi-Fi Stalker": stalker,
            "Threat Watch": threats,
            "Network Pulse": pulse,
            "House Arrest": arrest,
        },
    }


def _version_from_pyproject(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _updater_available() -> tuple[bool, Optional[str]]:
    if not (APP_DIR / ".git").exists():
        return False, "This install is not a git checkout, so it can't update itself."
    if not UPDATER_UNIT.exists():
        return False, ("The updater service is not installed. Run "
                       "`bash /opt/unifi-toolkit/lxc/install.sh` once as root to add it.")
    return True, None


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}


async def _current_channel() -> Optional[str]:
    return await _git("rev-parse", "--abbrev-ref", "HEAD")


@router.get("/info")
async def system_info():
    """What is running: versions, channel, commit and environment."""
    branch = await _current_channel()
    commit = await _git("rev-parse", "--short", "HEAD")
    commit_date = await _git("log", "-1", "--format=%cI")
    available, reason = _updater_available()
    return {
        **_versions(),
        "channel": branch,
        "channel_label": CHANNELS.get(branch or "", branch),
        "channels": [{"id": k, "label": v} for k, v in CHANNELS.items()],
        "commit": commit,
        "commit_date": commit_date,
        "python": platform.python_version(),
        "deployment": os.getenv("DEPLOYMENT_TYPE", "local"),
        "updater_available": available,
        "updater_reason": reason,
        "update": _read_status(),
    }


async def _fetch(channel: str, force: bool) -> bool:
    now = time.monotonic()
    if not force and now - _last_fetch.get(channel, 0) < FETCH_TTL:
        return True
    ok = await _git("fetch", "--quiet", "origin",
                    f"+refs/heads/{channel}:refs/remotes/origin/{channel}", timeout=60) is not None
    if ok:
        _last_fetch[channel] = now
    return ok


@router.get("/updates")
async def check_updates(channel: Optional[str] = None, refresh: bool = False):
    """Compare the running commit with the newest commit on a channel."""
    current = await _current_channel()
    channel = channel or (current if current in CHANNELS else "main")
    if channel not in CHANNELS:
        raise HTTPException(status_code=400, detail="Unknown channel")
    if not (APP_DIR / ".git").exists():
        return {"channel": channel, "checked": False,
                "error": "This install is not a git checkout."}

    if not await _fetch(channel, refresh):
        return {"channel": channel, "checked": False,
                "error": "Could not reach GitHub to check for updates."}

    remote_ref = f"origin/{channel}"
    remote_commit = await _git("rev-parse", "--short", remote_ref)
    if remote_commit is None:
        return {"channel": channel, "checked": False,
                "error": f"The channel '{channel}' does not exist on GitHub yet."}

    head = await _git("rev-parse", "HEAD")
    remote_full = await _git("rev-parse", remote_ref)
    behind = int(await _git("rev-list", "--count", f"HEAD..{remote_ref}") or 0)
    log = await _git("log", "--no-merges", "--format=%h%x09%cI%x09%s", "-n", "15",
                     f"HEAD..{remote_ref}") or ""
    changes = []
    for line in log.splitlines():
        sha, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        changes.append({"commit": sha, "date": date, "subject": subject})

    latest_version = _version_from_pyproject(
        await _git("show", f"{remote_ref}:pyproject.toml"))
    switching = channel != current
    return {
        "channel": channel,
        "channel_label": CHANNELS[channel],
        "checked": True,
        "current_channel": current,
        "current_commit": (head or "")[:7],
        "current_version": _versions()["app"],
        "latest_commit": remote_commit,
        "latest_version": latest_version,
        "commits_behind": behind,
        "update_available": switching or (head != remote_full),
        "switching_channel": switching,
        "changes": changes,
    }


class UpdateRequest(BaseModel):
    channel: str


@router.post("/update")
async def start_update(req: UpdateRequest):
    """Ask the root updater to move this install to the newest commit of a channel."""
    if req.channel not in CHANNELS:
        raise HTTPException(status_code=400, detail="Unknown channel")
    available, reason = _updater_available()
    if not available:
        raise HTTPException(status_code=409, detail=reason)
    if _read_status().get("state") == "running":
        raise HTTPException(status_code=409, detail="An update is already running.")
    try:
        STATUS_FILE.write_text(json.dumps({
            "state": "queued", "channel": req.channel,
            "message": "Waiting for the updater to start",
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }))
        REQUEST_FILE.write_text(req.channel + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not queue the update: {e}")
    return {"queued": True, "channel": req.channel}


@router.get("/update/status")
async def update_status():
    status = _read_status()
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()[-40:]
    except OSError:
        lines = []
    return {**status, "log": lines}

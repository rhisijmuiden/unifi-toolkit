"""
Backups page and API: schedule, passphrase, remote locations (FTP, FTPS,
SCP), back up now, history, and restore from a local or remote backup.

Secrets (passphrase, location passwords and keys) are stored encrypted and
never returned: the API says `has_passphrase` / `has_password` instead.
"""
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from shared.backup import archive as A
from shared.backup import service as S
from shared.backup.archive import BackupError
from shared.backup.transports import KINDS, transport_for
from shared.crypto import encrypt_password
from shared.database import get_database
from shared.models.backup import BackupDestination, BackupRun
from shared.templating import make_templates

router = APIRouter(tags=["backup"])
templates = make_templates(str(Path(__file__).resolve().parent.parent / "templates"))

MIN_PASSPHRASE = 12
_bg: set = set()


class SettingsIn(BaseModel):
    enabled: bool = False
    run_time: str = "03:00"
    full_every_days: int = Field(7, ge=1, le=90)
    keep_chains: int = Field(4, ge=1, le=100)
    notify_on_failure: bool = True
    passphrase: Optional[str] = None  # None or "" keeps the saved one


class DestinationIn(BaseModel):
    name: str
    kind: str
    host: str
    port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None  # None or "" keeps the saved one
    clear_password: bool = False
    private_key: Optional[str] = None  # None or "" keeps the saved one
    clear_private_key: bool = False
    verify_tls: bool = True
    path: str = ""
    enabled: bool = True


class RunIn(BaseModel):
    kind: str = "auto"


class RestoreIn(BaseModel):
    source: Union[int, str]
    name: str
    passphrase: Optional[str] = None
    confirm: bool = False


def _settings_dict(row) -> dict:
    return {
        "enabled": bool(row.enabled),
        "run_time": row.run_time or "03:00",
        "full_every_days": row.full_every_days or 7,
        "keep_chains": row.keep_chains or 4,
        "notify_on_failure": bool(row.notify_on_failure),
        "has_passphrase": bool(row.passphrase_encrypted),
    }


def _fingerprint(host_key: Optional[str]) -> Optional[str]:
    if not host_key:
        return None
    try:
        import asyncssh
        return asyncssh.import_public_key(host_key).get_fingerprint("sha256")
    except Exception:
        return None


def _dest_dict(d: BackupDestination) -> dict:
    return {
        "id": d.id, "name": d.name, "kind": d.kind, "host": d.host, "port": d.port,
        "username": d.username or "", "has_password": bool(d.password_encrypted),
        "has_private_key": bool(d.private_key_encrypted),
        "host_key": d.host_key, "fingerprint": _fingerprint(d.host_key), "verify_tls": bool(d.verify_tls), "path": d.path or "",
        "enabled": bool(d.enabled), "last_status": d.last_status, "last_message": d.last_message,
        "last_run_at": S.iso_utc(d.last_run_at),
    }


async def _dest(session, dest_id: int) -> BackupDestination:
    d = await session.get(BackupDestination, dest_id)
    if d is None:
        raise HTTPException(404, "That backup location does not exist.")
    return d


@router.get("/system/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    return templates.TemplateResponse(request, "system/backups.html", {})


# ---------------------------------------------------------------- settings

@router.get("/api/backup/settings")
async def get_settings():
    async for session in get_database().get_session():
        row = await S.get_settings_row(session)
        await session.commit()
        return _settings_dict(row)


@router.put("/api/backup/settings")
async def put_settings(data: SettingsIn):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", data.run_time or ""):
        raise HTTPException(422, "Enter the time as HH:MM, for example 03:00.")
    if data.passphrase and len(data.passphrase) < MIN_PASSPHRASE:
        raise HTTPException(422, f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    async for session in get_database().get_session():
        row = await S.get_settings_row(session)
        if data.enabled and not (data.passphrase or row.passphrase_encrypted):
            raise HTTPException(422, "Set a backup passphrase first.")
        if data.enabled and not row.enabled:
            # Don't fire straight away for a time that already passed today.
            now = datetime.now()
            if now.strftime("%H:%M") >= data.run_time:
                row.last_scheduled_date = now.strftime("%Y-%m-%d")
        row.enabled = data.enabled
        row.run_time = data.run_time
        row.full_every_days = data.full_every_days
        row.keep_chains = data.keep_chains
        row.notify_on_failure = data.notify_on_failure
        if data.passphrase:
            changed = bool(row.passphrase_encrypted)
            row.passphrase_encrypted = encrypt_password(data.passphrase)
            if changed:
                # A chain must use one passphrase: the next backup starts a new chain.
                S._save_index({})
        await session.commit()
        return _settings_dict(row)


# ---------------------------------------------------------------- locations

def _validate_dest(data: DestinationIn, existing: Optional[BackupDestination]) -> None:
    if data.kind not in KINDS:
        raise HTTPException(422, "Choose FTP, FTPS or SCP.")
    if not data.name.strip():
        raise HTTPException(422, "Give the location a name.")
    if not data.host.strip() or re.search(r"[\s/]", data.host.strip()):
        raise HTTPException(422, "Enter the server's host name or IP address.")
    if data.kind == "scp" and not (data.username or "").strip():
        raise HTTPException(422, "SCP needs a username.")
    if data.kind == "scp":
        has_secret = (data.password or data.private_key
                      or (existing and not data.clear_password and existing.password_encrypted)
                      or (existing and not data.clear_private_key and existing.private_key_encrypted))
        if not has_secret:
            raise HTTPException(422, "SCP needs a password or a private key.")
    if ".." in (data.path or "").split("/"):
        raise HTTPException(422, "The folder may not contain '..'.")


def _apply_dest(d: BackupDestination, data: DestinationIn) -> None:
    changed_server = (d.host, d.port, d.kind) != (data.host.strip(), data.port or KINDS[data.kind], data.kind)
    d.name = data.name.strip()
    d.kind = data.kind
    d.host = data.host.strip()
    d.port = data.port or KINDS[data.kind]
    d.username = (data.username or "").strip() or None
    if data.clear_password:
        d.password_encrypted = None
    elif data.password:
        d.password_encrypted = encrypt_password(data.password)
    if data.kind != "scp" or data.clear_private_key:
        d.private_key_encrypted = None
    elif data.private_key and data.private_key.strip():
        d.private_key_encrypted = encrypt_password(data.private_key.strip() + "\n")
    if changed_server or data.kind != "scp":
        d.host_key = None
    d.verify_tls = data.verify_tls
    d.path = (data.path or "").strip()
    d.enabled = data.enabled


@router.get("/api/backup/destinations")
async def list_destinations():
    async for session in get_database().get_session():
        rows = (await session.execute(select(BackupDestination).order_by(BackupDestination.id))).scalars().all()
        return [_dest_dict(d) for d in rows]


@router.post("/api/backup/destinations")
async def create_destination(data: DestinationIn):
    _validate_dest(data, None)
    async for session in get_database().get_session():
        d = BackupDestination()
        _apply_dest(d, data)
        session.add(d)
        await session.commit()
        return _dest_dict(d)


@router.put("/api/backup/destinations/{dest_id}")
async def update_destination(dest_id: int, data: DestinationIn):
    async for session in get_database().get_session():
        d = await _dest(session, dest_id)
        _validate_dest(data, d)
        _apply_dest(d, data)
        await session.commit()
        return _dest_dict(d)


@router.delete("/api/backup/destinations/{dest_id}")
async def delete_destination(dest_id: int):
    async for session in get_database().get_session():
        d = await _dest(session, dest_id)
        await session.delete(d)
        await session.commit()
        return {"deleted": True}


@router.delete("/api/backup/destinations/{dest_id}/host-key")
async def forget_host_key(dest_id: int):
    async for session in get_database().get_session():
        d = await _dest(session, dest_id)
        d.host_key = None
        await session.commit()
        return _dest_dict(d)


@router.post("/api/backup/destinations/{dest_id}/test")
async def test_destination(dest_id: int):
    async for session in get_database().get_session():
        d = await _dest(session, dest_id)
        try:
            result = await transport_for(S.location_of(d)).test()
        except BackupError as e:
            d.last_status, d.last_message = "failed", str(e)
            await session.commit()
            raise HTTPException(502, str(e))
        if d.kind == "scp" and not d.host_key and result.get("host_key"):
            d.host_key = result["host_key"]
        d.last_status, d.last_message = "ok", "Connection and write test passed."
        await session.commit()
        return {"ok": True, "fingerprint": result.get("fingerprint"), "destination": _dest_dict(d)}


@router.get("/api/backup/destinations/{dest_id}/backups")
async def remote_backups(dest_id: int):
    async for session in get_database().get_session():
        d = await _dest(session, dest_id)
        try:
            return await S.list_remote(d)
        except BackupError as e:
            raise HTTPException(502, str(e))


# ---------------------------------------------------------------- run, history, local

@router.post("/api/backup/run", status_code=202)
async def run_now(data: RunIn):
    if data.kind not in ("auto", "full", "incremental"):
        raise HTTPException(422, "Choose full or incremental.")
    if S.is_running():
        raise HTTPException(409, "A backup or restore is already running.")
    async for session in get_database().get_session():
        row = await S.get_settings_row(session)
        await session.commit()
        if not row.passphrase_encrypted:
            raise HTTPException(422, "Set a backup passphrase first.")

    async def job():
        try:
            await S.run_backup(data.kind, trigger="manual")
        except BackupError:
            pass
    task = asyncio.create_task(job())
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    await asyncio.sleep(0.05)  # let the job take the lock and record the run
    return {"started": True}


@router.get("/api/backup/status")
async def status(limit: int = 15):
    async for session in get_database().get_session():
        runs = (await session.execute(
            select(BackupRun).order_by(BackupRun.id.desc()).limit(max(1, min(limit, 100))))).scalars().all()
        return {
            "running": S.is_running(),
            "runs": [S.run_dict(r) for r in runs],
            "local": S.list_local(),
            "last_restore": S.last_restore(),
        }


@router.get("/api/backup/local/{name}")
async def download_local(name: str):
    if not A.parse_name(name) or name not in S.local_names():
        raise HTTPException(404, "No such backup.")
    return FileResponse(S.backup_dir() / name, media_type="application/octet-stream", filename=name)


@router.post("/api/backup/restore")
async def restore(data: RestoreIn):
    if not data.confirm:
        raise HTTPException(422, "Confirm the restore first.")
    try:
        run = await S.restore(data.source, data.name, data.passphrase or None)
    except BackupError as e:
        raise HTTPException(409, str(e))
    if run["status"] != "ok":
        raise HTTPException(422, run["message"])
    S.schedule_restart()
    return run

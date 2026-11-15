"""
Backup jobs: make backups, copy them to the remote locations, apply
retention, restore, and run the daily schedule.

Local copies live in $TOOLKIT_STATE_DIR/backups (default
/var/lib/unifi-toolkit/backups) with an index.json of their manifests, so an
incremental backup knows which chunks its chain already holds without
downloading anything. Every run copies whatever a remote location is missing,
so a location that was offline catches up on the next run.

A restore rebuilds the database into $TOOLKIT_STATE_DIR/restore/pending.db
and restarts the toolkit; run.py swaps it in before the migrations run (see
apply_pending_restore). The current database is kept next to it first.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select

from shared.backup import archive as A
from shared.backup.archive import BackupError
from shared.backup.transports import Location, transport_for
from shared.crypto import decrypt_password
from shared.database import get_database
from shared.models.backup import BackupDestination, BackupRun, BackupSettings

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent.parent
RESTART_EXIT_CODE = 75  # non-zero, so systemd (Restart=on-failure) starts the toolkit again

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------- paths

def state_dir() -> Path:
    return Path(os.getenv("TOOLKIT_STATE_DIR", "/var/lib/unifi-toolkit"))


def backup_dir() -> Path:
    return state_dir() / "backups"


def restore_dir() -> Path:
    return state_dir() / "restore"


def db_path() -> Path:
    from shared.config import get_settings
    url = os.getenv("DATABASE_URL") or get_settings().database_url
    if not url.startswith("sqlite"):
        raise BackupError("Backups need the SQLite database.")
    return Path(url.split("///", 1)[-1]).resolve()


def _index_file() -> Path:
    return backup_dir() / "index.json"


def load_index() -> Dict[str, dict]:
    try:
        return json.loads(_index_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_index(index: Dict[str, dict]) -> None:
    backup_dir().mkdir(parents=True, exist_ok=True)
    tmp = _index_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=1))
    os.replace(tmp, _index_file())


def local_names() -> List[str]:
    d = backup_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if A.parse_name(p.name))


# ---------------------------------------------------------------- settings

async def get_settings_row(session) -> BackupSettings:
    row = (await session.execute(select(BackupSettings).where(BackupSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = BackupSettings(id=1, enabled=False, run_time="03:00", full_every_days=7, keep_chains=4,
                             notify_on_failure=True)
        session.add(row)
        await session.flush()
    return row


def location_of(d: BackupDestination) -> Location:
    return Location(
        kind=d.kind, host=d.host, port=d.port, username=d.username,
        password=decrypt_password(d.password_encrypted) if d.password_encrypted else None,
        private_key=decrypt_password(d.private_key_encrypted) if d.private_key_encrypted else None,
        host_key=d.host_key, verify_tls=d.verify_tls if d.verify_tls is not None else True, path=d.path or "",
    )


def _passphrase(row: BackupSettings) -> str:
    if not row.passphrase_encrypted:
        raise BackupError("Set a backup passphrase first.")
    try:
        return decrypt_password(row.passphrase_encrypted)
    except Exception:
        raise BackupError("The saved passphrase can't be read (was ENCRYPTION_KEY changed?). Save the passphrase again.")


# ---------------------------------------------------------------- backup

def _alembic_revision(path: Path) -> Optional[str]:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _choose_kind(requested: str, row: BackupSettings, index: Dict[str, dict]) -> str:
    chains = A.group_chains(n for n in local_names() if n in index)
    if requested == "full" or not chains:
        return "full"
    if requested == "incremental":
        return "incremental"
    last_full = datetime.strptime(chains[-1][0]["stamp"], "%Y%m%d-%H%M%S")
    if datetime.now() - last_full >= timedelta(days=max(1, row.full_every_days or 7)) - timedelta(minutes=30):
        return "full"
    return "incremental"


def _new_stamp() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while any(n.startswith(f"unifi-toolkit-{stamp}-") for n in local_names()):
        stamp = (datetime.strptime(stamp, "%Y%m%d-%H%M%S") + timedelta(seconds=1)).strftime("%Y%m%d-%H%M%S")
    return stamp


def _build(kind: str, passphrase: str, index: Dict[str, dict]) -> A.Built:
    from app import __version__
    from shared.config import get_settings
    known, chain, parent = [], None, None
    if kind == "incremental":
        last_chain = A.group_chains(n for n in local_names() if n in index)[-1]
        chain = last_chain[0]["name"]
        parent = last_chain[-1]["name"]
        for item in last_chain:
            known.extend(index[item["name"]].get("stored_chunks", []))
    src = db_path()
    meta = {
        "app_version": __version__,
        "alembic_revision": _alembic_revision(src),
        "encryption_key": get_settings().encryption_key,
    }
    return A.build_backup(src, backup_dir() / ".work", kind=kind, stamp=_new_stamp(), passphrase=passphrase,
                          known_chunks=known, chain=chain, parent=parent, meta=meta)


def _kept_chains(names: List[str], keep: int) -> List[List[dict]]:
    chains = A.group_chains(names)
    return chains[-max(1, keep):]


def _apply_local_retention(keep: int, index: Dict[str, dict]) -> None:
    kept = {i["name"] for c in _kept_chains(local_names(), keep) for i in c}
    for n in local_names():
        if n not in kept:
            (backup_dir() / n).unlink(missing_ok=True)
            index.pop(n, None)
    for n in list(index):
        if n not in kept:
            index.pop(n)


async def _sync_destination(dest: BackupDestination, keep: int) -> str:
    """Upload what the location is missing and delete chains past retention there."""
    t = transport_for(location_of(dest))
    remote = await t.list()
    local = local_names()
    kept_local = [i["name"] for c in _kept_chains(local, keep) for i in c]
    uploaded = 0
    for n in kept_local:
        if n not in remote:
            await t.upload(backup_dir() / n, n)
            uploaded += 1
    all_names = sorted(set(remote) | set(kept_local))
    kept = {i["name"] for c in _kept_chains(all_names, keep) for i in c}
    removed = 0
    for n in remote:
        if A.parse_name(n) and n not in kept:
            await t.delete(n)
            removed += 1
        elif n.endswith(".utkb.part") and A.parse_name(n[:-5]):
            await t.delete(n)  # leftover of a broken upload
    parts = [f"{uploaded} uploaded"]
    if removed:
        parts.append(f"{removed} removed by retention")
    return ", ".join(parts)


async def sync_destinations(session, row: BackupSettings, only_id: Optional[int] = None) -> List[dict]:
    q = select(BackupDestination).where(BackupDestination.enabled.is_(True))
    if only_id is not None:
        q = select(BackupDestination).where(BackupDestination.id == only_id)
    results = []
    for dest in (await session.execute(q)).scalars().all():
        try:
            msg = await _sync_destination(dest, row.keep_chains or 4)
            dest.last_status, dest.last_message = "ok", msg
        except BackupError as e:
            dest.last_status, dest.last_message = "failed", str(e)
        except Exception as e:  # never let one location stop the others
            logger.exception("Backup sync to %s failed", dest.name)
            dest.last_status, dest.last_message = "failed", f"Unexpected error: {e}"
        dest.last_run_at = datetime.now(timezone.utc)
        results.append({"id": dest.id, "name": dest.name, "status": dest.last_status, "message": dest.last_message})
    await session.commit()
    return results


async def run_backup(kind: str = "auto", trigger: str = "manual") -> dict:
    """Make a backup and copy it everywhere. Returns the run as a dict."""
    if _lock.locked():
        raise BackupError("A backup or restore is already running.")
    async with _lock:
        async for session in get_database().get_session():
            row = await get_settings_row(session)
            run = BackupRun(action="backup", trigger=trigger, status="running")
            session.add(run)
            await session.commit()
            try:
                passphrase = _passphrase(row)
                index = load_index()
                chosen = _choose_kind(kind, row, index)
                built = await asyncio.to_thread(_build, chosen, passphrase, index)
                backup_dir().mkdir(parents=True, exist_ok=True)
                os.replace(built.path, backup_dir() / built.name)
                os.chmod(backup_dir() / built.name, 0o640)
                entry = {k: v for k, v in built.manifest.items() if k != "encryption_key"}
                index[built.name] = entry
                _apply_local_retention(row.keep_chains or 4, index)
                _save_index(index)
                run.kind, run.name = chosen, built.name
                run.size = (backup_dir() / built.name).stat().st_size
                results = await sync_destinations(session, row)
                failed = [r for r in results if r["status"] != "ok"]
                note = ""
                if kind == "incremental" and chosen == "full":
                    note = "No full backup to build on, so this is a full backup. "
                if failed:
                    run.status = "partial"
                    run.message = note + "Not copied to: " + "; ".join(f"{r['name']} ({r['message']})" for r in failed)
                else:
                    run.status = "ok"
                    run.message = note + (f"Copied to {len(results)} location(s)." if results
                                          else "Kept locally only: no remote location is set up.")
            except BackupError as e:
                run.status, run.message = "failed", str(e)
            except Exception as e:
                logger.exception("Backup failed")
                run.status, run.message = "failed", f"Unexpected error: {e}"
            finally:
                shutil.rmtree(backup_dir() / ".work", ignore_errors=True)
            run.finished_at = datetime.now(timezone.utc)
            await session.commit()
            result = run_dict(run)
            if run.status != "ok" and row.notify_on_failure:
                await _notify(result)
            return result


async def _notify(run: dict) -> None:
    from shared import mailer
    try:
        cfg = await mailer.load_config()
        if cfg is None:
            return
        async for session in get_database().get_session():
            from shared.models.mail_settings import MailSettings
            ms = (await session.execute(select(MailSettings).where(MailSettings.id == 1))).scalar_one_or_none()
            to = mailer.split_recipients(ms.default_recipients if ms else None)
        if not to:
            return
        word = "failed" if run["status"] == "failed" else "was not copied everywhere"
        text = f"The UniFi Toolkit backup {word}.\n\n{run['message']}\n"
        body = (f"<p>The UniFi Toolkit backup {word}.</p>"
                f"<p>{mailer.html.escape(run['message'] or '')}</p>")
        await mailer.send_mail(to, f"UniFi Toolkit backup {word}", text,
                               mailer.render_html("Backup problem", body), cfg=cfg)
    except Exception:
        logger.exception("Could not send the backup failure mail")


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """SQLite gives the stored UTC times back without a zone; say they are UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def run_dict(r: BackupRun) -> dict:
    return {
        "id": r.id, "action": r.action, "kind": r.kind, "trigger": r.trigger, "name": r.name,
        "status": r.status, "size": r.size, "message": r.message,
        "started_at": iso_utc(r.started_at),
        "finished_at": iso_utc(r.finished_at),
    }


# ---------------------------------------------------------------- listing

def describe(names: List[str], sizes: Optional[Dict[str, int]] = None) -> List[dict]:
    """Chains as the UI shows them, newest first."""
    out = []
    for chain in reversed(A.group_chains(names)):
        out.append({
            "full": chain[0]["name"],
            "items": [dict(i, size=(sizes or {}).get(i["name"]),
                           created=datetime.strptime(i["stamp"], "%Y%m%d-%H%M%S").astimezone().isoformat())
                      for i in reversed(chain)],
        })
    return out


def list_local() -> List[dict]:
    sizes = {n: (backup_dir() / n).stat().st_size for n in local_names()}
    return describe(list(sizes), sizes)


async def list_remote(dest: BackupDestination) -> List[dict]:
    names = await transport_for(location_of(dest)).list()
    return describe([n for n in names if A.parse_name(n)])


# ---------------------------------------------------------------- restore

def _check_revision(rev: Optional[str]) -> None:
    if not rev:
        return
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.util import CommandError
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "alembic"))
    try:
        if ScriptDirectory.from_config(cfg).get_revision(rev) is None:
            raise CommandError(rev)
    except CommandError:
        raise BackupError("This backup was made by a newer version of the toolkit. "
                          "Update the toolkit first, then restore.")


async def restore(source, name: str, passphrase: Optional[str] = None) -> dict:
    """Rebuild the database from a backup and stage it. `source` is "local" or a destination id."""
    if not A.parse_name(name):
        raise BackupError("Not a backup file name.")
    if _lock.locked():
        raise BackupError("A backup or restore is already running.")
    from shared.config import get_settings
    async with _lock:
        async for session in get_database().get_session():
            row = await get_settings_row(session)
            run = BackupRun(action="restore", trigger="manual", name=name, status="running")
            session.add(run)
            await session.commit()
            work = restore_dir() / "work"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            try:
                phrase = passphrase or _passphrase(row)
                if source == "local":
                    files = [backup_dir() / n for n in A.chain_for(local_names(), name)]
                    where = "this server"
                else:
                    dest = await session.get(BackupDestination, int(source))
                    if dest is None:
                        raise BackupError("That backup location no longer exists.")
                    t = transport_for(location_of(dest))
                    files = []
                    for n in A.chain_for(await t.list(), name):
                        await t.download(n, work / n)
                        files.append(work / n)
                    where = dest.name
                pending = restore_dir() / "pending.db"
                manifest = await asyncio.to_thread(A.assemble, files, phrase, work / "rebuilt.db", work)
                _check_revision(manifest.get("alembic_revision"))
                changed = A.reencrypt_credentials(work / "rebuilt.db", manifest.get("encryption_key"),
                                                  get_settings().encryption_key)
                os.replace(work / "rebuilt.db", pending)
                (restore_dir() / "pending.json").write_text(json.dumps({
                    "name": name, "from": where, "created": manifest.get("created"),
                    "app_version": manifest.get("app_version"), "reencrypted": changed,
                }))
                run.kind = manifest.get("kind")
                run.status = "ok"
                run.message = f"Restored {name} from {where}. The toolkit restarts to load it."
            except BackupError as e:
                run.status, run.message = "failed", str(e)
            except Exception as e:
                logger.exception("Restore failed")
                run.status, run.message = "failed", f"Unexpected error: {e}"
            finally:
                shutil.rmtree(work, ignore_errors=True)
            run.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return run_dict(run)


def schedule_restart(delay: float = 1.5) -> None:
    """Exit after the response is sent; systemd starts the toolkit again and run.py swaps the database in."""
    loop = asyncio.get_running_loop()
    loop.call_later(delay, os._exit, RESTART_EXIT_CODE)


def apply_pending_restore() -> Optional[str]:
    """Called by run.py before the migrations: swap a staged restore in. Returns a log line or None."""
    pending = restore_dir() / "pending.db"
    if not pending.exists():
        return None
    target = db_path()
    info = {}
    try:
        info = json.loads((restore_dir() / "pending.json").read_text())
    except (OSError, ValueError):
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if target.exists():
        shutil.copy2(target, restore_dir() / f"before-restore-{stamp}.db")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    os.replace(pending, target)
    (restore_dir() / "pending.json").unlink(missing_ok=True)
    info.update({"applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "previous_copy": f"before-restore-{stamp}.db"})
    # The restore's own history line was in the database that was just replaced.
    try:
        con = sqlite3.connect(str(target))
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
            con.execute(
                "INSERT INTO backup_runs (action, kind, \"trigger\", name, status, message, started_at, finished_at)"
                " VALUES ('restore', NULL, 'manual', ?, 'ok', ?, ?, ?)",
                (info.get("name"), f"Restored {info.get('name', '?')} from {info.get('from', '?')}. "
                 f"The previous database is kept as {info['previous_copy']}.", now, now))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass  # a backup from before the backup tables existed; the migrations add them
    (restore_dir() / "last.json").write_text(json.dumps(info))
    # Keep the three newest safety copies.
    for old in sorted(restore_dir().glob("before-restore-*.db"))[:-3]:
        old.unlink(missing_ok=True)
    return f"Restored database from backup {info.get('name', '?')}; previous database kept as {info['previous_copy']}"


def last_restore() -> Optional[dict]:
    try:
        return json.loads((restore_dir() / "last.json").read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- schedule

def is_running() -> bool:
    return _lock.locked()


async def _tick() -> None:
    async for session in get_database().get_session():
        row = await get_settings_row(session)
        await session.commit()
        if not row.enabled or not row.passphrase_encrypted:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        try:
            hh, mm = (int(x) for x in (row.run_time or "03:00").split(":"))
        except ValueError:
            hh, mm = 3, 0
        if row.last_scheduled_date == today or (now.hour, now.minute) < (hh, mm):
            return
        row.last_scheduled_date = today
        await session.commit()
    try:
        await run_backup("auto", trigger="schedule")
    except BackupError as e:
        logger.warning("Scheduled backup skipped: %s", e)


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Backup scheduler tick failed")
        await asyncio.sleep(30)


async def mark_interrupted() -> None:
    """Runs still marked running were cut off by a restart (or captured mid-run by a backup)."""
    from sqlalchemy import update
    async for session in get_database().get_session():
        await session.execute(update(BackupRun).where(BackupRun.status == "running").values(
            status="failed", message="Interrupted: the toolkit restarted before this finished.",
            finished_at=datetime.now(timezone.utc)))
        await session.commit()


async def start_scheduler() -> None:
    global _task
    try:
        await mark_interrupted()
    except Exception:
        logger.exception("Could not tidy up interrupted backup runs")
    if _task is None:
        _task = asyncio.create_task(_loop())


async def stop_scheduler() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None

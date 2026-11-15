"""
Alerts page and API: which alerts are mailed, the weekly report, the
devices seen on the network and the alert log.
"""
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from shared import alerts as AL
from shared import mailer
from shared.database import get_database
from shared.models.alerts import AlertLog, KnownDevice
from shared.templating import make_templates

router = APIRouter(tags=["alerts"])
templates = make_templates(str(Path(__file__).resolve().parent.parent / "templates"))


class AlertSettingsIn(BaseModel):
    recipients: Optional[str] = None
    unknown_device_enabled: bool = False
    threat_enabled: bool = False
    threat_min_severity: int = Field(1, ge=1, le=3)
    console_enabled: bool = False
    report_enabled: bool = False
    report_weekday: int = Field(0, ge=0, le=6)
    report_time: str = "08:00"


class ApproveIn(BaseModel):
    approved: bool = True


def _settings_dict(row, mail_ready: bool) -> dict:
    return {
        "recipients": row.recipients or "",
        "unknown_device_enabled": bool(row.unknown_device_enabled),
        "threat_enabled": bool(row.threat_enabled),
        "threat_min_severity": row.threat_min_severity or 1,
        "console_enabled": bool(row.console_enabled),
        "report_enabled": bool(row.report_enabled),
        "report_weekday": row.report_weekday or 0,
        "report_time": row.report_time or "08:00",
        "devices_seeded": bool(row.devices_seeded),
        "mail_ready": mail_ready,
    }


def _iso(dt) -> Optional[str]:
    dt = AL.aware(dt)
    return dt.isoformat() if dt else None


def _device_dict(d: KnownDevice) -> dict:
    return {
        "mac": d.mac, "name": d.name, "hostname": d.hostname, "vendor": d.vendor, "ip": d.ip,
        "network": d.network, "essid": d.essid, "is_wired": bool(d.is_wired), "approved": bool(d.approved),
        "first_seen": _iso(d.first_seen), "last_seen": _iso(d.last_seen),
    }


@router.get("/system/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    return templates.TemplateResponse(request, "system/alerts.html", {})


@router.get("/api/alerts/settings")
async def get_settings():
    mail_ready = await mailer.load_config() is not None
    async for session in get_database().get_session():
        row = await AL.get_settings_row(session)
        await session.commit()
        return _settings_dict(row, mail_ready)


@router.put("/api/alerts/settings")
async def put_settings(data: AlertSettingsIn):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", data.report_time or ""):
        raise HTTPException(422, "Enter the time as HH:MM, for example 08:00.")
    for a in mailer.split_recipients(data.recipients):
        if not mailer.valid_address(a):
            raise HTTPException(422, f"Not a valid e-mail address: {a}")
    mail_ready = await mailer.load_config() is not None
    async for session in get_database().get_session():
        row = await AL.get_settings_row(session)
        if data.report_enabled and not row.report_enabled:
            # Don't send straight away when today is the day and the time has passed.
            now = datetime.now()
            if now.weekday() == data.report_weekday and now.strftime("%H:%M") >= data.report_time:
                row.last_report_date = now.strftime("%Y-%m-%d")
        row.recipients = ", ".join(mailer.split_recipients(data.recipients)) or None
        row.unknown_device_enabled = data.unknown_device_enabled
        row.threat_enabled = data.threat_enabled
        row.threat_min_severity = data.threat_min_severity
        row.console_enabled = data.console_enabled
        row.report_enabled = data.report_enabled
        row.report_weekday = data.report_weekday
        row.report_time = data.report_time
        await session.commit()
        return _settings_dict(row, mail_ready)


@router.get("/api/alerts/devices")
async def new_devices(days: int = 30):
    since = AL.utcnow() - timedelta(days=max(1, min(days, 365)))
    async for session in get_database().get_session():
        total = len((await session.execute(select(KnownDevice.mac))).all())
        rows = (await session.execute(
            select(KnownDevice).where(KnownDevice.baseline.is_(False), KnownDevice.first_seen >= since)
            .order_by(KnownDevice.first_seen.desc()).limit(200))).scalars().all()
        return {"known": total, "new": [_device_dict(d) for d in rows]}


@router.post("/api/alerts/devices/{mac}/approve")
async def approve(mac: str, data: ApproveIn):
    async for session in get_database().get_session():
        d = await session.get(KnownDevice, mac.lower())
        if d is None:
            raise HTTPException(404, "Unknown device.")
        d.approved = data.approved
        await session.commit()
        return _device_dict(d)


@router.get("/api/alerts/log")
async def alert_log(limit: int = 25):
    async for session in get_database().get_session():
        rows = (await session.execute(
            select(AlertLog).order_by(AlertLog.id.desc()).limit(max(1, min(limit, 200))))).scalars().all()
        return [{"id": r.id, "created_at": _iso(r.created_at), "kind": r.kind, "subject": r.subject,
                 "sent": bool(r.sent), "error": r.error} for r in rows]


async def _live_clients():
    clients, _, _ = await AL._console_clients()
    return clients


@router.get("/api/alerts/report-preview", response_class=HTMLResponse)
async def report_preview():
    clients = await _live_clients()
    async for session in get_database().get_session():
        subject, _, body = await AL.build_report(session, clients)
        return HTMLResponse(mailer.render_html("Weekly report", body))


@router.post("/api/alerts/test-report")
async def test_report():
    if await mailer.load_config() is None:
        raise HTTPException(409, "Mail is not set up or switched off. Set up the mail server first.")
    clients = await _live_clients()
    async for session in get_database().get_session():
        row = await AL.get_settings_row(session)
        entry = await AL.send_report(session, row, clients, kind="test")
        if not entry.sent:
            raise HTTPException(502, entry.error or "The report could not be sent.")
        return {"sent": True, "subject": entry.subject}

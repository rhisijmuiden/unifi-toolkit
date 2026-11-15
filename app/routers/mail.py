"""
Mail server (SMTP) settings page and API.

The password is stored encrypted (shared.crypto) and is never sent back to
the browser: GET returns `has_password` instead, and a PUT with an empty
password keeps the stored one.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from shared import mailer
from shared.crypto import encrypt_password
from shared.database import get_database
from shared.models.mail_settings import MailSettings
from shared.templating import make_templates

router = APIRouter(tags=["mail"])
templates = make_templates(str(Path(__file__).resolve().parent.parent / "templates"))


class MailSettingsIn(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(587, ge=1, le=65535)
    security: str = "starttls"
    verify_tls: bool = True
    username: Optional[str] = None
    # None or "" keeps the stored password; clear_password removes it.
    password: Optional[str] = None
    clear_password: bool = False
    from_address: str = ""
    from_name: str = "UniFi Toolkit"
    reply_to: Optional[str] = None
    default_recipients: Optional[str] = None
    timeout: int = Field(20, ge=3, le=120)


class TestMailIn(BaseModel):
    to: Optional[str] = None


def _public(row: Optional[MailSettings]) -> dict:
    if row is None:
        row = MailSettings(id=1, enabled=False, host="", port=587, security="starttls", verify_tls=True,
                           from_address="", from_name="UniFi Toolkit", timeout=20)
    return {
        "enabled": bool(row.enabled),
        "host": row.host or "",
        "port": row.port or 587,
        "security": row.security or "starttls",
        "verify_tls": True if row.verify_tls is None else bool(row.verify_tls),
        "username": row.username or "",
        "has_password": bool(row.password_encrypted),
        "from_address": row.from_address or "",
        "from_name": row.from_name or "UniFi Toolkit",
        "reply_to": row.reply_to or "",
        "default_recipients": row.default_recipients or "",
        "timeout": row.timeout or 20,
    }


def validate(data: MailSettingsIn) -> None:
    """Raise HTTPException(422) with a readable reason for bad settings."""
    if data.security not in mailer.SECURITY_MODES:
        raise HTTPException(422, "Security must be starttls, ssl or none.")
    if data.enabled:
        if not data.host.strip():
            raise HTTPException(422, "Enter the SMTP server.")
        if not data.from_address.strip():
            raise HTTPException(422, "Enter a From address.")
    if data.from_address.strip() and not mailer.valid_address(data.from_address.strip()):
        raise HTTPException(422, f"Not a valid e-mail address: {data.from_address}")
    if data.reply_to and data.reply_to.strip() and not mailer.valid_address(data.reply_to.strip()):
        raise HTTPException(422, f"Not a valid e-mail address: {data.reply_to}")
    for a in mailer.split_recipients(data.default_recipients):
        if not mailer.valid_address(a):
            raise HTTPException(422, f"Not a valid e-mail address: {a}")
    if data.security == "none" and (data.username or "").strip():
        raise HTTPException(422, "A login needs an encrypted connection. Choose STARTTLS or SSL/TLS.")


async def _row(session) -> Optional[MailSettings]:
    return (await session.execute(select(MailSettings).where(MailSettings.id == 1))).scalar_one_or_none()


@router.get("/system/mail", response_class=HTMLResponse)
async def mail_page(request: Request):
    return templates.TemplateResponse(request, "system/mail.html", {})


@router.get("/api/mail/settings")
async def get_settings():
    async for session in get_database().get_session():
        return _public(await _row(session))


@router.put("/api/mail/settings")
async def put_settings(data: MailSettingsIn):
    validate(data)
    async for session in get_database().get_session():
        row = await _row(session)
        if row is None:
            row = MailSettings(id=1)
            session.add(row)
        row.enabled = data.enabled
        row.host = data.host.strip()
        row.port = data.port
        row.security = data.security
        row.verify_tls = data.verify_tls
        row.username = (data.username or "").strip() or None
        if data.clear_password:
            row.password_encrypted = None
        elif data.password:
            row.password_encrypted = encrypt_password(data.password)
        row.from_address = data.from_address.strip()
        row.from_name = data.from_name.strip() or "UniFi Toolkit"
        row.reply_to = (data.reply_to or "").strip() or None
        row.default_recipients = ", ".join(mailer.split_recipients(data.default_recipients)) or None
        row.timeout = data.timeout
        await session.commit()
        return _public(row)


@router.post("/api/mail/test")
async def send_test(data: TestMailIn):
    cfg = await mailer.load_config()
    if cfg is None:
        raise HTTPException(409, "Mail is not set up or switched off. Save the settings first.")
    to = mailer.split_recipients(data.to)
    if not to:
        async for session in get_database().get_session():
            row = await _row(session)
            to = mailer.split_recipients(row.default_recipients if row else None)
    if not to:
        raise HTTPException(422, "Enter a recipient or set default recipients.")
    mode = {"starttls": "STARTTLS", "ssl": "SSL/TLS", "none": "no encryption"}[cfg.security]
    text = (f"This is a test message from UniFi Toolkit.\n\n"
            f"Server: {cfg.host}:{cfg.port} ({mode})\n"
            "If you can read this, outgoing mail works.")
    body = (f"<p>This is a test message from UniFi Toolkit.</p>"
            f"<p><strong>Server:</strong> {mailer.html.escape(cfg.host)}:{cfg.port} ({mode})</p>"
            "<p>If you can read this, outgoing mail works.</p>")
    try:
        await mailer.send_mail(to, "UniFi Toolkit test message", text,
                               mailer.render_html("Test message", body), cfg=cfg)
    except mailer.MailError as e:
        raise HTTPException(502, str(e))
    return {"sent": True, "to": to}

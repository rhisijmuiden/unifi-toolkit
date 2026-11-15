"""
Send mail through the configured SMTP server.

Uses the standard library (smtplib + email) in a worker thread, with:
- STARTTLS or implicit TLS (SMTPS), TLS 1.2 minimum and certificate checks
  on by default; plain SMTP only for an explicit "none" (LAN relays),
- AUTH when a username is set (smtplib picks PLAIN/LOGIN/CRAM-MD5 from EHLO),
- multipart/alternative messages (plain text + HTML in the RHIS mail layout),
  RFC 5322 Date and Message-ID, UTF-8 headers and bodies.
"""
import asyncio
import html
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Iterable, List, Optional

from sqlalchemy import select

from shared.crypto import decrypt_password
from shared.database import get_database
from shared.models.mail_settings import MailSettings

SECURITY_MODES = ("starttls", "ssl", "none")


class MailError(Exception):
    """A user-facing reason why mail could not be sent."""


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str = "starttls"
    verify_tls: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    from_address: str = ""
    from_name: str = "UniFi Toolkit"
    reply_to: Optional[str] = None
    timeout: int = 20


def valid_address(addr: str) -> bool:
    name, email = parseaddr(addr or "")
    return bool(email) and "@" in email and "." in email.split("@")[-1] and " " not in email


def split_recipients(value: Optional[str]) -> List[str]:
    return [a.strip() for a in (value or "").replace(";", ",").split(",") if a.strip()]


async def load_config() -> Optional[SmtpConfig]:
    """The saved SMTP settings, or None when mail is off or not set up."""
    db = get_database()
    async for session in db.get_session():
        row = (await session.execute(select(MailSettings).where(MailSettings.id == 1))).scalar_one_or_none()
        if not row or not row.enabled or not row.host or not row.from_address:
            return None
        return SmtpConfig(
            host=row.host, port=row.port, security=row.security, verify_tls=row.verify_tls,
            username=row.username or None,
            password=decrypt_password(row.password_encrypted) if row.password_encrypted else None,
            from_address=row.from_address, from_name=row.from_name or "UniFi Toolkit",
            reply_to=row.reply_to or None, timeout=row.timeout or 20,
        )
    return None


def render_html(title: str, body_html: str, footer: str = "Sent by UniFi Toolkit") -> str:
    """Wrap content in the RHIS mail layout (inline styles for mail clients)."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#F6F7FB;font-family:Archivo,Segoe UI,Helvetica,Arial,sans-serif;color:#14213D">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F6F7FB;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#FFFFFF;border-radius:16px;overflow:hidden;border:1px solid #DCE1EB">
<tr><td style="background:#14213D;padding:20px 28px">
<span style="font-size:22px;font-weight:800;letter-spacing:.02em;color:#FFFFFF">RH<span style="color:#FF6B57">I</span>S</span>
<span style="font-size:13px;color:#9CC3FF;padding-left:10px">UniFi Toolkit</span>
</td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;color:#14213D">{html.escape(title)}</h1>
<div style="font-size:15px;line-height:1.6;color:#14213D">{body_html}</div>
</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #DCE1EB;font-size:12px;color:#4F5B73">{html.escape(footer)}</td></tr>
</table></td></tr></table></body></html>"""


def build_message(cfg: SmtpConfig, to: Iterable[str], subject: str, text: str,
                  html_body: Optional[str] = None) -> EmailMessage:
    recipients = [a for a in to if a]
    if not recipients:
        raise MailError("No recipients.")
    for a in recipients:
        if not valid_address(a):
            raise MailError(f"Not a valid e-mail address: {a}")
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name, cfg.from_address))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.from_address.split("@")[-1] or None)
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def _tls_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _send_sync(cfg: SmtpConfig, msg: EmailMessage) -> None:
    if cfg.security not in SECURITY_MODES:
        raise MailError(f"Unknown security mode: {cfg.security}")
    ctx = _tls_context(cfg.verify_tls)
    try:
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=ctx)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        with server:
            server.ehlo()
            if cfg.security == "starttls":
                if not server.has_extn("starttls"):
                    raise MailError("The server does not offer STARTTLS. Choose SSL/TLS or check the port.")
                server.starttls(context=ctx)
                server.ehlo()
            if cfg.username:
                if cfg.security == "none":
                    raise MailError("Refusing to send a password over an unencrypted connection. Use STARTTLS or SSL/TLS.")
                server.login(cfg.username, cfg.password or "")
            server.send_message(msg)
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailError("The server rejected the username or password.")
    except smtplib.SMTPRecipientsRefused as e:
        raise MailError(f"The server refused the recipient(s): {', '.join(e.recipients)}")
    except smtplib.SMTPSenderRefused:
        raise MailError("The server refused the sender address. Check the From address.")
    except ssl.SSLCertVerificationError as e:
        raise MailError(f"The server's TLS certificate is not trusted ({e.verify_message}). "
                        "Fix the certificate or turn off certificate checks.")
    except ssl.SSLError as e:
        raise MailError(f"TLS failed: {e.reason or e}. Check the security mode and port.")
    except smtplib.SMTPServerDisconnected as e:
        raise MailError(f"The server closed the connection ({e}). Check the security mode, port and login.")
    # SMTPException subclasses OSError, so it must be caught before the network errors.
    except smtplib.SMTPException as e:
        raise MailError(f"SMTP error: {e}")
    except (TimeoutError, OSError) as e:
        raise MailError(f"Could not connect to {cfg.host}:{cfg.port} ({e}).")


async def send_mail(to: Iterable[str], subject: str, text: str, html_body: Optional[str] = None,
                    cfg: Optional[SmtpConfig] = None) -> None:
    """Send one message. Raises MailError with a readable reason."""
    cfg = cfg or await load_config()
    if cfg is None:
        raise MailError("Mail is not set up or switched off.")
    msg = build_message(cfg, list(to), subject, text, html_body)
    await asyncio.to_thread(_send_sync, cfg, msg)

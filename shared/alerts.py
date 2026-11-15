"""
Mail alerts and the weekly report.

A background loop runs every minute and:
- records every client MAC it sees (known_devices) and mails new ones,
- mails new Threat Watch events at or above the chosen severity,
- mails when the UniFi console has been unreachable for 5 minutes, and when it is back,
- sends the weekly report at the chosen day and time.

The first device scan only records what is already on the network, so
turning this on never floods the mailbox with every existing device. The
same goes for threats: the first check starts after the newest stored event.
"""
import asyncio
import html
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from shared import mailer
from shared.database import get_database
from shared.models.alerts import AlertLog, AlertSettings, KnownDevice

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
CONSOLE_GRACE = timedelta(minutes=5)
LAST_SEEN_EVERY = timedelta(minutes=10)
SEVERITY = {1: "High", 2: "Medium", 3: "Low"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_LISTED = 25

_task: Optional[asyncio.Task] = None
_last_seen_write: Optional[datetime] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def get_settings_row(session) -> AlertSettings:
    row = (await session.execute(select(AlertSettings).where(AlertSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = AlertSettings(id=1, unknown_device_enabled=False, threat_enabled=False, threat_min_severity=1,
                            console_enabled=False, report_enabled=False, report_weekday=0, report_time="08:00",
                            devices_seeded=False, console_down_alerted=False)
        session.add(row)
        await session.flush()
    return row


async def recipients(session, row: AlertSettings) -> List[str]:
    own = mailer.split_recipients(row.recipients)
    if own:
        return own
    from shared.models.mail_settings import MailSettings
    ms = (await session.execute(select(MailSettings).where(MailSettings.id == 1))).scalar_one_or_none()
    return mailer.split_recipients(ms.default_recipients if ms else None)


async def send_alert(session, row: AlertSettings, kind: str, subject: str, text: str, body_html: str,
                     title: Optional[str] = None) -> AlertLog:
    """Send one alert mail and log it. Never raises: a failed send is logged with its reason."""
    entry = AlertLog(kind=kind, subject=subject, detail=text[:4000], sent=False)
    session.add(entry)
    try:
        to = await recipients(session, row)
        if not to:
            raise mailer.MailError("No recipients: set them here or on the mail server page.")
        await mailer.send_mail(to, f"[UniFi Toolkit] {subject}", text,
                               mailer.render_html(title or subject, body_html))
        entry.sent = True
    except mailer.MailError as e:
        entry.error = str(e)
    except Exception as e:  # a broken mail must never stop the loop
        logger.exception("Alert mail failed")
        entry.error = f"Unexpected error: {e}"
    await session.commit()
    return entry


def _table(headers: List[str], rows: List[List[str]]) -> str:
    th = "".join(f'<th style="text-align:left;padding:6px 8px;border-bottom:1px solid #DCE1EB;font-size:12px;'
                 f'color:#4F5B73;text-transform:uppercase">{html.escape(h)}</th>' for h in headers)
    trs = "".join(
        "<tr>" + "".join(f'<td style="padding:6px 8px;border-bottom:1px solid #EEF1F7;font-size:14px">'
                         f'{html.escape(str(c if c is not None else ""))}</td>' for c in r) + "</tr>"
        for r in rows)
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin:8px 0 16px">' \
           f"<tr>{th}</tr>{trs}</table>"


def _device_label(d) -> str:
    return d.name or d.hostname or d.vendor or d.mac


def _device_row(d) -> List[str]:
    where = d.essid if not d.is_wired and d.essid else ("Wired" if d.is_wired else "")
    return [_device_label(d), d.mac, d.ip or "", d.network or "", where]


# ---------------------------------------------------------------- devices

async def check_devices(session, row: AlertSettings, clients: Dict[str, dict]) -> List[KnownDevice]:
    """Record the clients; return the devices that are new (and alert on them when enabled)."""
    global _last_seen_write
    now = utcnow()
    known = {d.mac: d for d in (await session.execute(select(KnownDevice))).scalars().all()}
    first_scan = not row.devices_seeded
    new: List[KnownDevice] = []
    refresh = _last_seen_write is None or now - _last_seen_write >= LAST_SEEN_EVERY
    for mac, c in clients.items():
        mac = (mac or "").lower()
        if not mac:
            continue
        d = known.get(mac)
        if d is None:
            d = KnownDevice(mac=mac, first_seen=now, last_seen=now, baseline=first_scan)
            session.add(d)
            known[mac] = d
            if not first_scan:
                new.append(d)
        elif not refresh:
            continue
        d.last_seen = now
        d.name = c.get("name") or d.name
        d.hostname = c.get("hostname") or d.hostname
        d.vendor = c.get("oui") or d.vendor
        d.ip = c.get("ip") or d.ip
        d.network = c.get("network") or d.network
        d.essid = c.get("essid") or d.essid
        d.is_wired = bool(c.get("is_wired"))
    if refresh:
        _last_seen_write = now
    if first_scan:
        row.devices_seeded = True
    await session.commit()
    if new and row.unknown_device_enabled:
        n = len(new)
        subject = f"New device on your network: {_device_label(new[0])}" if n == 1 else f"{n} new devices on your network"
        lines = [f"- {_device_label(d)} ({d.mac}) {d.ip or ''} {d.network or ''}".rstrip() for d in new[:MAX_LISTED]]
        text = "The toolkit saw a device it has never seen before:\n\n" + "\n".join(lines) + \
               "\n\nIf you know it, mark it as known on the Alerts page."
        body = ("<p>The toolkit saw a device it has never seen before.</p>"
                + _table(["Device", "MAC", "IP", "Network", "Connection"], [_device_row(d) for d in new[:MAX_LISTED]])
                + "<p>If you know it, mark it as known on the Alerts page.</p>")
        await send_alert(session, row, "unknown_device", subject, text, body, title="New device")
    return new


# ---------------------------------------------------------------- threats

async def check_threats(session, row: AlertSettings) -> int:
    from tools.threat_watch.database import ThreatEvent
    newest = (await session.execute(select(func.max(ThreatEvent.id)))).scalar()
    if row.last_threat_id is None or not row.threat_enabled:
        row.last_threat_id = newest or 0
        await session.commit()
        return 0
    events = (await session.execute(
        select(ThreatEvent).where(ThreatEvent.id > row.last_threat_id,
                                  ThreatEvent.severity.isnot(None),
                                  ThreatEvent.severity <= row.threat_min_severity,
                                  ThreatEvent.ignored.is_(False))
        .order_by(ThreatEvent.id))).scalars().all()
    row.last_threat_id = max(newest or 0, row.last_threat_id)
    await session.commit()
    if not events:
        return 0
    n = len(events)
    top = events[0]
    subject = (f"Threat: {top.signature or top.category or 'IPS event'}" if n == 1
               else f"{n} new threat events")
    rows = [[aware(e.timestamp).astimezone().strftime("%d-%m %H:%M") if e.timestamp else "",
             SEVERITY.get(e.severity, e.severity), e.action or "", e.signature or e.category or "",
             f"{e.src_ip or ''}{' (' + e.src_country + ')' if e.src_country else ''}", e.dest_ip or ""]
            for e in events[:MAX_LISTED]]
    text = "New Threat Watch events:\n\n" + "\n".join(
        f"- {r[0]} {r[1]} {r[2]}: {r[3]} from {r[4]} to {r[5]}" for r in rows)
    if n > MAX_LISTED:
        text += f"\n...and {n - MAX_LISTED} more."
    body = ("<p>Threat Watch recorded new events.</p>"
            + _table(["Time", "Severity", "Action", "Signature", "Source", "Destination"], rows)
            + (f"<p>...and {n - MAX_LISTED} more. Open Threat Watch for all of them.</p>" if n > MAX_LISTED else ""))
    await send_alert(session, row, "threat", subject, text, body, title="Threat Watch")
    return n


# ---------------------------------------------------------------- console

async def check_console(session, row: AlertSettings, problem: Optional[str]) -> Optional[str]:
    """Track console reachability. Returns "down", "up" or None (nothing sent)."""
    now = utcnow()
    if problem:
        if row.console_down_since is None:
            row.console_down_since = now
            await session.commit()
            return None
        if (row.console_enabled and not row.console_down_alerted
                and now - aware(row.console_down_since) >= CONSOLE_GRACE):
            row.console_down_alerted = True
            await session.commit()
            since = aware(row.console_down_since).astimezone().strftime("%H:%M")
            text = f"The toolkit can't reach your UniFi console since {since}.\n\nReason: {problem}"
            body = (f"<p>The toolkit can't reach your UniFi console since {since}.</p>"
                    f"<p><strong>Reason:</strong> {html.escape(problem)}</p>")
            await send_alert(session, row, "console_down", "Can't reach your UniFi console", text, body)
            return "down"
        return None
    if row.console_down_since is not None:
        was_alerted = row.console_down_alerted
        minutes = int((now - aware(row.console_down_since)).total_seconds() // 60)
        row.console_down_since = None
        row.console_down_alerted = False
        await session.commit()
        if was_alerted and row.console_enabled:
            text = f"The toolkit reaches your UniFi console again, after about {minutes} minutes."
            await send_alert(session, row, "console_up", "UniFi console is reachable again", text,
                             f"<p>{html.escape(text)}</p>")
            return "up"
    return None


# ---------------------------------------------------------------- weekly report

async def build_report(session, clients: Optional[Dict[str, dict]], days: int = 7) -> Tuple[str, str, str]:
    """Subject, plain text and HTML body of the report for the last `days` days."""
    from tools.threat_watch.database import ThreatEvent
    from shared.models.backup import BackupRun
    now = utcnow()
    since = now - timedelta(days=days)
    parts_t, parts_h = [], []

    # Clients
    if clients is not None:
        wired = sum(1 for c in clients.values() if c.get("is_wired"))
        line = f"{len(clients)} clients online now ({wired} wired, {len(clients) - wired} Wi-Fi)."
    else:
        line = "The console could not be reached, so there are no live client numbers."
    parts_t.append(line)
    parts_h.append(f"<h2 style=\"font-size:17px;margin:16px 0 4px\">Clients</h2><p>{html.escape(line)}</p>")

    # New devices
    new = (await session.execute(
        select(KnownDevice).where(KnownDevice.first_seen >= since, KnownDevice.baseline.is_(False))
        .order_by(KnownDevice.first_seen.desc()))).scalars().all()
    head = f"{len(new)} new device(s) this week." if new else "No new devices this week."
    parts_t.append(head + "".join(f"\n- {_device_label(d)} ({d.mac})" for d in new[:MAX_LISTED]))
    parts_h.append(f"<h2 style=\"font-size:17px;margin:16px 0 4px\">New devices</h2><p>{html.escape(head)}</p>"
                   + (_table(["Device", "MAC", "IP", "Network", "Connection"], [_device_row(d) for d in new[:MAX_LISTED]])
                      if new else ""))

    # Threats
    base = [ThreatEvent.timestamp >= since.replace(tzinfo=None), ThreatEvent.ignored.is_(False)]
    total = (await session.execute(select(func.count(ThreatEvent.id)).where(*base))).scalar() or 0
    blocked = (await session.execute(select(func.count(ThreatEvent.id)).where(*base, ThreatEvent.action == "block"))).scalar() or 0
    sev = dict((await session.execute(select(ThreatEvent.severity, func.count(ThreatEvent.id)).where(*base)
                                      .group_by(ThreatEvent.severity))).all())
    head = (f"{total} threat event(s), {blocked} blocked. High {sev.get(1, 0)}, medium {sev.get(2, 0)}, low {sev.get(3, 0)}."
            if total else "No threat events this week.")
    parts_t.append(head)
    h = f"<h2 style=\"font-size:17px;margin:16px 0 4px\">Threats</h2><p>{html.escape(head)}</p>"
    if total:
        sigs = (await session.execute(
            select(ThreatEvent.signature, func.count(ThreatEvent.id).label("n")).where(*base, ThreatEvent.signature.isnot(None))
            .group_by(ThreatEvent.signature).order_by(func.count(ThreatEvent.id).desc()).limit(5))).all()
        srcs = (await session.execute(
            select(ThreatEvent.src_ip, ThreatEvent.src_country, func.count(ThreatEvent.id)).where(*base, ThreatEvent.src_ip.isnot(None))
            .group_by(ThreatEvent.src_ip, ThreatEvent.src_country).order_by(func.count(ThreatEvent.id).desc()).limit(5))).all()
        if sigs:
            h += _table(["Most seen signature", "Events"], [[s, n] for s, n in sigs])
            parts_t.append("Most seen: " + "; ".join(f"{s} ({n})" for s, n in sigs))
        if srcs:
            h += _table(["Source", "Country", "Events"], [[ip, cc or "", n] for ip, cc, n in srcs])
    parts_h.append(h)

    # Backups
    runs = (await session.execute(select(BackupRun).where(BackupRun.action == "backup", BackupRun.started_at >= since)
                                  .order_by(BackupRun.id.desc()))).scalars().all()
    if runs:
        c = Counter(r.status for r in runs)
        head = f"{len(runs)} backup(s): {c.get('ok', 0)} OK, {c.get('partial', 0)} partly done, {c.get('failed', 0)} failed."
    else:
        head = "No backups this week. Turn on daily backups under System > Backups."
    parts_t.append(head)
    parts_h.append(f"<h2 style=\"font-size:17px;margin:16px 0 4px\">Backups</h2><p>{html.escape(head)}</p>")

    period = f"{since.astimezone().strftime('%d-%m-%Y')} to {now.astimezone().strftime('%d-%m-%Y')}"
    subject = f"Weekly report {period}"
    text = f"UniFi Toolkit weekly report, {period}\n\n" + "\n\n".join(parts_t)
    body = f"<p style=\"color:#4F5B73\">{html.escape(period)}</p>" + "".join(parts_h)
    return subject, text, body


def report_due(row: AlertSettings, now: datetime) -> bool:
    if not row.report_enabled or now.weekday() != (row.report_weekday or 0):
        return False
    if row.last_report_date == now.strftime("%Y-%m-%d"):
        return False
    return now.strftime("%H:%M") >= (row.report_time or "08:00")


async def send_report(session, row: AlertSettings, clients: Optional[Dict[str, dict]], kind: str = "report") -> AlertLog:
    subject, text, body = await build_report(session, clients)
    return await send_alert(session, row, kind, subject, text, body, title="Weekly report")


# ---------------------------------------------------------------- loop

async def _console_clients() -> Tuple[Optional[Dict[str, dict]], Optional[str], bool]:
    """(clients, problem, configured). Never raises."""
    from shared import unifi_session
    from shared.models.unifi_config import UniFiConfig
    async for session in get_database().get_session():
        configured = (await session.execute(select(UniFiConfig.id).where(UniFiConfig.id == 1))).scalar() is not None
    if not configured:
        return None, None, False
    try:
        client = await unifi_session.get_shared_client()
        if client is None:
            return None, unifi_session.connection_problem() or "Could not connect to the console.", True
        return await client.get_clients(), None, True
    except Exception as e:
        return None, str(e) or e.__class__.__name__, True


async def tick() -> None:
    clients, problem, configured = await _console_clients()
    async for session in get_database().get_session():
        row = await get_settings_row(session)
        await session.commit()
        if clients is not None:
            await check_devices(session, row, clients)
        await check_threats(session, row)
        if configured:
            await check_console(session, row, problem)
        now = datetime.now()
        if report_due(row, now):
            row.last_report_date = now.strftime("%Y-%m-%d")
            await session.commit()
            await send_report(session, row, clients)


async def _loop() -> None:
    await asyncio.sleep(20)  # let the tool schedulers connect first
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Alert check failed")
        await asyncio.sleep(TICK_SECONDS)


async def start_scheduler() -> None:
    global _task
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

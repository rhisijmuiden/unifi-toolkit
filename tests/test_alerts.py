"""Mail alerts and the weekly report (shared/alerts.py, app/routers/alerts.py)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import alerts as AL
from shared import mailer
from shared.models.alerts import AlertLog, KnownDevice
from shared.models.base import Base
from shared.models import backup as _b, mail_settings as _m  # noqa: F401
from tools.threat_watch.database import ThreatEvent


@pytest.fixture
def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class FakeDB:
        async def get_session(self):
            async with factory() as session:
                yield session

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(create())
    from app.routers import alerts as router_mod
    monkeypatch.setattr(AL, "get_database", lambda: FakeDB())
    monkeypatch.setattr(router_mod, "get_database", lambda: FakeDB())
    monkeypatch.setattr(mailer, "get_database", lambda: FakeDB())
    sent = []

    async def fake_send(to, subject, text, html_body=None, cfg=None):
        sent.append({"to": list(to), "subject": subject, "text": text, "html": html_body})
    monkeypatch.setattr(mailer, "send_mail", fake_send)
    monkeypatch.setattr(AL, "_last_seen_write", None)
    app = FastAPI()
    app.include_router(router_mod.router)
    return {"factory": factory, "sent": sent, "client": TestClient(app), "router": router_mod}


def run(coro):
    return asyncio.run(coro)


async def _with_row(factory, fn):
    async with factory() as s:
        row = await AL.get_settings_row(s)
        row.recipients = "ops@example.com"
        await s.commit()
        return await fn(s, row)


def clients(*macs, **extra):
    return {m: {"mac": m, "hostname": f"host-{m[-2:]}", "ip": "192.168.1.10", "network": "LAN",
                "is_wired": False, "essid": "Home", "oui": "Acme", **extra} for m in macs}


def test_first_scan_is_silent_then_new_devices_alert(env):
    f, sent = env["factory"], env["sent"]

    async def go(s, row):
        row.unknown_device_enabled = True
        first = await AL.check_devices(s, row, clients("aa:00:00:00:00:01", "aa:00:00:00:00:02"))
        assert first == [] and row.devices_seeded
        again = await AL.check_devices(s, row, clients("aa:00:00:00:00:01", "aa:00:00:00:00:02"))
        assert again == []
        new = await AL.check_devices(s, row, clients("aa:00:00:00:00:01", "AA:00:00:00:00:03"))
        assert [d.mac for d in new] == ["aa:00:00:00:00:03"]
        devs = (await s.execute(select(KnownDevice).order_by(KnownDevice.mac))).scalars().all()
        assert [d.baseline for d in devs] == [True, True, False]
    run(_with_row(f, go))
    assert len(sent) == 1
    assert "host-03" in sent[0]["subject"] and sent[0]["to"] == ["ops@example.com"]
    assert "aa:00:00:00:00:03" in sent[0]["html"]


def test_new_devices_not_mailed_when_off_but_recorded(env):
    f, sent = env["factory"], env["sent"]

    async def go(s, row):
        await AL.check_devices(s, row, clients("aa:00:00:00:00:01"))
        new = await AL.check_devices(s, row, clients("aa:00:00:00:00:09"))
        assert len(new) == 1
    run(_with_row(f, go))
    assert sent == []


def test_threats_start_after_newest_and_respect_severity(env):
    f, sent = env["factory"], env["sent"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def ev(i, sev, ignored=False):
        return ThreatEvent(unifi_event_id=f"e{i}", timestamp=now, severity=sev, signature=f"SIG {i}",
                           action="block", src_ip="203.0.113.5", src_country="NL", dest_ip="192.168.1.2",
                           ignored=ignored)

    async def go(s, row):
        s.add(ev(1, 1))
        await s.commit()
        row.threat_enabled = True
        assert await AL.check_threats(s, row) == 0  # first check: start after the newest event
        s.add_all([ev(2, 1), ev(3, 3), ev(4, 1, ignored=True)])
        await s.commit()
        assert await AL.check_threats(s, row) == 1  # only the high one, not low, not ignored
        assert await AL.check_threats(s, row) == 0
    run(_with_row(f, go))
    assert len(sent) == 1 and "SIG 2" in sent[0]["subject"]


def test_console_down_after_grace_and_back(env, monkeypatch):
    f, sent = env["factory"], env["sent"]
    clock = [datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(AL, "utcnow", lambda: clock[0])

    async def go(s, row):
        row.console_enabled = True
        assert await AL.check_console(s, row, "refused API key") is None
        clock[0] += timedelta(minutes=3)
        assert await AL.check_console(s, row, "refused API key") is None
        clock[0] += timedelta(minutes=3)
        assert await AL.check_console(s, row, "refused API key") == "down"
        assert await AL.check_console(s, row, "refused API key") is None  # only once
        clock[0] += timedelta(minutes=10)
        assert await AL.check_console(s, row, None) == "up"
        # A short blip never mails.
        assert await AL.check_console(s, row, "timeout") is None
        assert await AL.check_console(s, row, None) is None
    run(_with_row(f, go))
    assert [m["subject"] for m in sent] == ["[UniFi Toolkit] Can't reach your UniFi console",
                                            "[UniFi Toolkit] UniFi console is reachable again"]
    assert "refused API key" in sent[0]["text"]


def test_report_due():
    from shared.models.alerts import AlertSettings
    row = AlertSettings(report_enabled=True, report_weekday=0, report_time="08:00")
    monday = datetime(2026, 11, 16, 8, 0)
    assert AL.report_due(row, monday)
    assert not AL.report_due(row, monday.replace(hour=7, minute=59))
    assert not AL.report_due(row, monday + timedelta(days=1))
    row.last_report_date = "2026-11-16"
    assert not AL.report_due(row, monday)
    row.report_enabled = False
    assert not AL.report_due(row, monday + timedelta(days=7))


def test_report_contents(env):
    f = env["factory"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def go(s, row):
        await AL.check_devices(s, row, clients("aa:00:00:00:00:01"))
        await AL.check_devices(s, row, clients("aa:00:00:00:00:01", "aa:00:00:00:00:02"))
        s.add_all([ThreatEvent(unifi_event_id=f"r{i}", timestamp=now, severity=1 if i < 2 else 2,
                               signature="ET SCAN", action="block" if i else "alert", src_ip="198.51.100.7")
                   for i in range(3)])
        await s.commit()
        return await AL.build_report(s, clients("aa:00:00:00:00:01", "aa:00:00:00:00:02", is_wired=True))
    subject, text, body = run(_with_row(f, go))
    assert subject.startswith("Weekly report")
    assert "2 clients online now (2 wired, 0 Wi-Fi)" in text
    assert "1 new device(s) this week" in text and "aa:00:00:00:00:02" in text
    assert "3 threat event(s), 2 blocked. High 2, medium 1" in text
    assert "ET SCAN" in body and "No backups this week" in text


def test_send_alert_logs_failures(env, monkeypatch):
    f = env["factory"]

    async def broken(*a, **k):
        raise mailer.MailError("The server rejected the username or password.")
    monkeypatch.setattr(mailer, "send_mail", broken)

    async def go(s, row):
        e = await AL.send_alert(s, row, "test", "Hello", "t", "<p>t</p>")
        assert e.sent is False and "rejected" in e.error
        row.recipients = None
        e = await AL.send_alert(s, row, "test", "Hello", "t", "<p>t</p>")
        assert "No recipients" in e.error
        return (await s.execute(select(AlertLog))).scalars().all()
    assert len(run(_with_row(f, go))) == 2


def test_router_settings_devices_and_log(env):
    c = env["client"]
    r = c.put("/api/alerts/settings", json={"recipients": "a@b.nl; c@d.nl", "unknown_device_enabled": True,
                                            "threat_enabled": True, "threat_min_severity": 2, "console_enabled": True,
                                            "report_enabled": True, "report_weekday": 4, "report_time": "07:30"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["recipients"] == "a@b.nl, c@d.nl" and s["threat_min_severity"] == 2 and s["report_weekday"] == 4
    assert c.put("/api/alerts/settings", json={"report_time": "7:30"}).status_code == 422
    assert c.put("/api/alerts/settings", json={"recipients": "nope"}).status_code == 422

    async def seed(sess, row):
        await AL.check_devices(sess, row, clients("aa:00:00:00:00:01"))
        await AL.check_devices(sess, row, clients("aa:00:00:00:00:01", "aa:00:00:00:00:05"))
    run(_with_row(env["factory"], seed))
    d = c.get("/api/alerts/devices").json()
    assert d["known"] == 2 and [x["mac"] for x in d["new"]] == ["aa:00:00:00:00:05"]
    assert c.post("/api/alerts/devices/AA:00:00:00:00:05/approve", json={"approved": True}).json()["approved"] is True
    assert c.post("/api/alerts/devices/ff:ff:ff:ff:ff:ff/approve", json={}).status_code == 404
    log = c.get("/api/alerts/log").json()
    assert log and log[0]["kind"] == "unknown_device" and log[0]["sent"] is True

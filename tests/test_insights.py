"""Firmware overview, WAN availability and speed test history (shared/insights.py)."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import insights as IN
from shared.models.base import Base
from shared.models.insights import FirmwareChange, SpeedTest, WanSample

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


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
    run(create())
    from app.routers import insights as router_mod
    monkeypatch.setattr(IN, "get_database", lambda: FakeDB())
    monkeypatch.setattr(router_mod, "get_database", lambda: FakeDB())
    app = FastAPI()
    app.include_router(router_mod.router)
    return {"factory": factory, "client": TestClient(app)}


# ---------------------------------------------------------------- parsing

def test_wan_samples_from_health():
    health = {
        "www": {"latency": 12},
        "wan": {"status": "ok", "wan_ip": "1.2.3.4", "isp_name": "KPN", "latency_avg": 30},
        "wan2": {"status": "error", "wan_ip": None, "status_reason": "no link"},
        "wan3": {"status": "unknown"},
        "lan": {"status": "ok"},
    }
    s = IN.wan_samples_from_health(health)
    assert [x["wan"] for x in s] == ["wan", "wan2"]
    assert s[0]["up"] is True and s[0]["latency_ms"] == 12.0 and s[0]["isp"] == "KPN"
    assert s[0]["reason"] is None
    assert s[1]["up"] is False and s[1]["reason"] == "no link"


def test_wan_ok_without_ip_is_down():
    s = IN.wan_samples_from_health({"wan": {"status": "ok", "wan_ip": ""}})
    assert s[0]["up"] is False


def test_speedtest_from_devices():
    devs = [{"type": "uap"}, {"type": "ugw", "speedtest-status": {
        "rundate": 1763300000, "xput_download": 512.3, "xput_upload": 98.1, "latency": 7,
        "server": {"provider": "KPN", "city": "Amsterdam", "cc": "NL"}}}]
    t = IN.speedtest_from_devices(devs)
    assert t["download_mbps"] == 512.3 and t["upload_mbps"] == 98.1 and t["latency_ms"] == 7.0
    assert t["server"] == "KPN, Amsterdam, NL"
    assert t["run_at"] == datetime.fromtimestamp(1763300000, tz=UTC)
    assert IN.speedtest_from_devices([{"speedtest-status": {"rundate": 0}}]) is None


def test_firmware_rows_sorted_upgradable_first():
    devs = [
        {"mac": "AA:00", "version": "6.0", "name": "Office AP", "model": "U7PG2", "type": "uap", "state": 1},
        {"mac": "BB:00", "version": "7.1", "name": "Switch", "model": "US8", "type": "usw", "state": 0,
         "upgradable": True, "upgrade_to_firmware": "7.2"},
        {"mac": "CC:00", "name": "no version"},
    ]
    rows = IN.firmware_rows(devs)
    assert [r["mac"] for r in rows] == ["bb:00", "aa:00"]
    assert rows[0]["upgrade_to"] == "7.2" and rows[0]["online"] is False
    assert rows[1]["upgrade_to"] is None and rows[1]["online"] is True


# ---------------------------------------------------------------- recording

def test_record_firmware_logs_first_seen_and_changes(env):
    async def go():
        async with env["factory"]() as s:
            rows = [{"mac": "aa", "name": "AP", "model": "U6", "version": "1.0"}]
            first = await IN.record_firmware(s, rows)
            assert len(first) == 1 and first[0].old_version is None
            assert await IN.record_firmware(s, rows) == []
            rows[0]["version"] = "1.1"
            changed = await IN.record_firmware(s, rows)
            assert changed[0].old_version == "1.0" and changed[0].new_version == "1.1"
    run(go())


def test_record_speedtest_dedupes(env):
    t = {"run_at": datetime(2026, 11, 1, tzinfo=UTC), "download_mbps": 100.0, "upload_mbps": 10.0,
         "latency_ms": 5.0, "server": None}

    async def go():
        async with env["factory"]() as s:
            assert await IN.record_speedtest(s, dict(t)) is True
            assert await IN.record_speedtest(s, dict(t)) is False
            assert await IN.record_speedtest(s, None) is False
    run(go())


def test_purge_drops_old_samples(env):
    now = datetime(2026, 11, 16, tzinfo=UTC)

    async def go():
        async with env["factory"]() as s:
            await IN.record_wan(s, [{"wan": "wan", "up": True}], now=now - timedelta(days=IN.WAN_RETENTION_DAYS + 1))
            await IN.record_wan(s, [{"wan": "wan", "up": True}], now=now)
            await IN.purge(s, now=now)
            from sqlalchemy import select
            rows = (await s.execute(select(WanSample))).scalars().all()
            assert len(rows) == 1
    run(go())


# ---------------------------------------------------------------- reporting

def _samples(start, pattern, wan="wan"):
    """pattern: string of 'u'/'d', one sample per minute from start ('-' = no sample)."""
    out = []
    for i, c in enumerate(pattern):
        if c == "-":
            continue
        out.append(WanSample(ts=start + timedelta(minutes=i), wan=wan, up=c == "u",
                             latency_ms=10.0 if c == "u" else None, reason=None if c == "u" else "down"))
    return out


def test_summarize_wan_availability_and_outages():
    start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    rows = _samples(start, "uuuddduuuu")
    out = IN.summarize_wan(rows, 2, date(2026, 11, 16), tz=UTC)["wan"]
    assert out["samples"] == 10 and out["down_minutes"] == 3
    assert out["availability"] == 70.0
    assert out["avg_latency_ms"] == 10.0
    assert [d["date"] for d in out["days"]] == ["2026-11-15", "2026-11-16"]
    assert out["days"][0]["availability"] is None and out["days"][1]["availability"] == 70.0
    assert len(out["outages"]) == 1
    o = out["outages"][0]
    assert o["start"] == (start + timedelta(minutes=3)).isoformat()
    assert o["end"] == (start + timedelta(minutes=6)).isoformat()
    assert o["minutes"] == 3
    assert out["up_now"] is True


def test_summarize_wan_gap_ends_outage_and_ongoing_outage():
    start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    # down twice, then 10 minutes without samples, then down again till now
    rows = _samples(start, "udd----------dd")
    out = IN.summarize_wan(rows, 1, date(2026, 11, 16), tz=UTC)["wan"]
    assert len(out["outages"]) == 2
    newest, older = out["outages"]
    assert older["end"] == (start + timedelta(minutes=2)).isoformat()
    assert newest["end"] is None
    assert older["minutes"] == 2 and newest["minutes"] == 2
    assert out["up_now"] is False


def test_summarize_wan_multiple_wans():
    start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    rows = _samples(start, "uuuu") + _samples(start, "uudd", wan="wan2")
    out = IN.summarize_wan(rows, 1, date(2026, 11, 16), tz=UTC)
    assert out["wan"]["availability"] == 100.0 and out["wan2"]["availability"] == 50.0


# ---------------------------------------------------------------- API

def test_api_wan_and_speedtests(env):
    now = IN.utcnow()

    async def seed():
        async with env["factory"]() as s:
            await IN.record_wan(s, [{"wan": "wan", "up": True, "latency_ms": 8.0}], now=now - timedelta(minutes=1))
            await IN.record_wan(s, [{"wan": "wan", "up": False}], now=now)
            for i, dl in enumerate((100.0, 200.0)):
                await IN.record_speedtest(s, {"run_at": now - timedelta(days=i + 1), "download_mbps": dl,
                                              "upload_mbps": 20.0, "latency_ms": None, "server": "X"})
    run(seed())
    r = env["client"].get("/api/insights/wan?days=7").json()
    assert r["days"] == 7 and r["wans"]["wan"]["availability"] == 50.0
    assert len(r["wans"]["wan"]["days"]) == 7
    r = env["client"].get("/api/insights/speedtests").json()
    assert len(r["tests"]) == 2 and r["tests"][0]["download_mbps"] == 200.0
    assert r["avg"] == {"download_mbps": 150.0, "upload_mbps": 20.0, "latency_ms": None}


def test_api_firmware_needs_console(env, monkeypatch):
    from shared import unifi_session

    async def none():
        return None
    monkeypatch.setattr(unifi_session, "get_shared_client", none)
    monkeypatch.setattr(unifi_session, "connection_problem", lambda: None)
    async def seed():
        async with env["factory"]() as s:
            await IN.record_firmware(s, [{"mac": "aa", "name": "AP", "model": "U6", "version": "1.0"}])
            await IN.record_firmware(s, [{"mac": "aa", "name": "AP", "model": "U6", "version": "1.1"}])
    run(seed())
    r = env["client"].get("/api/insights/firmware")
    assert r.status_code == 200
    d = r.json()
    assert d["devices"] is None and d["error"] == "No UniFi console is connected."
    assert len(d["changes"]) == 1  # history still shown without a console


def test_api_firmware_records_changes(env, monkeypatch):
    from shared import unifi_session
    devices = [{"mac": "AA", "version": "1.0", "name": "AP", "model": "U6", "type": "uap", "state": 1}]

    class Client:
        async def get_devices_raw(self):
            return devices

    async def client():
        return Client()
    monkeypatch.setattr(unifi_session, "get_shared_client", client)
    r = env["client"].get("/api/insights/firmware").json()
    assert r["upgradable"] == 0 and r["changes"] == []  # first sighting is not a change
    devices[0]["version"] = "1.1"
    devices[0]["upgradable"] = True
    r = env["client"].get("/api/insights/firmware").json()
    assert r["upgradable"] == 1
    assert r["changes"][0]["old_version"] == "1.0" and r["changes"][0]["new_version"] == "1.1"

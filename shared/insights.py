"""
Firmware overview, WAN availability and speed test history.

UniFi itself only keeps a short window of WAN uptime and the last speed
test, so a background loop records them:
- every minute: one sample per WAN (up or down, latency),
- every 10 minutes: the gateway's last speed test, and each device's
  firmware version (a change is logged).

Availability is up samples / all samples of a period. Minutes in which the
console could not be read are not counted either way: the toolkit can't
tell whether the internet was down or only the console unreachable.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import delete, func, select

from shared.database import get_database
from shared.models.insights import FirmwareChange, SpeedTest, WanSample

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
SLOW_EVERY = 10  # ticks between speed test and firmware reads
WAN_RETENTION_DAYS = 400
SAMPLE_GAP = timedelta(minutes=3)  # a longer silence between samples ends an outage window

_task: Optional[asyncio.Task] = None
_ticks = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    dt = aware(dt)
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- parsing

def wan_samples_from_health(health: Dict[str, dict]) -> List[dict]:
    """One sample per WAN subsystem in a get_health() result."""
    out = []
    www_latency = (health.get("www") or {}).get("latency")
    for name in sorted(k for k in health if k.startswith("wan")):
        h = health[name] or {}
        status = h.get("status")
        if status in (None, "unknown"):
            continue  # no gateway, or a WAN that isn't set up
        latency = h.get("latency_avg")
        if name == "wan" and www_latency is not None:
            latency = www_latency
        out.append({
            "wan": name,
            "up": status == "ok" and bool(h.get("wan_ip")),
            "latency_ms": float(latency) if latency is not None else None,
            "wan_ip": h.get("wan_ip"),
            "isp": h.get("isp_name"),
            "reason": h.get("status_reason") if status != "ok" else None,
        })
    return out


def speedtest_from_devices(devices: Iterable[dict]) -> Optional[dict]:
    """The gateway's last speed test from stat/device, or None."""
    for d in devices:
        st = d.get("speedtest-status") or {}
        run = st.get("rundate")
        if not run or st.get("xput_download") is None:
            continue
        server = st.get("server") or {}
        where = ", ".join(x for x in (server.get("provider"), server.get("city"), server.get("cc")) if x)
        return {
            "run_at": datetime.fromtimestamp(int(run), tz=timezone.utc),
            "download_mbps": float(st["xput_download"]),
            "upload_mbps": float(st["xput_upload"]) if st.get("xput_upload") is not None else None,
            "latency_ms": float(st["latency"]) if st.get("latency") is not None else None,
            "server": where or None,
        }
    return None


def firmware_rows(devices: Iterable[dict]) -> List[dict]:
    from shared.unifi_client import get_friendly_model_name
    rows = []
    for d in devices:
        if not d.get("mac") or not d.get("version"):
            continue
        rows.append({
            "mac": d["mac"].lower(),
            "name": d.get("name") or get_friendly_model_name(d.get("model") or "") or d["mac"],
            "model": get_friendly_model_name(d.get("model") or ""),
            "model_code": d.get("model"),
            "type": d.get("type"),
            "version": d.get("version"),
            "upgradable": bool(d.get("upgradable")),
            "upgrade_to": d.get("upgrade_to_firmware") if d.get("upgradable") else None,
            "online": d.get("state") == 1,
            "ip": d.get("ip"),
            "uptime": d.get("uptime"),
        })
    rows.sort(key=lambda r: (not r["upgradable"], (r["type"] or ""), r["name"].lower()))
    return rows


# ---------------------------------------------------------------- recording

async def record_wan(session, samples: List[dict], now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    for s in samples:
        session.add(WanSample(ts=now, **s))
    await session.commit()


async def record_speedtest(session, test: Optional[dict]) -> bool:
    if not test:
        return False
    exists = (await session.execute(
        select(SpeedTest.id).where(SpeedTest.run_at == test["run_at"]))).scalar()
    if exists:
        return False
    session.add(SpeedTest(**test))
    await session.commit()
    return True


async def last_versions(session) -> Dict[str, str]:
    sub = select(FirmwareChange.mac, func.max(FirmwareChange.id).label("mid")).group_by(FirmwareChange.mac).subquery()
    rows = (await session.execute(
        select(FirmwareChange.mac, FirmwareChange.new_version).join(sub, FirmwareChange.id == sub.c.mid))).all()
    return {mac: v for mac, v in rows}


async def record_firmware(session, rows: List[dict], now: Optional[datetime] = None) -> List[FirmwareChange]:
    """Log devices seen for the first time and devices whose version changed."""
    now = now or utcnow()
    known = await last_versions(session)
    changes = []
    for r in rows:
        old = known.get(r["mac"])
        if old == r["version"]:
            continue
        c = FirmwareChange(ts=now, mac=r["mac"], name=r["name"], model=r["model"], old_version=old,
                           new_version=r["version"])
        session.add(c)
        changes.append(c)
    if changes:
        await session.commit()
    return changes


async def purge(session, now: Optional[datetime] = None) -> None:
    cutoff = (now or utcnow()) - timedelta(days=WAN_RETENTION_DAYS)
    await session.execute(delete(WanSample).where(WanSample.ts < cutoff))
    await session.commit()


# ---------------------------------------------------------------- reporting

def summarize_wan(samples: List[WanSample], days: int, today: date, tz=None) -> dict:
    """
    Availability per WAN: overall percentage, one bar per day (local dates,
    oldest first), and the outages (consecutive down samples).
    """
    tz = tz or datetime.now().astimezone().tzinfo
    by_wan: Dict[str, List[WanSample]] = {}
    for s in samples:
        by_wan.setdefault(s.wan, []).append(s)
    out = {}
    for wan, rows in sorted(by_wan.items()):
        rows.sort(key=lambda s: aware(s.ts))
        per_day: Dict[date, List[int]] = {}
        lat = []
        for s in rows:
            d = aware(s.ts).astimezone(tz).date()
            c = per_day.setdefault(d, [0, 0])
            c[0] += 1
            c[1] += 1 if s.up else 0
            if s.up and s.latency_ms is not None:
                lat.append(s.latency_ms)
        bars = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            total, up = per_day.get(d, [0, 0])
            bars.append({"date": d.isoformat(), "samples": total,
                         "availability": round(100.0 * up / total, 3) if total else None})
        outages, cur = [], None
        prev_ts = None
        for s in rows:
            ts = aware(s.ts)
            gap = prev_ts is not None and ts - prev_ts > SAMPLE_GAP
            if cur and (s.up or gap):
                cur["end"] = ts if s.up and not gap else prev_ts
                outages.append(cur)
                cur = None
            if not s.up:
                if cur is None:
                    cur = {"start": ts, "end": None, "reason": s.reason, "minutes": 0}
                cur["minutes"] += 1  # one sample per minute
            prev_ts = ts
        if cur:
            outages.append(cur)  # still down: end stays None
        total = sum(1 for _ in rows)
        up = sum(1 for s in rows if s.up)
        last = rows[-1]
        out[wan] = {
            "availability": round(100.0 * up / total, 3) if total else None,
            "samples": total,
            "down_minutes": total - up,
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
            "isp": last.isp, "wan_ip": last.wan_ip, "up_now": last.up, "last_sample": iso(last.ts),
            "days": bars,
            "outages": [{"start": iso(o["start"]), "end": iso(o["end"]), "reason": o["reason"],
                         "minutes": o["minutes"]}
                        for o in reversed(outages)][:50],
        }
    return out


# ---------------------------------------------------------------- loop

async def tick() -> None:
    global _ticks
    from shared import unifi_session
    client = await unifi_session.get_shared_client()
    if client is None:
        return
    slow = _ticks % SLOW_EVERY == 0
    _ticks += 1
    health = await client.get_health()
    devices = await client.get_devices_raw() if slow else None
    async for session in get_database().get_session():
        if health:
            await record_wan(session, wan_samples_from_health(health))
        if devices:
            await record_speedtest(session, speedtest_from_devices(devices))
            await record_firmware(session, firmware_rows(devices))
            await purge(session)


async def _loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Insights check failed")
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

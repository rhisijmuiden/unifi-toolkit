"""
Insights page and API: firmware overview, WAN availability, speed tests.
"""
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from shared import insights as IN
from shared.database import get_database
from shared.models.insights import FirmwareChange, SpeedTest, WanSample
from shared.templating import make_templates

router = APIRouter(tags=["insights"])
templates = make_templates(str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request):
    return templates.TemplateResponse(request, "insights.html", {})


@router.get("/api/insights/firmware")
async def firmware():
    """
    Live firmware per device, plus the logged changes. Without a console the
    change history is still returned, with devices None and the reason.
    """
    from shared import unifi_session
    client = await unifi_session.get_shared_client()
    rows, error = None, None
    if client is None:
        error = unifi_session.connection_problem() or "No UniFi console is connected."
    else:
        rows = IN.firmware_rows(await client.get_devices_raw())
    async for session in get_database().get_session():
        if rows:
            await IN.record_firmware(session, rows)
        changes = (await session.execute(
            select(FirmwareChange).where(FirmwareChange.old_version.isnot(None))
            .order_by(FirmwareChange.id.desc()).limit(50))).scalars().all()
        return {
            "devices": rows,
            "error": error,
            "upgradable": sum(1 for r in rows or [] if r["upgradable"]),
            "changes": [{"ts": IN.iso(c.ts), "mac": c.mac, "name": c.name, "model": c.model,
                         "old_version": c.old_version, "new_version": c.new_version} for c in changes],
        }


@router.get("/api/insights/wan")
async def wan(days: int = 30):
    days = max(1, min(days, IN.WAN_RETENTION_DAYS))
    today = datetime.now().astimezone().date()
    since = IN.utcnow() - timedelta(days=days)
    async for session in get_database().get_session():
        rows = (await session.execute(select(WanSample).where(WanSample.ts >= since))).scalars().all()
        first = (await session.execute(select(WanSample.ts).order_by(WanSample.ts).limit(1))).scalar()
        return {"days": days, "since": IN.iso(first), "wans": IN.summarize_wan(rows, days, today)}


@router.get("/api/insights/speedtests")
async def speedtests(days: int = 90):
    since = IN.utcnow() - timedelta(days=max(1, min(days, 3650)))
    async for session in get_database().get_session():
        rows = (await session.execute(select(SpeedTest).where(SpeedTest.run_at >= since)
                                      .order_by(SpeedTest.run_at))).scalars().all()
        tests = [{"run_at": IN.iso(t.run_at), "download_mbps": t.download_mbps, "upload_mbps": t.upload_mbps,
                  "latency_ms": t.latency_ms, "server": t.server} for t in rows]

        def avg(key):
            v = [t[key] for t in tests if t[key] is not None]
            return round(sum(v) / len(v), 1) if v else None
        return {"days": days, "tests": tests,
                "avg": {"download_mbps": avg("download_mbps"), "upload_mbps": avg("upload_mbps"),
                        "latency_ms": avg("latency_ms")}}

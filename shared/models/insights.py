"""
History the toolkit keeps itself: WAN availability samples, speed test
results and firmware changes. UniFi only shows a short window of these.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, UniqueConstraint

from shared.models.base import Base


def _now():
    return datetime.now(timezone.utc)


class WanSample(Base):
    """One check of one WAN, about once a minute. Kept for WAN_RETENTION_DAYS."""
    __tablename__ = "insights_wan_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    wan = Column(String, nullable=False)  # wan, wan2, ...
    up = Column(Boolean, nullable=False)
    latency_ms = Column(Float, nullable=True)
    wan_ip = Column(String, nullable=True)
    isp = Column(String, nullable=True)
    reason = Column(String, nullable=True)


class SpeedTest(Base):
    """A speed test the gateway ran (scheduled in UniFi or by hand)."""
    __tablename__ = "insights_speedtests"
    __table_args__ = (UniqueConstraint("run_at", name="speedtest_run_at_unique"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    download_mbps = Column(Float, nullable=True)
    upload_mbps = Column(Float, nullable=True)
    latency_ms = Column(Float, nullable=True)
    wan = Column(String, nullable=True)
    server = Column(String, nullable=True)


class FirmwareChange(Base):
    """A device that came back with another firmware version."""
    __tablename__ = "insights_firmware_changes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    mac = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    model = Column(String, nullable=True)
    old_version = Column(String, nullable=True)  # None: first time seen
    new_version = Column(String, nullable=False)

"""
Mail alerts, the weekly report, and the devices the toolkit has seen.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text

from shared.models.base import Base


def _now():
    return datetime.now(timezone.utc)


class AlertSettings(Base):
    """Single row: which alerts are on, and the weekly report schedule."""
    __tablename__ = "alert_settings"
    __table_args__ = (CheckConstraint("id = 1", name="alert_single_row_check"),)

    id = Column(Integer, primary_key=True)
    # Comma-separated; empty means the mail server's default recipients.
    recipients = Column(String, nullable=True)

    unknown_device_enabled = Column(Boolean, nullable=False, default=False)
    threat_enabled = Column(Boolean, nullable=False, default=False)
    # Suricata severity: 1 = high, 2 = medium, 3 = low. Mail events at or above this level.
    threat_min_severity = Column(Integer, nullable=False, default=1)
    console_enabled = Column(Boolean, nullable=False, default=False)

    report_enabled = Column(Boolean, nullable=False, default=False)
    report_weekday = Column(Integer, nullable=False, default=0)  # 0 = Monday
    report_time = Column(String, nullable=False, default="08:00")

    # Bookkeeping
    devices_seeded = Column(Boolean, nullable=False, default=False)
    last_threat_id = Column(Integer, nullable=True)
    last_report_date = Column(String, nullable=True)  # YYYY-MM-DD of the last scheduled report
    console_down_since = Column(DateTime(timezone=True), nullable=True)
    console_down_alerted = Column(Boolean, nullable=False, default=False)


class KnownDevice(Base):
    """Every client MAC the toolkit has seen on the network."""
    __tablename__ = "known_devices"

    mac = Column(String, primary_key=True)
    first_seen = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen = Column(DateTime(timezone=True), nullable=False, default=_now)
    name = Column(String, nullable=True)
    hostname = Column(String, nullable=True)
    vendor = Column(String, nullable=True)
    ip = Column(String, nullable=True)
    network = Column(String, nullable=True)
    essid = Column(String, nullable=True)
    is_wired = Column(Boolean, nullable=False, default=False)
    # Seen in the first scan, before alerts were on: never reported as new.
    baseline = Column(Boolean, nullable=False, default=False)
    # The user marked it as theirs.
    approved = Column(Boolean, nullable=False, default=False)


class AlertLog(Base):
    """Every alert mail the toolkit tried to send."""
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    kind = Column(String, nullable=False)  # unknown_device | threat | console_down | console_up | report | test
    subject = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    sent = Column(Boolean, nullable=False, default=False)
    error = Column(Text, nullable=True)

"""
Outgoing mail (SMTP) settings, shared by every feature that sends mail.
"""
from sqlalchemy import Boolean, CheckConstraint, Column, Integer, LargeBinary, String

from shared.models.base import Base


class MailSettings(Base):
    """Single row: the SMTP server the toolkit sends mail through."""
    __tablename__ = "mail_settings"
    __table_args__ = (CheckConstraint("id = 1", name="mail_single_row_check"),)

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    host = Column(String, nullable=False, default="")
    port = Column(Integer, nullable=False, default=587)
    # "starttls" (port 587), "ssl" (implicit TLS, port 465) or "none" (port 25, LAN relays only)
    security = Column(String, nullable=False, default="starttls")
    verify_tls = Column(Boolean, nullable=False, default=True)
    username = Column(String, nullable=True)
    password_encrypted = Column(LargeBinary, nullable=True)
    from_address = Column(String, nullable=False, default="")
    from_name = Column(String, nullable=False, default="UniFi Toolkit")
    reply_to = Column(String, nullable=True)
    # Comma-separated default recipients for alerts and reports.
    default_recipients = Column(String, nullable=True)
    timeout = Column(Integer, nullable=False, default=20)

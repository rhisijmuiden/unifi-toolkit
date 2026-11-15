"""
Backup settings, remote backup locations and the backup history.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, LargeBinary, String, Text

from shared.models.base import Base


def _now():
    return datetime.now(timezone.utc)


class BackupSettings(Base):
    """Single row: schedule, retention and the archive passphrase."""
    __tablename__ = "backup_settings"
    __table_args__ = (CheckConstraint("id = 1", name="backup_single_row_check"),)

    id = Column(Integer, primary_key=True)
    # Scheduled backups. Manual backups work while this is off.
    enabled = Column(Boolean, nullable=False, default=False)
    run_time = Column(String, nullable=False, default="03:00")  # HH:MM, container local time
    # A full backup starts a new chain every N days; the other days are incremental.
    full_every_days = Column(Integer, nullable=False, default=7)
    # How many chains (a full backup plus its incrementals) to keep, locally and remotely.
    keep_chains = Column(Integer, nullable=False, default=4)
    # Every archive is encrypted with this passphrase (scrypt + AES-256-GCM).
    passphrase_encrypted = Column(LargeBinary, nullable=True)
    notify_on_failure = Column(Boolean, nullable=False, default=True)
    # Date (YYYY-MM-DD) of the last scheduled run, so a run happens once a day.
    last_scheduled_date = Column(String, nullable=True)


class BackupDestination(Base):
    """A remote location backups are copied to: FTP, FTPS or SCP."""
    __tablename__ = "backup_destinations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # ftp | ftps | scp
    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String, nullable=True)
    password_encrypted = Column(LargeBinary, nullable=True)
    # SCP only: an OpenSSH private key, used instead of or with the password.
    private_key_encrypted = Column(LargeBinary, nullable=True)
    # SCP only: the server's host key, pinned on the first successful connection.
    host_key = Column(Text, nullable=True)
    # FTPS only: check the server's certificate.
    verify_tls = Column(Boolean, nullable=False, default=True)
    path = Column(String, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    last_status = Column(String, nullable=True)  # ok | failed
    last_message = Column(Text, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)


class BackupRun(Base):
    """One backup or restore, for the history list."""
    __tablename__ = "backup_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String, nullable=False, default="backup")  # backup | restore
    kind = Column(String, nullable=True)  # full | incremental
    trigger = Column(String, nullable=False, default="manual")  # manual | schedule
    name = Column(String, nullable=True)  # archive file name
    status = Column(String, nullable=False, default="running")  # running | ok | partial | failed
    size = Column(Integer, nullable=True)
    message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    finished_at = Column(DateTime(timezone=True), nullable=True)

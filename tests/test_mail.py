"""Mail server settings and the SMTP sender (shared/mailer.py, app/routers/mail.py)."""
import smtplib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import mailer
from shared.models.base import Base
from shared.models.mail_settings import MailSettings  # noqa: F401  (registers the table)
from app.routers import mail as mail_router


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what was done."""
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ssl_context = host, port, context
        self.extensions = {"starttls", "auth"}
        self.calls, self.sent = [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return name in self.extensions

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if password != "secret":
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def cfg(**kw):
    base = dict(host="smtp.example.com", port=587, from_address="toolkit@example.com")
    base.update(kw)
    return mailer.SmtpConfig(**base)


class TestMessage:
    def test_headers_and_both_parts(self):
        m = mailer.build_message(cfg(reply_to="ops@example.com"), ["a@example.com", "b@example.com"],
                                 "Hi", "plain", "<p>html</p>")
        assert m["To"] == "a@example.com, b@example.com"
        assert m["From"] == "UniFi Toolkit <toolkit@example.com>"
        assert m["Reply-To"] == "ops@example.com"
        assert m["Message-ID"].endswith("@example.com>")
        assert m["Date"] and m["Auto-Submitted"] == "auto-generated"
        assert [p.get_content_type() for p in m.iter_parts()] == ["text/plain", "text/html"]

    def test_bad_address_refused(self):
        with pytest.raises(mailer.MailError, match="Not a valid"):
            mailer.build_message(cfg(), ["not-an-address"], "s", "t")

    def test_no_recipients_refused(self):
        with pytest.raises(mailer.MailError, match="No recipients"):
            mailer.build_message(cfg(), [], "s", "t")

    def test_html_layout_escapes_title(self):
        out = mailer.render_html("<b>x</b>", "<p>ok</p>")
        assert "&lt;b&gt;x&lt;/b&gt;" in out and "<p>ok</p>" in out

    def test_split_recipients(self):
        assert mailer.split_recipients(" a@x.nl; b@x.nl, ,") == ["a@x.nl", "b@x.nl"]


class TestSend:
    async def test_starttls_then_login(self):
        await mailer.send_mail(["a@example.com"], "s", "t", cfg=cfg(username="u", password="secret"))
        s = FakeSMTP.instances[0]
        assert s.calls[:4] == ["ehlo", "starttls", "ehlo", ("login", "u")]
        assert len(s.sent) == 1

    async def test_ssl_uses_tls_context_with_minimum_version(self):
        await mailer.send_mail(["a@example.com"], "s", "t", cfg=cfg(security="ssl", port=465))
        s = FakeSMTP.instances[0]
        assert s.ssl_context.minimum_version >= mailer.ssl.TLSVersion.TLSv1_2
        assert "starttls" not in s.calls

    async def test_missing_starttls_is_an_error(self, fake_smtp, monkeypatch):
        orig = FakeSMTP.__init__

        def no_tls(self, *a, **k):
            orig(self, *a, **k)
            self.extensions = set()
        monkeypatch.setattr(FakeSMTP, "__init__", no_tls)
        with pytest.raises(mailer.MailError, match="STARTTLS"):
            await mailer.send_mail(["a@example.com"], "s", "t", cfg=cfg())

    async def test_password_never_sent_in_plain_text(self):
        with pytest.raises(mailer.MailError, match="unencrypted"):
            await mailer.send_mail(["a@example.com"], "s", "t",
                                   cfg=cfg(security="none", port=25, username="u", password="secret"))
        assert FakeSMTP.instances[0].sent == []

    async def test_bad_login_is_readable(self):
        with pytest.raises(mailer.MailError, match="rejected the username"):
            await mailer.send_mail(["a@example.com"], "s", "t", cfg=cfg(username="u", password="wrong"))

    def test_unverified_context(self):
        ctx = mailer._tls_context(False)
        assert ctx.check_hostname is False and ctx.verify_mode == mailer.ssl.CERT_NONE


@pytest.fixture
def client(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class FakeDB:
        async def get_session(self):
            async with factory() as session:
                yield session

    import asyncio

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(create())
    monkeypatch.setattr(mail_router, "get_database", lambda: FakeDB())
    monkeypatch.setattr(mailer, "get_database", lambda: FakeDB())
    app = FastAPI()
    app.include_router(mail_router.router)
    return TestClient(app)


SETTINGS = {
    "enabled": True, "host": "smtp.example.com", "port": 587, "security": "starttls",
    "username": "u", "password": "secret", "from_address": "toolkit@example.com",
    "default_recipients": "ops@example.com; noc@example.com",
}


class TestRouter:
    def test_defaults_when_nothing_saved(self, client):
        s = client.get("/api/mail/settings").json()
        assert s["enabled"] is False and s["port"] == 587 and s["has_password"] is False

    def test_password_is_never_returned_and_kept_on_empty(self, client):
        r = client.put("/api/mail/settings", json=SETTINGS)
        assert r.status_code == 200
        body = r.json()
        assert "password" not in body and body["has_password"] is True
        assert body["default_recipients"] == "ops@example.com, noc@example.com"
        r = client.put("/api/mail/settings", json=dict(SETTINGS, password=""))
        assert r.json()["has_password"] is True
        r = client.put("/api/mail/settings", json=dict(SETTINGS, password="", clear_password=True))
        assert r.json()["has_password"] is False

    @pytest.mark.parametrize("change,reason", [
        ({"security": "tls13"}, "Security"),
        ({"host": ""}, "SMTP server"),
        ({"from_address": "nope"}, "Not a valid"),
        ({"default_recipients": "a@b.nl, broken"}, "broken"),
        ({"security": "none"}, "encrypted connection"),
    ])
    def test_validation(self, client, change, reason):
        r = client.put("/api/mail/settings", json=dict(SETTINGS, **change))
        assert r.status_code == 422 and reason in r.json()["detail"]

    def test_test_mail_goes_to_default_recipients(self, client):
        client.put("/api/mail/settings", json=SETTINGS)
        r = client.post("/api/mail/test", json={})
        assert r.status_code == 200
        assert r.json()["to"] == ["ops@example.com", "noc@example.com"]
        msg = FakeSMTP.instances[-1].sent[0]
        assert msg["Subject"] == "UniFi Toolkit test message"
        assert ("login", "u") in FakeSMTP.instances[-1].calls

    def test_test_mail_reports_smtp_errors(self, client):
        client.put("/api/mail/settings", json=dict(SETTINGS, password="wrong"))
        r = client.post("/api/mail/test", json={"to": "me@example.com"})
        assert r.status_code == 502 and "rejected" in r.json()["detail"]

    def test_test_mail_needs_settings(self, client):
        r = client.post("/api/mail/test", json={"to": "me@example.com"})
        assert r.status_code == 409


async def test_smtp_errors_are_not_reported_as_connection_errors(monkeypatch):
    def boom(self, msg):
        raise smtplib.SMTPDataError(554, b"rejected")
    monkeypatch.setattr(FakeSMTP, "send_message", boom)
    with pytest.raises(mailer.MailError, match="SMTP error"):
        await mailer.send_mail(["a@example.com"], "s", "t", cfg=cfg())

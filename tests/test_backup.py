"""Backups: archive format, encryption, incremental chains, restore and the FTP/FTPS/SCP transports."""
import asyncio
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from shared.backup import archive as A
from shared.backup.archive import BackupError
from shared.backup.transports import Location, transport_for

PASS = "correct horse battery staple"


def make_db(path: Path, rows: int = 12000, blob: int = 600) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v BLOB)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(os.urandom(blob),) for _ in range(rows)])
    con.commit()
    con.close()


def count(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------- names and chains

def test_names_and_chains():
    names = [
        "unifi-toolkit-20261101-030000-incr.utkb",  # orphan: no full before it
        "unifi-toolkit-20261102-030000-full.utkb",
        "unifi-toolkit-20261103-030000-incr.utkb",
        "unifi-toolkit-20261104-030000-incr.utkb",
        "unifi-toolkit-20261109-030000-full.utkb",
        "unifi-toolkit-20261110-030000-incr.utkb",
        "notes.txt",
        "unifi-toolkit-20261111-030000-incr.utkb.part",
    ]
    chains = A.group_chains(names)
    assert [len(c) for c in chains] == [3, 2]
    assert A.chain_for(names, "unifi-toolkit-20261104-030000-incr.utkb") == names[1:4]
    assert A.chain_for(names, "unifi-toolkit-20261109-030000-full.utkb") == [names[4]]
    with pytest.raises(BackupError):
        A.chain_for(names, "unifi-toolkit-20261101-030000-incr.utkb")


# ---------------------------------------------------------------- encryption

def test_encrypt_roundtrip_and_failures(tmp_path):
    src = tmp_path / "plain"
    src.write_bytes(os.urandom(A.SEGMENT * 2 + 123))
    enc, out = tmp_path / "enc", tmp_path / "out"
    A.encrypt_file(src, enc, PASS)
    A.decrypt_file(enc, out, PASS)
    assert out.read_bytes() == src.read_bytes()

    with pytest.raises(BackupError, match="Wrong passphrase"):
        A.decrypt_file(enc, out, "wrong passphrase!")

    data = bytearray(enc.read_bytes())
    data[-10] ^= 1
    (tmp_path / "tampered").write_bytes(bytes(data))
    with pytest.raises(BackupError, match="damaged"):
        A.decrypt_file(tmp_path / "tampered", out, PASS)

    # Cutting the file at a segment boundary must not pass as a complete file.
    whole = enc.read_bytes()
    first_len = int.from_bytes(whole[32:36], "big")
    (tmp_path / "cut").write_bytes(whole[:36 + first_len])
    with pytest.raises(BackupError):
        A.decrypt_file(tmp_path / "cut", out, PASS)


def test_empty_file_roundtrip(tmp_path):
    (tmp_path / "e").write_bytes(b"")
    A.encrypt_file(tmp_path / "e", tmp_path / "e.enc", PASS)
    A.decrypt_file(tmp_path / "e.enc", tmp_path / "e.out", PASS)
    assert (tmp_path / "e.out").read_bytes() == b""


# ---------------------------------------------------------------- full + incremental

def test_full_and_incremental_restore(tmp_path):
    db = tmp_path / "live.db"
    make_db(db)
    work = tmp_path / "work"
    full = A.build_backup(db, work, kind="full", stamp="20261101-030000", passphrase=PASS, meta={"x": 1})
    assert full.manifest["chain"] == full.name and full.manifest["x"] == 1
    assert set(full.manifest["stored_chunks"]) == set(full.manifest["chunks"])

    make_db(db, rows=200)  # appends: most chunks stay the same
    inc = A.build_backup(db, work, kind="incremental", stamp="20261102-030000", passphrase=PASS,
                         known_chunks=full.manifest["stored_chunks"], chain=full.name, parent=full.name)
    assert inc.manifest["chain"] == full.name and inc.manifest["parent"] == full.name
    assert 0 < len(inc.manifest["stored_chunks"]) < len(inc.manifest["chunks"])
    assert inc.path.stat().st_size < full.path.stat().st_size / 2

    out = tmp_path / "restored.db"
    m = A.assemble([full.path, inc.path], PASS, out, tmp_path / "w2")
    assert m["name"] == inc.name and count(out) == 12200

    # The full backup alone restores the older state.
    A.assemble([full.path], PASS, tmp_path / "older.db", tmp_path / "w3")
    assert count(tmp_path / "older.db") == 12000

    # An incremental without its full backup cannot be restored.
    with pytest.raises(BackupError, match="missing"):
        A.assemble([inc.path], PASS, tmp_path / "x.db", tmp_path / "w4")
    assert not list(work.glob("*.db")) and not list(work.glob("*.tar.gz"))


def test_reencrypt_credentials(tmp_path):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    db = tmp_path / "c.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg (id INTEGER PRIMARY KEY, password_encrypted BLOB, other BLOB)")
    con.execute("INSERT INTO cfg VALUES (1, ?, ?)", (Fernet(old.encode()).encrypt(b"s3cret"), b"raw"))
    con.execute("INSERT INTO cfg VALUES (2, NULL, NULL)")
    con.commit()
    con.close()
    assert A.reencrypt_credentials(db, old, new) == 1
    con = sqlite3.connect(db)
    pw, other = con.execute("SELECT password_encrypted, other FROM cfg WHERE id=1").fetchone()
    con.close()
    assert Fernet(new.encode()).decrypt(pw) == b"s3cret" and other == b"raw"
    assert A.reencrypt_credentials(db, new, new) == 0


# ---------------------------------------------------------------- transports

async def roundtrip(loc: Location, tmp_path: Path):
    t = transport_for(loc)
    res = await t.test()
    src = tmp_path / "up.bin"
    src.write_bytes(os.urandom(300_000))
    name = "unifi-toolkit-20261101-030000-full.utkb"
    await t.upload(src, name)
    assert name in await t.list()
    assert name + ".part" not in await t.list()
    await t.download(name, tmp_path / "down.bin")
    assert (tmp_path / "down.bin").read_bytes() == src.read_bytes()
    await t.delete(name)
    assert name not in await t.list()
    return res


@pytest.fixture
def ftp_server(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.servers import ThreadedFTPServer

    root = tmp_path / "ftproot"
    root.mkdir()

    def start(tls: bool):
        if tls:
            pytest.importorskip("OpenSSL")
            from pyftpdlib.handlers import TLS_FTPHandler as Handler
            key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(crt), "-days", "1", "-subj", "/CN=localhost"], check=True,
                           capture_output=True)
            Handler.certfile, Handler.keyfile = str(crt), str(key)
            Handler.tls_control_required = True
            Handler.tls_data_required = True
        else:
            from pyftpdlib.handlers import FTPHandler as Handler
        auth = DummyAuthorizer()
        auth.add_user("u", "pw", str(root), perm="elradfmwMT")
        Handler.authorizer = auth
        srv = ThreadedFTPServer(("127.0.0.1", 0), Handler)
        th = threading.Thread(target=srv.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
        th.start()
        servers.append(srv)
        return srv.address[1]

    servers = []
    yield start, root
    for s in servers:
        s.close_all()


async def test_ftp(ftp_server, tmp_path):
    start, root = ftp_server
    port = start(False)
    loc = Location(kind="ftp", host="127.0.0.1", port=port, username="u", password="pw", path="a/b")
    await roundtrip(loc, tmp_path)
    assert (root / "a" / "b").is_dir()
    with pytest.raises(BackupError, match="rejected"):
        await transport_for(Location(kind="ftp", host="127.0.0.1", port=port, username="u", password="x")).list()


async def test_ftps(ftp_server, tmp_path):
    start, _ = ftp_server
    port = start(True)
    loc = Location(kind="ftps", host="127.0.0.1", port=port, username="u", password="pw", path="bk",
                   verify_tls=False)
    await roundtrip(loc, tmp_path)
    with pytest.raises(BackupError, match="not trusted"):
        await transport_for(Location(kind="ftps", host="127.0.0.1", port=port, username="u", password="pw")).list()


async def test_connection_refused_is_readable():
    with pytest.raises(BackupError, match="Could not connect"):
        await transport_for(Location(kind="ftp", host="127.0.0.1", port=1, timeout=3)).list()


@pytest.fixture
async def ssh_server(tmp_path):
    asyncssh = pytest.importorskip("asyncssh")
    root = tmp_path / "sshroot"
    root.mkdir()
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "u" and password == "pw"

        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return username == "u" and key == client_key.convert_to_public()

    async def shell(process):
        p = await asyncio.create_subprocess_shell(process.command, cwd=str(root),
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        process.stdout.write(out.decode())
        process.stderr.write(err.decode())
        process.exit(p.returncode)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_host_keys=[host_key], server_factory=Server, process_factory=shell,
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root).encode()), allow_scp=True)
    port = server.sockets[0].getsockname()[1]
    yield {"port": port, "root": root, "host_key": host_key, "client_key": client_key}
    server.close()
    await server.wait_closed()


async def test_scp_password_and_host_key_pinning(ssh_server, tmp_path):
    loc = Location(kind="scp", host="127.0.0.1", port=ssh_server["port"], username="u", password="pw", path="bk")
    with pytest.raises(BackupError, match="Test the location first"):
        await transport_for(loc).list()
    res = await transport_for(loc).test()
    assert res["host_key"].startswith("ssh-ed25519 ") and res["fingerprint"].startswith("SHA256:")
    loc.host_key = res["host_key"]
    await roundtrip(loc, tmp_path)
    assert (ssh_server["root"] / "bk").is_dir()

    import asyncssh
    other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    loc.host_key = other
    with pytest.raises(BackupError, match="host key has changed"):
        await transport_for(loc).list()


async def test_scp_private_key_and_bad_login(ssh_server, tmp_path):
    key = ssh_server["client_key"].export_private_key().decode()
    hk = ssh_server["host_key"].export_public_key().decode()
    loc = Location(kind="scp", host="127.0.0.1", port=ssh_server["port"], username="u", private_key=key,
                   host_key=hk, path="keyed")
    await roundtrip(loc, tmp_path)
    bad = Location(kind="scp", host="127.0.0.1", port=ssh_server["port"], username="u", password="nope", host_key=hk)
    with pytest.raises(BackupError, match="rejected the login"):
        await transport_for(bad).list()


# ---------------------------------------------------------------- service + API, end to end over FTP

@pytest.fixture
def service_env(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.routers import backup as router_mod
    from shared.backup import service as S
    from shared.models.base import Base
    from shared.models import backup as _models  # noqa: F401
    from shared.models import mail_settings as _mail  # noqa: F401

    db_file = tmp_path / "toolkit.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TOOLKIT_STATE_DIR", str(tmp_path / "state"))
    engine = create_async_engine(url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class FakeDB:
        async def get_session(self):
            async with factory() as session:
                yield session

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(create())
    for mod in (S, router_mod):
        monkeypatch.setattr(mod, "get_database", lambda: FakeDB())
    app = FastAPI()
    app.include_router(router_mod.router)
    yield TestClient(app), S, db_file
    asyncio.run(engine.dispose())


def test_backup_sync_retention_and_restore(service_env, ftp_server, tmp_path):
    client, S, db_file = service_env
    start, ftp_root = ftp_server
    port = start(False)

    r = client.put("/api/backup/settings", json={"enabled": False, "run_time": "03:00", "full_every_days": 7,
                                                 "keep_chains": 1, "passphrase": "short"})
    assert r.status_code == 422
    r = client.put("/api/backup/settings", json={"enabled": True, "run_time": "03:00", "full_every_days": 7,
                                                 "keep_chains": 1, "passphrase": PASS})
    assert r.status_code == 200 and r.json()["has_passphrase"] is True and "passphrase" not in r.json()

    r = client.post("/api/backup/destinations", json={"name": "NAS", "kind": "ftp", "host": "127.0.0.1",
                                                      "port": port, "username": "u", "password": "pw",
                                                      "path": "bk"})
    assert r.status_code == 200, r.text
    dest = r.json()
    assert dest["has_password"] and "password" not in dest
    assert client.post(f"/api/backup/destinations/{dest['id']}/test").status_code == 200

    # A credential in the database, encrypted with this install's key.
    from shared.crypto import encrypt_password
    con = sqlite3.connect(db_file)
    con.execute("INSERT INTO mail_settings (id, enabled, host, port, security, verify_tls, from_address, from_name,"
                " timeout, password_encrypted) VALUES (1, 0, 'h', 587, 'starttls', 1, '', 'x', 20, ?)",
                (encrypt_password("mailpw"),))
    con.commit()
    con.close()

    full = asyncio.run(S.run_backup("auto"))
    assert full["status"] == "ok" and full["kind"] == "full", full
    inc = asyncio.run(S.run_backup("auto"))
    assert inc["status"] == "ok" and inc["kind"] == "incremental", inc
    remote = sorted(p.name for p in (ftp_root / "bk").iterdir())
    assert remote == sorted([full["name"], inc["name"]])

    chains = client.get(f"/api/backup/destinations/{dest['id']}/backups").json()
    assert [i["name"] for i in chains[0]["items"]] == [inc["name"], full["name"]]

    # keep_chains=1: a new full backup removes the old chain here and remotely.
    new_full = asyncio.run(S.run_backup("full"))
    assert new_full["status"] == "ok"
    assert sorted(p.name for p in (ftp_root / "bk").iterdir()) == [new_full["name"]]
    assert S.local_names() == [new_full["name"]]

    # Restore from the remote location, as another install with another key would.
    con = sqlite3.connect(db_file)
    con.execute("UPDATE mail_settings SET host = 'changed-after-backup'")
    con.commit()
    con.close()
    other_key = Fernet.generate_key().decode()
    import shared.config as cfg_mod
    from types import SimpleNamespace
    real_get = cfg_mod.get_settings
    cfg_mod.get_settings = lambda: SimpleNamespace(encryption_key=other_key)  # what restore() re-encrypts to
    try:
        r = client.post("/api/backup/restore", json={"source": dest["id"], "name": new_full["name"], "confirm": True})
    finally:
        cfg_mod.get_settings = real_get
    assert r.status_code == 200, r.text
    pending = S.restore_dir() / "pending.db"
    assert pending.exists()
    S.schedule_restart = lambda *a, **k: None  # not used after the call, but keep the process alive

    line = S.apply_pending_restore()
    assert "Restored database" in line and not pending.exists()
    con = sqlite3.connect(db_file)
    host, pw = con.execute("SELECT host, password_encrypted FROM mail_settings").fetchone()
    con.close()
    assert host == "h"
    assert Fernet(other_key.encode()).decrypt(pw) == b"mailpw"
    assert S.last_restore()["name"] == new_full["name"]
    assert list(S.restore_dir().glob("before-restore-*.db"))


def test_restore_needs_confirmation_and_valid_name(service_env):
    client, S, _ = service_env
    assert client.post("/api/backup/restore", json={"source": "local", "name": "x", "confirm": False}).status_code == 422
    r = client.post("/api/backup/restore", json={"source": "local", "name": "../../etc/passwd", "confirm": True})
    assert r.status_code == 409


def test_scp_location_needs_a_secret(service_env):
    client, _, _ = service_env
    r = client.post("/api/backup/destinations", json={"name": "x", "kind": "scp", "host": "h", "username": "u"})
    assert r.status_code == 422 and "password or a private key" in r.json()["detail"]

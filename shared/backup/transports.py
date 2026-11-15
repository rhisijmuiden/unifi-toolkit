"""
Remote backup locations: FTP, FTPS (explicit TLS) and SCP.

Every transport offers the same async calls: list(), upload(), download(),
delete() and test(). Uploads go to `<name>.part` first and are renamed when
complete, so a broken upload never looks like a finished backup.
"""
import asyncio
import ftplib
import posixpath
import shlex
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from shared.backup.archive import BackupError

KINDS = {"ftp": 21, "ftps": 21, "scp": 22}
TEST_FILE = ".unifi-toolkit-write-test"


@dataclass
class Location:
    kind: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    private_key: Optional[str] = None
    host_key: Optional[str] = None
    verify_tls: bool = True
    path: str = ""
    timeout: int = 30


def transport_for(loc: Location):
    if loc.kind in ("ftp", "ftps"):
        return FtpTransport(loc)
    if loc.kind == "scp":
        return ScpTransport(loc)
    raise BackupError(f"Unknown location type: {loc.kind}")


# ---------------------------------------------------------------- FTP / FTPS

class _FTP_TLS(ftplib.FTP_TLS):
    """FTPS that reuses the control connection's TLS session on data connections.

    Servers such as vsftpd (require_ssl_reuse) and FileZilla Server refuse
    data connections that start a new TLS session, which plain ftplib does.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class FtpTransport:
    def __init__(self, loc: Location):
        self.loc = loc

    def _connect(self) -> ftplib.FTP:
        loc = self.loc
        try:
            if loc.kind == "ftps":
                ctx = ssl.create_default_context()
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                if not loc.verify_tls:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                ftp = _FTP_TLS(context=ctx, timeout=loc.timeout)
            else:
                ftp = ftplib.FTP(timeout=loc.timeout)
            ftp.connect(loc.host, loc.port)
            ftp.login(loc.username or "anonymous", loc.password or "")
            if loc.kind == "ftps":
                ftp.prot_p()
            ftp.set_pasv(True)
            self._cwd(ftp, loc.path)
            return ftp
        except BackupError:
            raise
        except ftplib.error_perm as e:
            msg = str(e)
            if msg.startswith("530"):
                raise BackupError("The FTP server rejected the username or password.")
            raise BackupError(f"FTP error: {msg}")
        except ssl.SSLCertVerificationError as e:
            raise BackupError(f"The server's TLS certificate is not trusted ({e.verify_message}). "
                              "Fix the certificate or turn off certificate checks.")
        except ssl.SSLError as e:
            raise BackupError(f"TLS failed: {e.reason or e}. Check that the server supports FTPS.")
        except ftplib.Error as e:
            raise BackupError(f"FTP error: {e}")
        except OSError as e:
            raise BackupError(f"Could not connect to {loc.host}:{loc.port} ({e}).")

    @staticmethod
    def _cwd(ftp: ftplib.FTP, path: str) -> None:
        path = (path or "").strip()
        if not path:
            return
        if path.startswith("/"):
            ftp.cwd("/")
        for part in [p for p in path.split("/") if p]:
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                try:
                    ftp.mkd(part)
                    ftp.cwd(part)
                except ftplib.error_perm as e:
                    raise BackupError(f"Cannot open or create the folder {path} ({e}).")

    def _run(self, fn):
        def work():
            ftp = self._connect()
            try:
                return fn(ftp)
            except BackupError:
                raise
            except ftplib.all_errors as e:
                raise BackupError(f"FTP error: {e}")
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
        return asyncio.to_thread(work)

    async def list(self) -> List[str]:
        def fn(ftp):
            try:
                return [posixpath.basename(n) for n in ftp.nlst()]
            except ftplib.error_perm as e:
                if str(e).startswith("550"):  # empty directory on some servers
                    return []
                raise
        return await self._run(fn)

    async def upload(self, local: Path, name: str) -> None:
        def fn(ftp):
            with open(local, "rb") as f:
                ftp.storbinary(f"STOR {name}.part", f)
            try:
                ftp.delete(name)
            except ftplib.error_perm:
                pass
            ftp.rename(f"{name}.part", name)
        await self._run(fn)

    async def download(self, name: str, local: Path) -> None:
        def fn(ftp):
            with open(local, "wb") as f:
                ftp.retrbinary(f"RETR {name}", f.write)
        await self._run(fn)

    async def delete(self, name: str) -> None:
        await self._run(lambda ftp: ftp.delete(name))

    async def test(self) -> dict:
        def fn(ftp):
            import io
            ftp.storbinary(f"STOR {TEST_FILE}", io.BytesIO(b"ok\n"))
            ftp.delete(TEST_FILE)
            return {"host_key": None}
        return await self._run(fn)


# ---------------------------------------------------------------- SCP

class ScpTransport:
    """SCP over SSH (asyncssh). Listing and deleting use `ls` and `rm` over the same connection.

    The server's host key is pinned: the first successful test stores it, and
    every later connection refuses a different key.
    """

    def __init__(self, loc: Location):
        self.loc = loc

    def _dir(self) -> str:
        return (self.loc.path or "").strip() or "."

    def _remote(self, name: str) -> str:
        return posixpath.join(self._dir(), name)

    async def _connect(self, allow_new_key: bool = False):
        import asyncssh
        loc = self.loc
        opts = dict(host=loc.host, port=loc.port, username=loc.username or None,
                    connect_timeout=loc.timeout, login_timeout=loc.timeout)
        if loc.password:
            opts["password"] = loc.password
        if loc.private_key:
            try:
                opts["client_keys"] = [asyncssh.import_private_key(loc.private_key)]
            except (asyncssh.KeyImportError, ValueError) as e:
                raise BackupError(f"The private key could not be read ({e}). A key with a passphrase is not supported.")
        else:
            opts["client_keys"] = None
        if loc.host_key:
            try:
                opts["known_hosts"] = ([asyncssh.import_public_key(loc.host_key)], [], [])
            except (asyncssh.KeyImportError, ValueError):
                raise BackupError("The saved host key is not valid. Clear it and test the location again.")
        elif allow_new_key:
            opts["known_hosts"] = None
        else:
            raise BackupError("The server's host key is not known yet. Test the location first.")
        try:
            return await asyncssh.connect(**opts)
        except asyncssh.HostKeyNotVerifiable:
            raise BackupError("The server's host key has changed. If you expected this, clear the saved key "
                              "and test the location again. Otherwise someone may be intercepting the connection.")
        except asyncssh.PermissionDenied:
            raise BackupError("The SSH server rejected the login.")
        except asyncssh.Error as e:
            raise BackupError(f"SSH error: {e.reason if hasattr(e, 'reason') else e}")
        except (OSError, asyncio.TimeoutError) as e:
            raise BackupError(f"Could not connect to {loc.host}:{loc.port} ({e or 'timed out'}).")

    @staticmethod
    async def _cmd(conn, command: str) -> str:
        r = await conn.run(command, check=False)
        if r.exit_status != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            raise BackupError(f"Command failed on the server: {err[-1] if err else command}")
        return r.stdout or ""

    async def _mkdir(self, conn) -> None:
        if self._dir() != ".":
            await self._cmd(conn, f"mkdir -p -- {shlex.quote(self._dir())}")

    async def list(self) -> List[str]:
        async with await self._connect() as conn:
            d = shlex.quote(self._dir())
            out = await self._cmd(conn, f"if [ -d {d} ]; then ls -1A -- {d}; fi")
            return [l.strip() for l in out.splitlines() if l.strip()]

    async def upload(self, local: Path, name: str) -> None:
        import asyncssh
        async with await self._connect() as conn:
            await self._mkdir(conn)
            part = self._remote(name + ".part")
            try:
                await asyncssh.scp(str(local), (conn, part))
            except asyncssh.Error as e:
                raise BackupError(f"SCP upload failed: {getattr(e, 'reason', e)}")
            await self._cmd(conn, f"mv -f -- {shlex.quote(part)} {shlex.quote(self._remote(name))}")

    async def download(self, name: str, local: Path) -> None:
        import asyncssh
        async with await self._connect() as conn:
            try:
                await asyncssh.scp((conn, self._remote(name)), str(local))
            except asyncssh.Error as e:
                raise BackupError(f"SCP download failed: {getattr(e, 'reason', e)}")

    async def delete(self, name: str) -> None:
        async with await self._connect() as conn:
            await self._cmd(conn, f"rm -f -- {shlex.quote(self._remote(name))}")

    async def test(self) -> dict:
        import asyncssh
        import tempfile
        async with await self._connect(allow_new_key=True) as conn:
            key = conn.get_server_host_key()
            await self._mkdir(conn)
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write(b"ok\n")
                tmp.flush()
                try:
                    await asyncssh.scp(tmp.name, (conn, self._remote(TEST_FILE)))
                except asyncssh.Error as e:
                    raise BackupError(f"SCP upload failed: {getattr(e, 'reason', e)}")
            await self._cmd(conn, f"rm -f -- {shlex.quote(self._remote(TEST_FILE))}")
            return {
                "host_key": key.export_public_key().decode().strip() if key else None,
                "fingerprint": key.get_fingerprint("sha256") if key else None,
            }

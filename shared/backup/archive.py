"""
Backup archives: database snapshot, chunking, encryption and restore.

A backup is one encrypted file, `unifi-toolkit-YYYYMMDD-HHMMSS-full.utkb` or
`...-incr.utkb`. Inside (after decryption) is a gzip'd tar with:

- `manifest.json`: what the backup is, the chunk list of the database and
  the toolkit's ENCRYPTION_KEY (needed to read the stored credentials back).
- `chunks/<sha256>`: 1 MiB pieces of a consistent SQLite snapshot.

A full backup holds every chunk. An incremental backup holds only the chunks
that no earlier backup of its chain holds, so restoring it needs the full
backup and the incrementals before it.

Encryption: scrypt (n=2^15, r=8, p=1) derives an AES-256-GCM key from the
passphrase. The file is sealed in 1 MiB segments, each with its own nonce and
the segment number plus a "last" flag in the associated data, so reordering,
truncation and tampering are all detected.
"""
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = 1
CHUNK_SIZE = 1024 * 1024
SEGMENT = 1024 * 1024
MAGIC = b"UTKBAK1\0"
NAME_RE = re.compile(r"^unifi-toolkit-(\d{8}-\d{6})-(full|incr)\.utkb$")


class BackupError(Exception):
    """A user-facing reason why a backup or restore failed."""


# ---------- names ----------

def make_name(stamp: str, kind: str) -> str:
    return f"unifi-toolkit-{stamp}-{'full' if kind == 'full' else 'incr'}.utkb"


def parse_name(name: str) -> Optional[dict]:
    m = NAME_RE.match(name)
    if not m:
        return None
    return {"name": name, "stamp": m.group(1), "kind": "full" if m.group(2) == "full" else "incremental"}


def group_chains(names: Iterable[str]) -> List[List[dict]]:
    """Group backup file names into chains, oldest first. Incrementals without a full are dropped."""
    items = sorted(filter(None, (parse_name(n) for n in names)), key=lambda x: x["stamp"])
    chains: List[List[dict]] = []
    for it in items:
        if it["kind"] == "full":
            chains.append([it])
        elif chains:
            chains[-1].append(it)
    return chains


def chain_for(names: Iterable[str], target: str) -> List[str]:
    """The files needed to restore `target`: its full backup and every incremental up to it."""
    for chain in group_chains(names):
        ns = [c["name"] for c in chain]
        if target in ns:
            return ns[: ns.index(target) + 1]
    raise BackupError(f"Backup {target} or the full backup it builds on is missing.")


# ---------- encryption ----------

def _derive(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise BackupError("No backup passphrase is set.")
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    salt, prefix = os.urandom(16), os.urandom(8)
    header = MAGIC + salt + prefix
    aes = AESGCM(_derive(passphrase, salt))
    size = src.stat().st_size
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(header)
        index, done = 0, 0
        while True:
            block = fi.read(SEGMENT)
            done += len(block)
            last = done >= size
            aad = header + struct.pack(">I?", index, last)
            ct = aes.encrypt(prefix + struct.pack(">I", index), block, aad)
            fo.write(struct.pack(">I", len(ct)) + ct)
            index += 1
            if last:
                break


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        header = fi.read(len(MAGIC) + 24)
        if len(header) != len(MAGIC) + 24 or not header.startswith(MAGIC):
            raise BackupError(f"{src.name} is not a UniFi Toolkit backup.")
        salt, prefix = header[len(MAGIC):len(MAGIC) + 16], header[len(MAGIC) + 16:]
        aes = AESGCM(_derive(passphrase, salt))
        index = 0
        while True:
            raw = fi.read(4)
            if len(raw) != 4:
                raise BackupError(f"{src.name} is incomplete.")
            ct = fi.read(struct.unpack(">I", raw)[0])
            nonce = prefix + struct.pack(">I", index)
            last = fi.peek(1)[:1] == b""
            try:
                pt = aes.decrypt(nonce, ct, header + struct.pack(">I?", index, last))
            except InvalidTag:
                if index == 0:
                    raise BackupError("Wrong passphrase, or the backup is damaged.")
                raise BackupError(f"{src.name} is damaged or incomplete.")
            fo.write(pt)
            index += 1
            if last:
                break


# ---------- snapshot and archive ----------

def snapshot_db(db_path: Path, dst: Path) -> None:
    """A consistent copy of a live SQLite database (online backup API)."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _chunks(path: Path):
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK_SIZE)
            if not b:
                return
            yield hashlib.sha256(b).hexdigest(), b


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(b)
    return h.hexdigest()


@dataclass
class Built:
    name: str
    manifest: dict
    path: Path


def build_backup(db_path: Path, workdir: Path, *, kind: str, stamp: str, passphrase: str,
                 known_chunks: Iterable[str] = (), chain: Optional[str] = None, parent: Optional[str] = None,
                 meta: Optional[dict] = None) -> Built:
    """Snapshot the database and write an encrypted archive into workdir."""
    workdir.mkdir(parents=True, exist_ok=True)
    name = make_name(stamp, kind)
    snap = workdir / (name + ".db")
    plain = workdir / (name + ".tar.gz")
    try:
        snapshot_db(db_path, snap)
        known = set(known_chunks) if kind == "incremental" else set()
        order, stored = [], set()
        with tarfile.open(plain, "w:gz", compresslevel=6) as tar:
            for sha, data in _chunks(snap):
                order.append(sha)
                if sha in known or sha in stored:
                    continue
                stored.add(sha)
                info = tarfile.TarInfo(f"chunks/{sha}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            manifest = {
                "format": FORMAT,
                "name": name,
                "stamp": stamp,
                "kind": kind,
                "chain": chain if kind == "incremental" else name,
                "parent": parent if kind == "incremental" else None,
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "chunk_size": CHUNK_SIZE,
                "chunks": order,
                "stored_chunks": sorted(stored),
                "db_size": snap.stat().st_size,
                "db_sha256": file_sha256(snap),
                **(meta or {}),
            }
            raw = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        out = workdir / name
        encrypt_file(plain, out, passphrase)
        return Built(name=name, manifest=manifest, path=out)
    finally:
        for p in (snap, plain):
            p.unlink(missing_ok=True)


def read_archive(path: Path, passphrase: str, workdir: Path, want: Optional[set] = None):
    """Decrypt one archive. Returns (manifest, {sha: bytes}) for the chunks in `want` (all if None)."""
    workdir.mkdir(parents=True, exist_ok=True)
    plain = workdir / (path.name + ".tar.gz")
    try:
        decrypt_file(path, plain, passphrase)
        manifest, chunks = None, {}
        try:
            with tarfile.open(plain, "r:gz") as tar:
                for m in tar:
                    if not m.isfile():
                        continue
                    if m.name == "manifest.json":
                        manifest = json.loads(tar.extractfile(m).read())
                    elif m.name.startswith("chunks/"):
                        sha = m.name[7:]
                        if want is None or sha in want:
                            data = tar.extractfile(m).read()
                            if hashlib.sha256(data).hexdigest() != sha:
                                raise BackupError(f"{path.name} holds a damaged chunk.")
                            chunks[sha] = data
        except (tarfile.TarError, gzip.BadGzipFile, EOFError, json.JSONDecodeError):
            raise BackupError(f"{path.name} is damaged.")
        if not manifest or manifest.get("format") != FORMAT:
            raise BackupError(f"{path.name} has no readable manifest.")
        return manifest, chunks
    finally:
        plain.unlink(missing_ok=True)


def assemble(files: List[Path], passphrase: str, dst: Path, workdir: Path) -> dict:
    """Rebuild the database of the LAST file in `files` (its chain, oldest first) into dst."""
    target_manifest, _ = read_archive(files[-1], passphrase, workdir, want=set())
    need = set(target_manifest["chunks"])
    have: Dict[str, bytes] = {}
    for f in files:
        m, chunks = read_archive(f, passphrase, workdir, want=need - set(have))
        if f is not files[-1] and m.get("chain") != target_manifest.get("chain"):
            raise BackupError(f"{f.name} belongs to another chain.")
        have.update(chunks)
    missing = need - set(have)
    if missing:
        raise BackupError(f"{len(missing)} piece(s) of the database are missing from the chain.")
    with open(dst, "wb") as out:
        for sha in target_manifest["chunks"]:
            out.write(have[sha])
    if file_sha256(dst) != target_manifest["db_sha256"]:
        raise BackupError("The restored database does not match its checksum.")
    con = sqlite3.connect(str(dst))
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        raise BackupError(f"The restored database failed its integrity check: {ok}")
    return target_manifest


def reencrypt_credentials(db_file: Path, old_key: str, new_key: str) -> int:
    """
    Re-encrypt every `*_encrypted` BLOB column from the backup's ENCRYPTION_KEY
    to this install's key, so credentials keep working after a restore on
    another install. Returns the number of values changed.
    """
    if not old_key or old_key == new_key:
        return 0
    old, new = Fernet(old_key.encode()), Fernet(new_key.encode())
    changed = 0
    con = sqlite3.connect(str(db_file))
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")') if r[1].endswith("_encrypted")]
            for c in cols:
                for rowid, val in con.execute(f'SELECT rowid, "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL').fetchall():
                    try:
                        plain = old.decrypt(bytes(val))
                    except (InvalidToken, TypeError):
                        continue
                    con.execute(f'UPDATE "{t}" SET "{c}" = ? WHERE rowid = ?', (new.encrypt(plain), rowid))
                    changed += 1
        con.commit()
    finally:
        con.close()
    return changed

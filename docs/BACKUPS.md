# Backups

**System > Backups** (`/system/backups`) backs up the toolkit's database: every
setting, the UniFi connection, tracked devices, events, saved DNS servers, the
mail server and the backup settings themselves. The code is not backed up; it
comes from git.

## How a backup works

1. The toolkit takes a consistent snapshot of the live SQLite database
   (SQLite's online backup API, so it never stops).
2. The snapshot is cut into 1 MiB pieces. A **full** backup stores every
   piece. An **incremental** backup stores only the pieces that no earlier
   backup of its chain holds.
3. The pieces and a manifest go into a gzip'd tar, which is **encrypted with
   your passphrase**: scrypt (n=2^15, r=8, p=1) derives an AES-256-GCM key, and
   the file is sealed in 1 MiB segments so tampering, reordering and a
   truncated upload are all detected.
4. The file is kept in `/var/lib/unifi-toolkit/backups` and copied to every
   backup location that is switched on. Uploads go to `<name>.part` first and
   are renamed when complete.

Files are named `unifi-toolkit-YYYYMMDD-HHMMSS-full.utkb` or `...-incr.utkb`.
A **chain** is one full backup plus the incremental backups after it.
Restoring an incremental backup needs its full backup and every incremental
before it; the toolkit downloads them for you.

The manifest also holds the toolkit's `ENCRYPTION_KEY`, which the stored
passwords and API keys are encrypted with. That is why the archive itself is
always encrypted, and why the passphrase matters: **without the passphrase a
backup cannot be restored**. Keep it in a password manager.

## Schedule and retention

| Setting | Meaning |
| --- | --- |
| Back up every day, Time | One backup a day at this time (the container's local time). A missed time is caught up when the toolkit starts. |
| Full backup every (days) | Start a new chain after this many days. The other days are incremental. |
| Full backups to keep | Chains to keep. Older chains are deleted here **and at every location**, so give the toolkit a folder of its own. |
| Mail me when a backup fails | Mails the default recipients of the mail server when a backup fails or could not be copied everywhere. |

Changing the passphrase starts a new chain. Older backups keep their old
passphrase; enter it in the restore dialog to restore one of them.

## Backup locations

| Type | Notes |
| --- | --- |
| **SCP** | Over SSH, with a password or an OpenSSH private key (without a passphrase). The server's host key is saved on the first successful test and checked on every connection after that; a changed key is refused until you choose **Forget host key** and test again. Listing and clean-up run `ls`, `mkdir`, `mv` and `rm` over the same SSH login, so the account needs a normal shell. |
| **FTPS** | Explicit FTPS (`AUTH TLS`), TLS 1.2 or newer, encrypted data connections, with TLS session reuse (needed by vsftpd and FileZilla Server). The certificate is checked unless you turn that off. |
| **FTP** | Plain FTP. The login travels unencrypted, so only use it on your own network. The backups themselves are still encrypted. |

**Save and test** connects, creates the folder if needed, writes and deletes a
small test file. Every backup run copies whatever a location is missing, so a
location that was offline catches up on the next run.

## Restore

Choose **Restore** next to a backup on this server, or open **Backups** on a
location to restore from there. The toolkit:

1. downloads the chain, decrypts it, rebuilds the database and checks its
   checksum and `PRAGMA integrity_check`,
2. refuses a backup made by a newer toolkit version (update first),
3. re-encrypts the stored credentials to this install's `ENCRYPTION_KEY`, so
   a backup restores onto a fresh install too,
4. stages it as `/var/lib/unifi-toolkit/restore/pending.db` and restarts.

On start, `run.py` keeps the current database as
`restore/before-restore-<time>.db` (the three newest are kept), swaps the
restored one in and then runs the migrations, so an older backup is upgraded.

The restart relies on systemd (`Restart=on-failure`): the toolkit exits with
code 75 after a restore. Run by hand, start it again yourself.

### Restoring onto a new container

Install the toolkit (`lxc/create-ct.sh` or `lxc/install.sh`), open
**System > Backups**, set the same passphrase, add the backup location, and
restore from it.

## Files

- `shared/backup/archive.py`: snapshot, chunks, encryption, rebuild, re-encryption.
- `shared/backup/transports.py`: FTP, FTPS and SCP.
- `shared/backup/service.py`: backup runs, copying, retention, restore, the daily schedule.
- `app/routers/backup.py` and `app/templates/system/backups.html`: API and page.

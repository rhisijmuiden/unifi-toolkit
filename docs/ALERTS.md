# Alerts and the weekly report

**System > Alerts** (`/system/alerts`) mails you when something needs
attention, and sends a short weekly report. Mail goes through the mail
server (see [MAIL.md](MAIL.md)), to the recipients on the Alerts page or,
when that field is empty, the mail server's default recipients.

A background loop (`shared/alerts.py`) runs every minute.

| Alert | When |
| --- | --- |
| New device | A client MAC the toolkit has never seen. The first scan after install only records what is already there (marked as baseline), so turning this on never mails every existing device. Several new devices in one minute arrive as one mail. |
| Threat | New Threat Watch events at or above the chosen severity (high; medium or high; any). Ignored events are left out. The first check starts after the newest stored event. |
| Console unreachable | The toolkit can't reach the UniFi console for 5 minutes. The mail gives the reason; a second mail follows when it is back. Short blips never mail. |
| Backup failed | Set on the Backups page; see [BACKUPS.md](BACKUPS.md). |

The toolkit records every device it sees in `known_devices` whether or not
the new-device alert is on, so the **New devices** list and the report work
from day one. **Mark as known** keeps a device on the list but tags it as
yours.

## Weekly report

Sent on the chosen weekday at the chosen time (container local time), and
on demand with **Send report now**. **Preview report** opens it in the
browser without sending. It covers the past 7 days:

- clients online now (wired and Wi-Fi),
- new devices,
- threat events: total, blocked, per severity, most seen signatures and sources,
- backups: OK, partly done, failed.

Every mail, sent or not, is listed under **Sent alerts** with the reason
when it failed.

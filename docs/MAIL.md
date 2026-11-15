# Mail server

The toolkit sends alerts and reports through an SMTP server you set in
**System > Mail server** (`/system/mail`). Every feature that sends mail uses
these settings through `shared/mailer.py`.

## Settings

| Setting | Notes |
| --- | --- |
| Send mail | Off by default. Nothing is sent while it is off. |
| SMTP server, Port | Usually port 587 with STARTTLS, or 465 with SSL/TLS. |
| Security | **STARTTLS** upgrades a plain connection to TLS. **SSL/TLS** is TLS from the first byte (SMTPS). **None** is only for a relay on your own network; the toolkit refuses to send a password over it. TLS 1.2 is the minimum. |
| Check the server's certificate | On by default. Turn it off only for a server with a self-signed certificate on your own network. |
| Username, Password | Leave the username empty for a relay without a login. The password is stored encrypted with the toolkit's `ENCRYPTION_KEY` and is never returned by the API. Leave the field empty to keep the saved password. |
| From address, From name, Reply-to | The sender of every message. |
| Default recipients | Comma-separated. Alerts, reports and the test message go here. |
| Timeout | Seconds to wait for the server (3 to 120). |

Messages are multipart (plain text and HTML in the RHIS layout) with `Date`,
`Message-ID` and `Auto-Submitted: auto-generated` headers.

## Testing

**Send test message** sends with the *saved* settings, so save first. When the
server refuses, the page shows the reason, for example a rejected login, an
untrusted certificate or a server that does not offer STARTTLS.

## API

All state-changing calls need the `X-Requested-With: XMLHttpRequest` header.

- `GET /api/mail/settings` returns the settings, with `has_password` instead of the password.
- `PUT /api/mail/settings` saves them. `password: null` or `""` keeps the saved password; `clear_password: true` removes it.
- `POST /api/mail/test` with `{"to": "a@example.com"}` (optional) sends a test message.

## Sending mail from code

```python
from shared.mailer import send_mail, render_html, MailError

await send_mail(["ops@example.com"], "Subject", "Plain text",
                render_html("Title", "<p>HTML body</p>"))
```

`send_mail` raises `MailError` with a readable reason, including when mail is
switched off.

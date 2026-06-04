# Mail OTP Web Service

Standalone Outlook/Hotmail inbox diagnostic UI for Microsoft Graph refresh-token
mailboxes. It is useful when an operator wants to validate a mailbox line and
see the latest messages without running a full ChatGPT registration.

## Boundary

- Owns only local HTTP UI/API rendering, Microsoft refresh-token exchange, Graph
  message fetch, and OTP extraction.
- Accepts mailbox lines compatible with `sms_tool.mailbox`:
  - `email----password----client_id----refresh_token`
  - `email----password----refresh_token----client_id`
  - `email---password---refresh_token---access_token---0`
- Does not persist mailbox credentials, update `hotmail.txt`, update session
  JSON, or write SQLite account rows.
- If Microsoft returns a rotated refresh token, it returns the value in the API
  response for the operator to copy manually; it does not overwrite source files.

## Run locally

```powershell
python services\mail-otp-web\app.py
```

Default bind is read from `services/mail-otp-web/config.json` and is local-only. Start from `config.example.json` if you need to customize it:

```text
http://127.0.0.1:8791
```

Use `MAIL_OTP_CONFIG` to point at another config file:

```powershell
$env:MAIL_OTP_CONFIG = "F:\epsoft\GPT-Register-Tool\services\mail-otp-web\config.json"
python services\mail-otp-web\app.py
```

## API

`POST /api/extract`

```json
{
  "account_line": "user@hotmail.com----password----client_id----refresh_token",
  "subject_keyword": "openai",
  "limit": 12
}
```

The response is redacted by design: it returns message summaries and OTP
candidates, not full mailbox credentials.

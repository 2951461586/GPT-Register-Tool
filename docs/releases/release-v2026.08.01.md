# v2026.08.01

## Maintenance and cleanup

- Probed the active account pool and isolated 33 invalid or deactivated sessions (3 HTTP 401/token-invalid and 30 explicitly disabled). The active SQLite/session pool now contains 290 healthy accounts; network-unknown results were not deleted.
- Removed the retired PayPal no-card implementation, its configuration, and its one-off batch entry points. PayPal redirect parsing now has a single focused protocol module and regression coverage.
- Removed stale ReMail Agent Identity batch scripts.
- Removed registration payment-stage settings and the desktop's unconditional compatibility flag injection. The hidden CLI flag remains accepted for one compatibility release.
- Marked generated backup files as ignored and refreshed the architecture and directory ownership documents.

## Inbox and desktop

- Mail details now render readable plain text: scripts/styles and HTML tags are removed, entities are decoded, paragraphs are preserved, and empty messages have an explicit fallback.
- Added `MailBodyFormatter` unit coverage and moved PayPal protocol tests into focused files.

## Validation

- `python -m pytest -q`: 545 passed, 1 skipped, 7 subtests passed.
- `python -m compileall -q sms_tool scripts`: passed.
- `.dotnet/dotnet.exe test GPTRegisterTool.slnx --no-restore`: 20 passed.
- Desktop publish output is produced by `SmsWorkbench/build_dotnet.ps1` under `dist/net10`.

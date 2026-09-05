# ADR-0001: Unified Mailbox Provider Layout

- Status: Accepted
- Date: 2026-09-04

All mailbox provider implementations live under `sms_tool/providers/`. The
top-level `mailbox_*` modules remain import-compatible facades only. Routing and
OTP orchestration stay in `mailbox.py`, `mailbox_service.py`, and
`mailbox_strategies.py`.

This gives new providers one physical home while preserving existing callers.

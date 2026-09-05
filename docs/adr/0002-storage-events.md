# ADR-0002: Storage Emits Facts, Not Provider Side Effects

- Status: Accepted
- Date: 2026-09-04

The storage module persists account state and emits domain facts through
`sms_tool.account_events`. Provider-specific history or cleanup is handled by
the event dispatcher, so storage does not import mailbox implementations.

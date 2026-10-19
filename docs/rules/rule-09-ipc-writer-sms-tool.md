# Rule 09 — One IPC writer: `sms_tool/desktop_ipc.py` is the sole writer of the `@@SMSWORKBENCH_V2@@` envelope

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 9`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "SMSWORKBENCH_V2" sms_tool/`
- **Source section**: WPF UI

## Rule

One IPC writer: `sms_tool/desktop_ipc.py` is the sole writer of the `@@SMSWORKBENCH_V2@@` envelope.

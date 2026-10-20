# Rule 03 — Provider adapters must not import the registration or payment workflow

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 3`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "from \.\.\(registration\|payment\|paypal\)" sms_tool/providers/`
  and `scripts/architecture_scan.py::provider_port_violations` (the
  `desktop_ipc` / `mailbox_pool_writer` half, widened 2026-10-21)
- **Source section**: Dependency Direction; Ownership Matrix

## Rule

Provider adapters must not import the registration or payment workflow.

They also must not import the two application concerns that the routing owner
decides: **pool persistence** (`mailbox_pool_writer`) and the **stdout/IPC
channel** (`desktop_ipc`). Both arrive through `sms_tool/providers/ports.py`,
which `sms_tool/mailbox.py`'s layer wires lazily; an unwired rotation raises
rather than silently skipping the write-back.

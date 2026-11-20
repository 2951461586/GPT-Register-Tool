# Rule 03 — Provider adapters must not import the registration or payment workflow

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 3`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "from \.\.\(registration\|payment\|paypal\)" sms_tool/providers/`
  and `scripts/architecture_scan.py::provider_port_violations` (the
  `desktop.ipc` / `mailbox.pool_writer` half, widened 2026-10-21, path-based
  since 2026-11-20)
- **Source section**: Dependency Direction; Ownership Matrix

## Rule

Provider adapters must not import the registration or payment workflow.

They also must not import the two application concerns that the routing owner
decides: **pool persistence** (`sms_tool/mailbox/pool_writer.py`) and the
**stdout/IPC channel** (`sms_tool/desktop/ipc.py`). Both arrive through
`sms_tool/providers/ports.py`, which `sms_tool/mailbox/router.py`'s layer wires
lazily; an unwired rotation raises rather than silently skipping the write-back.

The scan names those two modules by **path relative to the `sms_tool` package**,
not by their former flat module names: the 2026-11-19 move emptied the old
bare-name check without failing it. `tests/governance/test_provider_ports.py`
pins each entry to a module that still exists.

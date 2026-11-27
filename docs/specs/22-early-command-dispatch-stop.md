---
id: spec-22-early-command-dispatch-stop
type: architecture-design
status: accepted
owner: sms_tool/commands/cli_dispatch.py
implements:
  - rule-24
depends-on:
  - spec-17-cli-lazy-command-seams
---

# Early CLI commands terminate dispatch after emitting their result

An early command handled in `main_early_commands` (`--desktop-read`, `--doctor`, `--import-account-json`, `--inspect-accounts`) must return `True` or exit immediately so execution never falls through into the registration pipeline.

## Enforced by

- `rule-24` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.

# Rule 12 — Command adapters call public workflow functions; they must not import private provider helpers or write SQLite/session files

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 12`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "^from \.\.\(pay_link\|store\|wallet_provider\|providers\)" sms_tool/commands/`
- **Source section**: Dependency Direction; CLI

## Rule

Command adapters call public workflow functions; they must not import private provider helpers or write SQLite/session files.

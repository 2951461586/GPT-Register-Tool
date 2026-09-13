# Telemetry And Runtime Data

## Correlation contract, version 1

### Log destinations

- `runtime/logs/sms_tool.log`: Python operator log. Human-readable, sanitized,
  rotated; use for registration, liveness, promotion, mailbox and one-click SMS
  stage messages.
- `runtime/logs/sms_tool.jsonl`: Python machine log. One sanitized JSON record
  per event with `command_id`, `run_id`, `account_ref`, stage and failure class.
- `runtime/app_*.log`: WPF host lifecycle and backend-process correlation log.
  It records task start/exit, command ids and UI-side diagnostics, but not the
  full Python event stream.
- `runtime/ui_errors.log`: WPF crash-only compatibility log. It is reserved for
  unhandled dispatcher/domain/task exceptions and is sanitized before writing.

The WPF panel is an ephemeral operator view, not an audit store. It folds JSON,
shows stage summaries, and keeps a bounded in-memory buffer. For post-run
diagnosis, use the two Python files plus `runtime/app_*.log` and correlate on
`command_id`/`run_id`.

Python file logs rotate under `runtime/logs/` in two channels:

- `sms_tool.log` — the operator log, one normalized line per record:
  `HH:MM:SS [*] [模块] 消息 · account_ref=<hash>`. Level markers are
  `[*] [!] [x] [.]`, logger names map to module labels (注册 / 浏览器注册 /
  账号测活 / 优惠检测 / 一键接码 / 邮箱 / 代理…), and registration stage
  records render as `阶段 · 中文名 (code) — 状态`. The trailing `account_ref`
  is the same 16-hex-character SHA-256 prefix carried by `sms_tool.jsonl` and
  the WPF `[后端进度]` lines, so a single account can be grepped across all
  three channels; the suffix is omitted for records that have no account.
  Envelope metadata and raw JSON never appear here.
- `sms_tool.jsonl` — the machine log, rotating JSON lines with `schema_version`,
  `source`, `command_id`, `task_name`, `run_id`, UTC `timestamp`, `level`,
  `logger`, and sanitized `message` for tooling and audits.

Both channels are opened by `configure_logging` through
`ResilientRotatingFileHandler`, which degrades a failed rotation to plain
appends instead of losing records. `RotatingFileHandler.emit` hands a rotation
error to `logging.Handler.handleError`, which only writes to stderr and then
**discards the record**; on Windows a log file held by another process cannot be
renamed, so the file stays above `maxBytes` and every later record fails — the
channel stops writing silently while the other keeps going. The resilient
handler swallows the `OSError`, warns **once per failure streak** through
logging (not stderr, which the desktop host does not capture), and retries the
rotation every 200 records. Each channel is opened in its own `try`, so one
unopenable file cannot silence the other.

`configure_logging` also routes the Python `warnings` module through logging
(`captureWarnings`), so library warnings render as `[!] [告警] <Category>: <text>`
in the operator log instead of raw `path:line:` stderr noise that would bypass
the formatter and leak into the WPF output panel.

WPF assigns `SMS_TOOL_COMMAND_ID` per backend process and logs `command_id` on
start/exit. Each registration creates a separate `run_id`; progress records and
Python logs carry both IDs. WPF remains text-formatted and correlates through
`command_id`, rather than pretending every legacy producer shares a JSON schema.

Registration JSONL rows also include driver, batch, attempt, failure class and
retryability. `proxy_pool_index=-1` means no pool index was supplied. Explicit
`source=test` rows are excluded by `registration_quality_metrics`; legacy rows
without a source remain ambiguous and need review before business reporting.

The registration `run_id` is bound before the first `started` event. Stage
duration is settled when leaving the current stage. `completed` is a **stage
name, not a verdict**: `RegistrationStateMachine.transition(COMPLETED)` does not
emit a stage event, and the terminal outcome of a run is published only by
`registration_progress.persist()`. A run can therefore reach the `completed`
stage and still end as `failed`, so an operator line reading "完成 — 成功" must
never be derived from the transition alone. Account identifiers in progress and
IPC records use stable hashes; operator-facing email labels are masked.

`runtime/registration_progress.jsonl` rotates by size under a cross-process
write lock. Quality metrics read both the active ledger and its rotated backup,
so rotation does not create an artificial reporting gap. Persisted text and
JSON logs apply log-specific email masking in addition to credential
sanitization.

Tests set `SMS_TOOL_EVENT_SOURCE=test` and redirect runtime paths to temporary
storage. Runtime paths hardcoded in older fixtures are not automatically
redirected by this environment variable.

Email references in the local progress ledger are still operational identity
data. Do not publish raw logs or ledgers. The count-only inventory below emits
neither identities nor credential values.

## Read-only inventory

```powershell
python scripts/registration_inventory.py
```

Reports Gmail/ReMail records matching the explicit root mailbox-pool import
format and runtime root file counts/sizes by category. It does not verify
credentials, query providers, inspect registration history, discover other
configured mailbox files or create accounts. It does not recurse into browser
profiles or modify files.

`review_debug_artifacts` means **review**, not safe-to-delete. Database backups,
session data, provider state, logs and OTP artifacts may contain the only
recovery evidence. Inventory never moves or deletes anything.

## Live comparison prerequisites

1. Supply authorized Gmail and ReMail samples through ignored local input.
2. Verify provider credentials separately; never print them in the report.
3. Confirm sample size, any provider spending cap and stop conditions.
4. Record driver/config/version and observation windows, including post-create
   liveness at agreed times. Separate registration failure from later revocation.
5. Stop at human verification or invalid credentials. Do not substitute new
   providers, purchase mailboxes or retry indefinitely.

No live comparison was run during this implementation. The default root pool
had no explicit Gmail/ReMail candidates at inventory time; other configured
sources were not inferred or contacted.

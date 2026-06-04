# Directory Map

This file classifies the repository by responsibility. It is intentionally about
physical placement; `docs/architecture.md` defines the behavioral boundaries.

## Top-level source directories

| Path | Classification | Owner / responsibility | Notes |
| --- | --- | --- | --- |
| `sms_tool/` | Python application core | CLI orchestration, mailbox handling, registration, payment links, payment adapters, storage, account scans | Keep command-specific imports lazy in `sms_tool.cli`. |
| `SmsWorkbench/` | Desktop UI | WPF launcher, account grid, dialogs, local command execution, desktop publish scripts | UI starts CLI commands; protocol/business logic stays in `sms_tool`. |
| `services/` | Local provider services | Optional GoPay, ADB, and mailbox helper services used by CLI/UI | Services expose explicit process/API boundaries and should not write account SQLite directly. |
| `tests/` | Offline verification | Unit tests for module seams and persistence semantics | Live vendor/browser tests must be opt-in. |
| `docs/` | Source-owned documentation | Architecture, boundaries, directory map, and operating notes | Do not place runtime logs or screenshots here unless deliberately curated. |
| `scripts/` | Operator scripts | Small launch/setup helpers that call source modules or local services | Keep scripts idempotent and repository-relative. |

## Root-level files

| Path | Classification | Owner / responsibility |
| --- | --- | --- |
| `chatgpt_phone_reg.py` | Compatibility entrypoint | Delegates to `sms_tool.cli`; no business logic should be added here. |
| `config.example.json` | Portable config template | Safe defaults and placeholders only. |
| `requirements.txt` | Python dependency manifest | Single committed Python dependency source. |
| `README.md` | Operator quick start | Setup, mailbox formats, common commands, and high-level module list. |
| `PROXY_GUIDE.md` | Proxy operation guide | Local proxy/stage-proxy setup; no machine-specific secrets. |
| `pytest.ini` | Test discovery compatibility | Keep even though the supported command is `python -m unittest`. |

## Runtime and generated directories

These directories are runtime state and are ignored by Git:

| Path | Contents | Rule |
| --- | --- | --- |
| `sessions/` | Generated `session_*.json` account/session files | Never commit; may contain tokens/cookies. |
| `runtime/` | SQLite index, caches, logs, debug output | Never commit; summarize redacted state only. |
| `dist/` | Published WPF executable | Rebuild with `SmsWorkbench/build_dotnet.ps1`; do not commit. |
| `.dotnet/` | Local bundled/runtime SDK | Local machine dependency; do not commit. |
| `__pycache__/`, `*.pyc` | Python bytecode | Delete or ignore. |

## `sms_tool/` module groups

| Group | Files | Boundary |
| --- | --- | --- |
| Entrypoints/config | `__main__.py`, `cli.py`, `config.py`, `paths.py` | Parse commands and resolve config/paths; no vendor protocol implementation. |
| Mailbox and phone inventory | `mailbox.py`, `providers/`, `smsbower.py`, `nextsms.py`, `phone_reuse.py` | Acquire/poll mailboxes or phone activations; no account persistence except through explicit callers. |
| Registration/auth | `registration.py`, `codex_oauth.py`, `codex_sentinel.py`, `codex_phone.py`, `session_refresh.py` | ChatGPT/OpenAI auth, OTP, session refresh, optional phone verification. |
| Payment links | `gen_pp_link.py`, `paypal_links.py` | Create/store hosted payment links from account access tokens; no PayPal account signup. |
| Payment execution | `paypal_browser_auto.py`, `paypal_auto.py`, `paypal_nocard.py`, `gopay_payment.py`, `gopay_wa_rebind.py`, `grpcurl_client.py` | Execute explicit payment commands only; use account seed and storage seams. |
| Account data/import/export | `account_seed.py`, `storage.py`, `codex_export.py`, `cpa_import.py`, `cpa_401_reimport.py`, `sub2api_import.py`, `import_targets.py`, `account_scan.py` | Normalize account/session state and external import/export payloads. |
| Shared utilities | `http_client.py`, `captcha_solver.py`, `nodriver_*`, `proxy_pool.py`, `utils.py` | Reusable transport/browser/helper logic with minimal state ownership. |

## `services/` module groups

| Path | Boundary |
| --- | --- |
| `services/gopay-flow/` | Local GoPay PaymentService and pure-protocol payment/signup implementation. |
| `services/gopay-app/` | GoPay App gRPC implementation/contract used by WA rebind mode. |
| `services/gopay-adb/` | ADB/notification sidecar for local emulator integration. |
| `services/mail-otp-web/` | Standalone Microsoft Graph inbox/OTP helper UI; operator diagnostic service, not the main registration mailbox owner. |

## Placement rules for new work

1. If it is a CLI command, add a lazy handler in `sms_tool.cli` and put the
   implementation in a focused module under `sms_tool/`.
2. If it is a desktop button/dialog, put UI code in `SmsWorkbench/` and call the
   CLI/backend rather than duplicating protocol logic in C#.
3. If it talks to a provider, isolate it under `sms_tool/providers/` or
   `services/<provider>/` and expose a small public method.
4. If it persists account state, route through `sms_tool.storage` or a documented
   storage seam.
5. If it is runtime output, put it under `runtime/` or `sessions/`, not in source
   directories.

# Project Architecture and Boundaries

This document defines the responsibilities of each module so a fresh clone can be configured and run on any Windows machine without hardcoded local paths. For the physical repository classification, see [`directory-map.md`](directory-map.md).

## Runtime Flow

```text
WPF or CLI
  -> mailbox source selection
  -> ChatGPT email registration
  -> auth session/access token fetch and stable HTTP-200 AT persistence boundary
  -> JIT AT probe/OAuth refresh when payment starts
  -> unified PayPal/GoPay/GCash/GrabPay/UPI/iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo link extraction
  -> session JSON + SQLite index
  -> status display and maintenance actions
```

## Email Change Flow

邮箱换绑是独立于注册、支付和普通测活的账号维护流程：

```text
WPF selected rows / CLI --email-file
  -> commands/email_change.py (argument adapter)
  -> account_email_change.py (provider allocation + eligibility/begin/OTP/verify)
  -> passwordless relogin without early persistence
  -> account liveness probe (HTTP 200 boundary)
  -> storage.migrate_account_email (destination conflict check + session/SQLite migration)
```

`ChangeEmailDialogService.cs` 只负责桌面输入，`MainWindow.ContextMenu.cs` 只负责选中行和任务生命周期；provider API、OTP 轮询、重新登录和持久化不得回流到 WPF code-behind。ReMail、CFWorker、Smailr 由 provider allocator 生成目标邮箱；iCloud、Outlook、Hotmail 必须从凭证池按账号数消费。

## Repository Layout

```text
chatgpt_phone_reg.py        Compatibility entrypoint; delegates to sms_tool.cli.
config.example.json         Portable config template. Copy to config.json locally.
config_schema.json          Cross-language config ownership manifest.
ipc_schema.json             Resident desktop-read protocol manifest.
README.md                   Setup and operations guide.
requirements.txt            Only Python dependency manifest.
start_proxy_pool.py         Standalone SOCKS5 proxy-pool server entrypoint.
verify_proxy.py             Standalone proxy configuration verification utility.
sms_tool/
  __main__.py               `python -m sms_tool` entrypoint; no import-time side effects.
  cli.py                    CLI parsing, high-level orchestration, process exit codes.
  config.py                 Deterministic immutable runtime config and preflight schema validation.
  sanitizer.py              Shared-policy redaction for text, structured output, IPC, and reports.
  account_models.py         Typed account/session persistence model and safe snapshot contract.
  paths.py                  Project-relative path resolution.
  account_seed.py           Shared account/session seed lookup and access-token extraction.
  mailbox.py                Mailbox provider routing and OTP retrieval compatibility seam.
  mailbox_service.py        Config-injected mailbox fetch/poll application service.
  mailbox_strategies.py     Typed provider adapter protocol and immutable provider registry.
  mailbox_types.py          Shared mailbox dataclass and type definitions.
  mailbox_parsers.py        Mailbox import format parsing.
  providers/                All mailbox provider implementations and low-level provider clients.
    mailbox_remail.py       ReMail order, pickup, adaptive OTP polling, and message normalization.
    mailbox_smailr.py       Smailr mailbox creation/reuse and OTP polling facade.
    mailbox_cfworker.py     CFWorker domain mailbox creation/fetch/OTP polling.
    mailbox_graph.py        Microsoft OAuth refresh boundary.
    mailbox_gmail.py        Gmail IMAP receive + SMTP send adapter.
    mailbox_icloud_url.py   Per-account iCloud OTP URL receive adapter.
    outlook_imap.py         Outlook IMAP folder discovery and message normalization.
  mail_otp.py               Shared OTP extraction/candidate filtering.
  commands/                 CLI subcommand helpers; no package-level __all__.
    helpers.py              Shared command-level utilities.
    payment.py              Protocol-payment argument adaptation and exit-code boundary.
    payment_links.py        Link generation, UPI, and explicit payment-execution commands.
    registration.py         Registration preflight, batch launch, and result adaptation.
    accounts.py             Account maintenance/import/export command adaptation.
    mailbox_ops.py          Inbox view and Gmail send command adaptation.
    one_click.py            One-click SMS and account-scan command adaptation.
docs/
  CONTEXT.md                Domain vocabulary and ownership rules.
  adr/                      Accepted architecture decisions.
  current/                  Current-state documentation entry points.
  audits/                   Historical audit interpretation and snapshots.
    omakse.py               Omakase command adaptation.
    email_change.py         Email-change argument adaptation.
  http_client.py            curl_cffi retry/transport handling.
  registration.py           Public registration facade and compatibility exports only.
  registration_state.py     Immutable input context, ordered state machine, and common stage deadline.
  registration_handlers.py Typed runtime state plus independent registration stage handlers and cleanup.
  registration_progress.py  Registration stage progress tracking and persistence.
  registration_concurrency.py Registration stage resource gates and wait metrics.
  cross_process_gate.py     OS file-lock slots shared by concurrent desktop/CLI processes.
  auth_flow.py              OpenAI signin/authorize/continue helpers.
  auth_headers.py           Auth header construction and normalization.
  account_creation.py       Account creation and auth-session fetch.
  batch_runner.py           Batch registration concurrency and result ordering.
  sentinel_tokens.py        Sentinel token extraction/cache/browser fallback.
  sentinel_quickjs.py       QuickJS SDK path and PoW fallback for Sentinel.
  otp_strategy.py           Registration OTP send/resend strategy.
  auth_state.py             client_auth_session_dump diagnostics.
  error_classification.py   Error type classification and normalization for retry/reporting.
  paypal_protocol.py        Shared PayPal protocol helpers (BA token extraction, Stripe redirect follower).
  paypal_proxy.py           PayPal stage proxy resolution and region rotation.
  paypal_reverse.py         PayPal reverse-engineering helpers for link extraction.
  k12_client.py             Legacy Workspace request/accept/leave adapter (explicit Python use only).
  k12_identity.py           Legacy Workspace identity extraction helper.
  workspace_scan.py         Legacy Workspace health check adapter; CLI scanning keeps it disabled.
  gen_pp_link.py            PayPal/Stripe payment-link generation. PayPal supports hosted long URL and PP direct approve URL; UPI uses a hosted link variant.
  checkout_contract.py      Canonical ChatGPT Checkout/Stripe init request and response contracts.
  payment_capability.py     Checkout + Stripe init capability probe; no PM creation or confirm.
  payment_catalog.py        Versioned shared payment-method catalog loader and alias normalization.
  payment_adapters.py       Typed adapter protocol and complete registry validation.
  payment_flow.py           Canonical payment stages and per-method flow profiles.
  payment_routing.py        Named proxy pools, stage routes, one-time selection, and redacted plans.
  payment_executor.py       Common payment execution state machine and terminal-result normalization.
  payment_link_manager.py   49-line compatibility shim; re-exports sms_tool.pay_link.
  payment_egress.py         Pre-side-effect proxy-country assertions with bounded caching.
  wallet_provider.py        Shared GoPay/GrabPay orchestration and structured outcomes.
  wallet_transport.py       GoPay/GrabPay HTTP transport, stage proxies, Stripe metadata, and redirect validation.
  gcash_provider.py         GCash custom-payment-method orchestration and structured outcomes.
  gcash_transport.py        GCash-specific Checkout update and custom-method HTTP transport.
  paypal_reconciliation.py  Independent, secret-free PayPal merchant-return reconciliation.
  payment_reconciliation.py Method-neutral reconciliation facade and unknown-result contract.
  paypal_authorization_queue.py Durable PayPal-only BA follow-up authorization queue.
  paypal/                   Project-local PayPal browser automation package (7 layers, see PayPal Payment Layer).
  paypal_auto.py            Compatibility shim re-exporting sms_tool.paypal (28 lines).
  paypal_link/              PayPal/Stripe/direct-card/UPI link generation and PayPal return
                            reconciliation; internal cohesion 0.00 (see Subpackage Structure).
  pay_link/                 Payment-link extraction state machine behind the
                            payment_link_manager.py shim (see Subpackage Structure).
  registration_drivers/     Browser registration drivers; highest fan-in in the package
                            (see Subpackage Structure).
  sentinel/                 OpenAI Sentinel token generation plus a vendored Node runner;
                            note sms_tool/sentinel/runtime/ is source, not build output.
  store/                    SQLite and session index persistence behind the 8-line
                            storage.py shim (see Subpackage Structure).
  nodriver_captcha.py       Nodriver-based CAPTCHA solver adapter.
  nodriver_paypal.py        Nodriver-based PayPal browser automation helper.
  captcha_solver.py         CAPTCHA solving abstraction.
  omakse_client.py          Omakase provider client adapter.
  phone_proxy.py            Phone verification proxy resolution.
  phone_reuse.py            Phone number reuse and inventory management.
  smsbower.py               SMSBower SMS provider activation and polling.
  sms_provider.py           SMS provider abstraction layer.
  proxy_pool.py             SOCKS5 proxy pool server with health checking.
  session_refresh.py        Protocol and isolated-browser ChatGPT session acquisition.
  payment_auth.py           JIT payment AT probe, HTTP-401 recovery chain, and token telemetry.
  payment_batch.py          Resumable batch payment executor, eligibility matrix, canary, retries, and atomic reports.
  account_liveness.py       Side-effect-free account liveness classification and quota parsing.
  account_recovery.py       Ordered AT recovery, verified persistence, quota status, and deactivation handling.
  agent_identity.py         Explicit Agent Identity/SUB2API credential conversion; not part of registration.
  sub2api_import.py         SUB2API import boundary with multi-mode auth.
  codex_export.py           Build Codex/CPA-compatible token JSON from session data.
  session_converter.py      Multi-format session/account export conversion core.
  codex_oauth.py            Codex OAuth authorization-code + PKCE login orchestration.
  codex_sentinel.py         Sentinel/cache cookie helpers for auth.openai.com requests.
  codex_phone.py            Optional add-phone SMS verification boundary.
  cpa_import.py             CPA API upload boundary; imports AT-only JSON and uploads normalized CPA payloads.
  import_targets.py         Import target normalization helpers.
  account_scan.py           Account health/quoter scan adapter.
  storage.py                8-line compatibility shim; re-exports sms_tool.store.
  desktop_read.py           Sanitized read-contract handlers and session metadata caches.
  desktop_serve.py          Resident JSONL desktop read server.
  doctor.py                 Offline runtime, dependency, and configuration diagnostics.
  utils.py                  Shared utility helpers.

SmsWorkbench/               WPF desktop UI.
  BackendTaskCoordinator.cs  Backend process task lifecycle, cancellation, and error normalization.
  SensitiveDataSanitizer.cs  C# consumer of the repository sensitive-data policy.
  ProtocolPaymentExecution.cs  Deterministic command planning and backend-result presentation.
services/
  protocol-payment/         Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo protocol extractors; wallets stay in sms_tool.
  mail-otp-web/             Standalone Microsoft Graph inbox/OTP diagnostic UI.
tests/                      Offline unit tests; see tests/README.md.
sessions/                   Generated session JSON, ignored by Git.
runtime/                    SQLite, debug output, caches, ignored by Git.
```

## Ownership Matrix

| Feature surface | Owning module | May call | Must not own |
| --- | --- | --- | --- |
| Desktop buttons/dialogs | `SmsWorkbench/` | `chatgpt_phone_reg.py`, SQLite/session read-only display helpers, read-only SMSBower catalog lookup | ChatGPT activation lifecycle, payment protocol, mailbox polling loops |
| CLI command routing | `sms_tool.cli` | Focused command modules | Provider protocol internals, payment workflow implementation, or long-lived state mutation outside handlers |
| Payment command adaptation | `sms_tool.commands.payment` | payment-link and batch public contracts, config and command helper seams | provider wire protocol, WPF state, account persistence implementation |
| Desktop payment command planning | `SmsWorkbench.ProtocolPaymentExecution*` | immutable request/view models and Python backend command names | WPF control access, provider protocol, process-global mutable state |
| Mailbox parsing/polling | `sms_tool.mailbox`, `sms_tool.providers/*` | Microsoft Graph, Gmail IMAP/SMTP, mailbox provider clients | Registration success persistence, payment state |
| Phone inventory | `sms_tool.phone_reuse`, `sms_tool.smsbower` | SMS provider APIs | ChatGPT account state, payment state |
| ChatGPT registration | `sms_tool.registration_handlers`, `sms_tool.registration_state`, `sms_tool.registration_concurrency` | immutable runtime config, mailbox/phone seams, stage resource gates, storage through result writers | Payment execution, CPA upload, process-current-directory config lookup |
| Auth/session refresh | `sms_tool.codex_oauth`, `sms_tool.session_refresh` | mailbox OTP seam, phone seam when explicitly enabled | Phone inventory purchasing outside configured provider seam |
| Account liveness | `sms_tool.account_liveness` | account seed data, `/backend-api/wham/usage` | Persistence, OAuth relogin, payment creation |
| Account recovery | `sms_tool.account_recovery` | account liveness, Codex OAuth/session refresh, storage | CPA API calls, payment creation |
| JIT payment authentication | `sms_tool.payment_auth` | account seed, account liveness/recovery | Registration success classification, payment-method creation |
| Payment link generation | `payment_methods.json`, `sms_tool.payment_catalog`, `sms_tool.payment_adapters`, `sms_tool.payment_link_manager`, `sms_tool.gen_pp_link`, `sms_tool.wallet_provider`, `sms_tool.wallet_transport` | immutable runtime config, account seed, shared Checkout contract, Stripe init, protocol adapters | duplicate method/alias registries, PayPal account signup, final customer authorization |
| Payment flow vocabulary | `sms_tool.payment_flow` | canonical stages and per-method profiles | proxy selection, provider HTTP |
| Payment route planning | `sms_tool.payment_routing` | method config, named pools, stage policy | registration/mailbox proxies, provider execution |
| Payment execution state | `sms_tool.payment_executor` | immutable request, adapter result, terminal state | CLI parsing, persistence, provider protocol details |
| Protocol extractor terminal reporting | `services/protocol-payment/common/protocol_core.py` | method result payload, shared sensitive-data policy | route planning, account persistence, provider orchestration |
| Checkout capability probing | `sms_tool.checkout_contract`, `sms_tool.payment_capability` | ChatGPT Checkout, Stripe init, matrix-selected country/proxy context | payment-method creation, confirm, approve, provider redirect |
| Batch payment execution | `sms_tool.payment_batch` | JIT auth, capability probe/payment manager, eligibility matrix, proxy stages, atomic reports | Registration/mailbox procurement, token-bearing public reports |
| PayPal return reconciliation | `sms_tool.paypal_reconciliation` | caller-supplied authenticated transport, allowlisted merchant return hosts | payment-link extraction, link persistence, payment authorization |
| Payment reconciliation dispatch | `sms_tool.payment_reconciliation` | catalog reconciliation policy and method-owned reconciler | link generation, automatic retry of unknown side effects |
| PayPal BA authorization queue | `sms_tool.paypal_authorization_queue` | completed PayPal BA extraction artifacts and explicit authorization handlers | non-PayPal methods, inline authorization during extraction |
| Payment execution | `sms_tool.paypal` (shim: `sms_tool.paypal_auto`) | account seed, saved payment links, provider services | Registration, mailbox pool edits, link regeneration as a side effect |
| Explicit Agent Identity conversion | `sms_tool.agent_identity` | account seed, Ed25519 key gen, storage | Registration flow, payment execution |
| SUB2API import | `sms_tool.sub2api_import` | agent identity, session converter, SUB2API API | Registration, payment, mailbox polling |
| Account import/export conversion | `sms_tool.session_converter`, `sms_tool.codex_export`, `sms_tool.cpa_import`, `sms_tool.sub2api_import` | session JSON, account seed, CPA/SUB2API API | Registration or payment execution |
| Account persistence | `sms_tool.store` (shim: `sms_tool.storage`) | session JSON and SQLite | Vendor protocol calls |
| Backend task lifecycle | `SmsWorkbench.BackendTaskCoordinator` | `PythonBackendClient`, cancellation, sanitized error/result normalization | WPF control state or command argument construction |
| Local helper services | `services/*` | Their own provider/runtime APIs | Direct account SQLite writes unless routed through CLI contracts |

## Dependency Direction

Dependencies flow inward from entrypoints and UI toward explicit application
contracts, then to provider and persistence adapters:

```text
WPF controls -> command planner -> Python CLI -> command adapter
                                      |              |
                                      v              v
                              application workflow -> provider/persistence adapter
```

- `MainWindow.*` owns control events only. Deterministic argument construction
  and backend JSON interpretation belong in `ProtocolPaymentExecution.cs` or a
  similarly focused, window-independent component.
- `sms_tool.cli` owns parser composition and compatibility wrappers. Payment
  selection and matrix/Canary adaptation belong in `sms_tool.commands.payment`.
- Command adapters call public workflow functions. They must not import private
  provider helpers, duplicate Checkout payloads, or write SQLite/session files.
- Provider adapters depend on shared contracts such as `checkout_contract` and
  result models; shared contracts do not import a concrete provider.
- Tests follow the same ownership: owner-module tests cover behavior, while
  routing tests cover only the boundary between two modules.

## Subpackage Structure（2026-08-31 机械拆分）

2026-08-31 的机械拆分把 `sms_tool/` 下的单体模块拆成 8 个子包。本节记录拆后的**实测状态**，
是 `## Repository Layout` 中目录条的展开。拆分的执行方式是"按行号切片 + 复制 import 前奏 + 加兼容壳"，
函数体未改动（`sms_tool/pay_link/base.py:1` 等 9 个子模块 docstring 自带
`mechanical split, bodies unchanged` 指纹），因此**目录结构变了，依赖结构基本没变**——
读本节时请把"有几个文件"和"有几条边界"分开看。

### 度量口径

除 providers 行外，数字为 2026-09-02 用 AST 全量扫描实测；providers 行于 2026-09-04 迁移后更新。
排除 `dist/`、`runtime/`、`.venv/`、`__pycache__/`、`sessions/`、`**/bin`、`**/obj`
（不排除则 `.py` 数翻 5 倍，结论不可用）。

| 指标 | 定义 |
| --- | --- |
| 入边 / 外部来源 | 指向该子包的依赖边数 / 其中来自子包外部（生产 + 测试）的模块数 |
| 出边 | 该子包指向 `sms_tool` 其它模块的依赖边数（不含 stdlib / 三方库） |
| 内部边 | 该子包内部子模块之间的依赖边数 |
| 内聚 | 内部边 ÷（内部边 + 出边）。**只衡量"对外依赖占比"，不衡量职责是否单一** |

### 总览

| 子包 | 文件 | 行数 | 入边 | 外部来源 | 出边 | 内部边 | 内聚 | 一句话定位 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `store/` | 7 | 1615 | 12 | 2 | 29 | 9 | 0.24 | 持久化核心，但被 8 行壳完全遮蔽 |
| `pay_link/` | 7 | 1762 | 12 | **1** | 87 | 10 | 0.10 | 出边最宽、外部来源最窄的哑铃 |
| `paypal/` | 8 | 2122 | 23 | 6 | 32 | 14 | **0.30** | 内部分层最规整，但有一个 P0 断链 |
| `paypal_link/` | 3 | 2768 | 4 | 2 | 12 | **0** | **0.00** | 两个互不相认的大文件共用一个目录 |
| `registration_drivers/` | 9 | 3508 | **45** | **14** | 36 | 11 | 0.23 | 全仓扇入最高，实现高度集中在 1 个文件 |
| `sentinel/` | 4 | 689 | 17 | 8 | **9** | 3 | 0.25 | 出口最干净，最容易独立 |
| `providers/` | 11 | 3770 | 由 mailbox seam 使用 | 统一邮箱 provider 实现 | — | — | — | 全部 provider 适配器集中目录 |
| `commands/` | 10 | 2646 | 18 | 4 | 78 | 5 | **0.06** | 编排层，低内聚是设计意图 |

### sms_tool/store/

**职责边界。** 拥有 SQLite schema 创建与迁移、账号邮箱的归一化与大小写不敏感去重、
注册/支付状态的列标记、注册检查点、以及从 `sessions/session_*.json` 重建 SQLite。

不负责：session JSON 的字段语义（归 `account_models.py`）、账号是否该删的业务判定
（归 `account_cleanup.py` / `account_liveness.py`）、任何网络调用。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `accounts.py` | 483 | upsert / 查询 / 邮箱迁移 |
| `normalize.py` | 397 | 邮箱与字段归一化 |
| `connection.py` | 269 | 连接、schema 初始化、路径解析 |
| `markers.py` | 268 | 配额 / 健康 / 促销状态标记 |
| `__init__.py` | 79 | 聚合出口 |
| `checkpoints.py` | 72 | 注册检查点 |
| `constants.py` | 47 | `EMAIL_RE` / `EXTRA_COLUMNS` / `KNOWN_EMAIL_DOMAINS` |

**对外接口。** `__init__.py:79` 的 `__all__` 共 52 个符号：25 个公共
（`database_path`、`init_database`、`upsert_account`、`migrate_account_email`、
`rebuild_from_session_dir` …）、27 个私有。**没有** stdlib 泄漏，是 4 个有 `__all__`
的子包里出口质量第二好的。

**依赖关系。** 内部三层：`constants` ← {`connection`, `normalize`} ← {`accounts`,
`checkpoints`, `markers`}，无环（内部边 9）。出边 29 全部指向 `sms_tool` 父包
（`config`、`paths`、`account_models`）。

入边只有 12，**包外直接引用 `sms_tool.store.*` 的仅 2 处**：`sms_tool/storage.py`
（兼容壳）与 `tests/test_store_modules.py`。真正的扇入是 **25 个文件（19 生产 + 6 测试），
且 100% 经过 `storage.py` 壳**——`account_seed.py`、`cli.py`、`registration_state.py`、
`desktop_read.py` 等 19 个生产模块全部写 `from sms_tool import storage`。

**已知的债。**

1. ⚠️ **故意的反向依赖，不要清理。** `store/connection.py:40` 有
   `DELIBERATE REVERSE DEPENDENCY - do not "clean this up"` 注释，`:57` 与 `:89`
   两处 `import sms_tool.storage as _storage` 反向取 `database_path`。原因：测试通过
   `patch.object(storage, "database_path")` 注入临时库，每次调用重新读属性才能让内部调用方
   也被重定向；改成绑定本地符号会让所有内部调用静默打到真实库。
   **两点修正**：(a) 该注释块目前只存在于**工作区**，尚未提交（`git diff sms_tool/store/connection.py`
   可见）；(b) 注释写"7 test files"，实测 **5 个**——`tests/test_account_health_queue.py:201`、
   `test_account_models.py:38,70,98`、`test_registration_checkpoint.py:31`、
   `test_store_modules.py:84,99,125,159`、`test_storage_dedup.py:22`。
2. 3 处函数级反向 import 破环：`accounts.py:143`（`..mailbox_remail`）、
   `markers.py:109`（`..account_health`）、`normalize.py:139`（`..payment_link_manager`）。
   其中 `store → payment_link_manager → pay_link → … → storage` 是真实的潜在环。
3. 壳比想象中薄：`storage.py` 只有 **8 行**（`from .store import *` + `__all__` 转发）。
   （49 行的壳是 `payment_link_manager.py`，两者常被记混。）壳越薄，"谁在依赖 store"就越难
   从 import 图上看出来——上面那 25 个文件在系统里登记的是对 `storage` 的依赖。

### sms_tool/pay_link/

**职责边界。** 拥有协议支付链接抽取的统一状态机：适配器编排、子进程抽取器调用、结果归一化、
终端状态判定、运行历史脱敏落盘。

不负责：具体支付方式的 HTTP 协议（在 `services/protocol-payment/`）、PayPal 链接生成
（归 `paypal_link/`）、代理路由规划（归 `payment_routing.py`）、能力探测（归 `payment_capability.py`）。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `adapters.py` | 601 | 各支付方式适配器调用 |
| `registry.py` | 290 | 适配器注册表 |
| `core.py` | 250 | `generate_payment_link` / `probe_payment_method` |
| `normalize.py` | 247 | 结果与异常归一化 |
| `base.py` | 225 | 配置、路径、脱敏、锁 |
| `persistence.py` | 62 | 运行历史落盘 |
| `__init__.py` | 87 | 聚合出口 |

**对外接口。** `__all__` 55 个：15 个公共（`generate_payment_link`、`probe_payment_method`、
`build_default_payment_registry`、`register_payment_adapter`、`PAYMENT_ADAPTERS` …）、
40 个私有。无 stdlib 泄漏，但私有占比 73%，说明拆的是命名空间不是边界。

**依赖关系。** 内部方向：`base` ← `adapters` ← {`core`, `registry`}，另加
`core` ← {`normalize`, `persistence`}。出边 **87**（8 个子包里最宽）指向 22 个父包模块。
入边 12，**外部来源只有 1 个**：`sms_tool/payment_link_manager.py`（49 行壳，
`:48` `from .pay_link import *`）。这是全仓最典型的"哑铃"：对外只有 1 个入口，对内 87 条出边。

**已知的债。**

1. **6 个子模块的 import 前奏逐字重复。** `base`/`adapters`/`core`/`normalize`/
   `persistence`/`registry` 的**第 4–26 行完全相同**（10 个 stdlib import + 9 条 `..` 导入），
   真正的内部依赖从第 28 行才开始。这是机械拆分把单体的 import 块复制进每个子模块的直接后果，
   也是"出边 87"的主要来源（6 × ~14 条重复边）。**要降出边，先删这 6 × 23 行，不用改任何函数。**
2. 声称的 "base → adapters → core/registry" 分层**不完整**：`core.py:32` 依赖 `registry`，
   而 `registry.py:28` 又依赖 `adapters`——`adapters` 与 `core`/`registry` 之间互为可达。
   当前靠 `from X import a, b, c` 的逐符号导入没触发 `ImportError`，但已无严格分层。
3. 壳 `payment_link_manager.py` 第 8–45 行保留了 38 行前置 import
   （`json`/`logging`/`subprocess`/`tempfile`… 及 9 个 `sms_tool` 模块），在
   `from .pay_link import *` 之前执行。拆分残留，应先验证再删。

### sms_tool/paypal/

**职责边界。** 拥有 PayPal 浏览器自动化：策略选择（reverse 协议 → nodriver → Camoufox/Cloak）、
步骤机、表单语义、DOM 定位原语、会话与指纹、卡片/地址/号码挑选、结果持久化。

不负责：PayPal 链接生成（归 `paypal_link/`）、BA 后续授权队列（归 `paypal_authorization_queue.py`）、
商户回跳对账（归 `paypal_reconciliation.py`）。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `orchestrator.py` | 485 | 策略选择与结果持久化 |
| `form_steps.py` | 434 | 语义化表单字段 |
| `dom_fields.py` | 429 | 通用定位/填写/读取原语 |
| `flow_steps.py` | 355 | 步骤机 + 人机验证/短信闸门 |
| `__init__.py` | 155 | 聚合出口 |
| `session.py` | 142 | 浏览器上下文与指纹 |
| `config_picker.py` | 109 | 卡/地址/号码挑选 |
| `errors.py` | 13 | `_PayPalStepError` |

**对外接口。** `__all__` 56 个，其中 **55 个私有，唯一公共符号是 `auto_pay`**。
这是 8 个子包里出口最"内向"的——包内 7 层互相调用不需要对外暴露，但把 55 个 `_` 前缀符号
塞进 `__all__` 会让 `from sms_tool.paypal import *` 的意义失效。

**依赖关系。** 内部分层是 8 个子包里最规整的，严格单向：

```text
errors / dom_fields / session  ->  form_steps  ->  flow_steps  ->  orchestrator
                                       config_picker --^
```

出边 32 指向 17 个父包模块；入边 23，外部来源 6 个
（`codex_phone.py`、`paypal_auto.py` 壳、4 个测试文件）。

**已知的债。**

1. 🔴 **P0 断链（拆分引入，已实测复现）。** `orchestrator.py:233`
   `from .nodriver_paypal import run_nodriver_pay` 与 `:244`、`:311`
   `from .proxy_bridge import proxy_for_browser` ——两个模块都在**父级**
   （`sms_tool/nodriver_paypal.py`、`sms_tool/proxy_bridge.py`），单点相对导入解析成
   `sms_tool.paypal.nodriver_paypal`，运行时抛 `ModuleNotFoundError`。
   对照 `registration_drivers/external_sessions.py:357` 的 `from ..proxy_bridge import` 才是对的。
   调用点 `orchestrator.py:106` **没有 `try` 包裹**（AST 确认 `auto_pay` 行 30–157 内无任何
   `Try` 覆盖 106 行），所以 reverse 协议失败后走 nodriver 兜底时，异常直接穿透 `auto_pay`，
   `:122` 的 Camoufox/Cloak 兜底**永远到不了**。
   测试没抓到是因为 `tests/test_paypal_orchestrator.py:71` 把 `_try_nodriver_pay` 整个
   monkeypatch 掉了——被测函数本体从未被执行。修法：`..` 改两层 + 补一条不打桩的导入测试。
2. 10 处"到父壳 `paypal_auto.py`"的引用**不是 import 级反向依赖**。实测 `sms_tool/paypal/`
   内没有任何 `import paypal_auto`；那 10 处是 docstring 溯源句（`config_picker.py:3`、
   `dom_fields.py:3`、`errors.py:3` 等）与配置键/错误串（`orchestrator.py:44,46` 的
   `CFG["paypal_auto"]`、`config_picker.py:38`）。真正需要清理的是 `__all__` 里的 55 个私有符号，
   不是这些文本。
3. `dom_fields.py` 的静默失败沿用了单体时期的行为：选择器失配统一返回 `False`，
   PayPal 前端改版后上层会看到"填表成功"而实际未填（见
   `docs/audit-2026-09-02-round5-summary.md`）。拆分没有改变这一点。

### sms_tool/paypal_link/

**职责边界。** 拥有 PayPal / 直连卡 / UPI 三类链接的**生成**，以及 PayPal 商户回跳的**对账**
（跳转链还原、远程状态判定、证据收集）。

不负责：链接抽取的状态机（归 `pay_link/`）、BA 后续授权（归 `paypal_authorization_queue.py`）、
浏览器自动化（归 `paypal/`）。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `reconciliation.py` | 1305 | 商户回跳对账 |
| `gen_link.py` | 1255 | PayPal/Stripe/直连卡/UPI 链接生成 |
| `__init__.py` | 208 | 聚合出口 |

**对外接口。** `__all__` **166 个符号，8 个子包里最宽**：68 公共、78 私有、
**20 个 stdlib/内建名泄漏**（`json`、`os`、`sys`、`re`、`html`、`hashlib`、`Path`、`Any`、
`Mapping`、`Optional`、`Protocol`、`Sequence`、`Enum`、`dataclass`、`annotations`、`main`、
`urljoin`、`parse_qs`、`unquote`、`urlsplit`）。`from sms_tool.paypal_link import *`
会污染调用方命名空间。

**依赖关系。** **内聚 0.00——内部边为 0。** `gen_link.py` 与 `reconciliation.py` 互不引用，
只是被塞进同一目录（它们分别源自 `gen_pp_link.py` 与 `paypal_reconciliation.py` 两个不相干的单体）。
出边 12 指向 11 个父包模块；入边 4，外部来源 2 个（`gen_pp_link.py`、`paypal_reconciliation.py`，
两者现在都是壳）。

**已知的债。**

1. `_PlmProxy` 惰性代理是补丁不是设计。`gen_link.py:6-19` 定义 `_PlmProxy`，
   `:23` `_plm = _PlmProxy()`，属性访问时才 `importlib.import_module("sms_tool.gen_pp_link")`。
   全文件 **15 处 `_plm.*` 调用**（`:296, 328, 405, 494, 559, 570, 693, 968, 987, 1013, 1066,
   1085, 1115, 1141`）。存在理由：拆分后 `gen_pp_link` 变成壳，`gen_link` 直接 import 父壳会成环。
   代价是每次属性访问走一次 `sys.modules` 查找，且 monkeypatch 语义依赖"读实时属性"这一微妙假设。
2. `__all__` 的 20 个 stdlib 泄漏来自机械 `import *` 转发。修法不是删 `__all__`，
   而是把它收敛到 68 个公共符号——但这会打破现有 `from sms_tool.paypal_link import json` 式的
   隐式依赖，需先 grep 确认。
3. 内聚 0.00 意味着"这个子包"目前不是一个内聚单元。若要把 `paypal_link/` 当成一个模块来理解，
   先要决定它究竟是"链接生成"还是"回跳对账"——两者没有共享代码。

### sms_tool/registration_drivers/

**职责边界。** 拥有浏览器注册的驱动抽象与各驱动实现：驱动名归一化、浏览器上下文、
反指纹注入、外部反检测浏览器的 CDP 接入（Roxy / Cloak / Camoufox / AdsPower）、
Playwright 状态机。

不负责：注册流程的阶段编排（归 `registration_handlers.py` / `registration_state.py`）、
并发闸门（归 `registration_concurrency.py`）、注册结果持久化（归 `store/`）。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `playwright.py` | **2035** | 唯一真实实现 |
| `external_sessions.py` | 901 | 反检测浏览器 CDP 轨 |
| `browser_session.py` | 373 | 浏览器上下文 |
| `stealth.py` | 92 | 反指纹注入 |
| `base.py` | 59 | enum + 归一化 + `BrowserRegistrationError` |
| `roxy.py` / `camoufox.py` / `cloak.py` | 19 / 12 / 12 | 三行包装 |
| `__init__.py` | 5 | 出口 |

**对外接口。** `__all__` 只有 **3 个符号**：`BROWSER_REGISTRATION_DRIVERS`、
`RegistrationDriver`、`normalize_registration_driver`——**全部来自 `base.py`，
8 个实现模块一个都不导出**。这是"契约面"与"实现面"分离得最清楚的一个子包。

**依赖关系。** 入边 **45 / 外部来源 14（8 生产 + 6 测试）**，是 8 个子包里扇入最高的
（`cli.py`、`config.py`、`registration.py`、`batch_runner.py`、`browser_pool.py`、
`account_liveness.py`、`account_recovery.py`、`scripts/_diag_camoufox_launch.py`）。
出边 36 指向 25 个父包模块；内部边 11；内聚 0.23。

**已知的债。**

1. "5 个驱动"是错觉。`camoufox.py`(12)、`cloak.py`(12)、`roxy.py`(19) 都只是
   `run_browser_registration(driver_name=...)` 的一层包装；真实实现只有
   `playwright.py` **2035 行（占子包 58%）** 加 `external_sessions.py` 901 行（占 26%）。
   这两个文件也是本子包待拆的全部内容。
2. `ADSPOWER` **不是**"有 enum 没实现"。实测 AdsPower 实现完好：
   `AdsPowerBrowserSession` 在 `external_sessions.py:689`，分派在 `:881`，
   配置键在 `:78-82`，CLI choices 在 `cli.py:248`，校验白名单在 `config.py:448`，
   并有 `tests/test_adspower_driver.py`（217 行）覆盖别名归一化与会话分派。
   缺的只是"一个驱动一个文件"的对称性——它与 Roxy/Cloak/Camoufox 一起挤在
   `external_sessions.py` 里。
3. `BrowserRegistrationError`（`base.py:53`）是事实上的公共契约（`external_sessions.py`
   至少 6 处抛它），但不在 `__init__.py` 的 `__all__` 里，调用方只能
   `from sms_tool.registration_drivers.base import BrowserRegistrationError`。
4. 内聚 0.23 偏低的主因是 `external_sessions.py` 与 `playwright.py` 各自横向触达
   `config` / `proxy_bridge` / `browser_fingerprint_pool` 等父包模块，而非子模块之间互调。

### sms_tool/sentinel/

**职责边界。** 拥有 OpenAI Sentinel token 的生成：flow 页面顺序、PoW 计算、
vendored Node runner 的子进程调度、bundle 组装、token 缓存。

不负责：Sentinel 结果的使用方（注册/支付各自消费）、`oai-did` 等指纹 Cookie 的策略
（归 `codex_sentinel.py`）。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `client.py` | 407 | flow 编排 + token 缓存 |
| `runner.py` | 194 | Node 子进程调度 |
| `bundle.py` | 67 | bundle 组装 |
| `__init__.py` | 21 | 出口 |

另含 vendored 资产：`runtime/sdk.js`(30 KB)、`runtime/sentinel-runner.js`(57 KB)、
`THIRD_PARTY_NOTICES.md`(10 行)。

**对外接口。** `__all__` 7 个：`SentinelToken`、`SentinelIssueError`、`FLOW_PAGE_URLS`、
`sentinel_backend`、`issue_sentinel_token`、`issue_sentinel_flow`、`issue_sentinel_bundle`。
**8 个子包里唯一"私有符号 0、stdlib 泄漏 0"的干净出口。**

**依赖关系。** 出边 **9** 指向 7 个父包模块（8 个子包里最少，仅次于 `providers/` 的 3）；
内部边 3；内聚 0.25。入边 17，外部来源 8 个（`auth_flow.py`、`batch_runner.py`、
`phone_registration.py`、`registration_handlers.py`、`registration_preflight.py`、
`paypal_extract.py`、`sentinel_tokens.py`、`tests/test_sentinel_runner.py`）。

**已知的债。**

1. **统计陷阱**：`sms_tool/sentinel/runtime/` 会被全仓扫描的 `runtime/` 排除规则**误伤**，
   导致 vendored JS 资产不在统计内。任何对该目录的审计/打包都要显式放行这一路径。
2. 双入口未收敛：根级 `sentinel_tokens.py` 与 `sentinel/` 并存，前者导入后者并缓存结果，
   但 8 个外部来源里仍有直接走 `sentinel_tokens` 的。新代码应直接用 `sms_tool.sentinel`。
3. `runner.py` 走子进程调 Node，异常面在进程边界——AST 静态扫描看不到
   `sentinel-runner.js` 内部的失败模式，PoW 失败只能靠子进程退出码与 stderr 推断。

### sms_tool/providers/

**职责边界。** 统一拥有全部 active 邮箱 provider 的**客户端与适配器实现**（CFWorker、Smailr、ReMail、Gmail、Graph、iCloud URL、Outlook IMAP）。Chongzhi 已移除。

不负责：provider 的分配与轮询编排（归 `mailbox.py` / `mailbox_service.py` /
`mailbox_strategies.py`）、OTP 抽取（归 `mail_otp.py`）。

| 文件 | 行数 |
| --- | --- |
| `cfworker_mailbox.py` | 591 |
| `smailr_mailbox.py` | 421 |
| `mailbox_*.py` / `outlook_imap.py` | provider-specific adapters |
| `__init__.py` | package description and explicit module exports |

**对外接口。** provider registry 通过 `mailbox.py` / `mailbox_strategies.py` 暴露稳定 seam；
具体 provider 模块可直接导入。顶层 `mailbox_*.py` 文件只保留兼容 facade，不再承载实现。

**依赖关系。** provider 实现只依赖 shared mailbox types/parsers/OTP helpers 与 provider-local low-level clients；
不依赖 WPF，不直接写账号存储。兼容 facade 不应成为新代码的导入目标。

**已知的债。**

1. 迁移已完成。新 provider 必须放在 `sms_tool/providers/`；顶层兼容 facade 只用于旧调用者，
   不得添加新的业务逻辑。
2. 与 `mailbox_strategies.py` 的 provider registry 概念重叠：一个按"目录"组织，
   一个按"注册表"组织。迁移第 3 个 provider 之前必须先定这一个问题。
3. 无 `__all__` 意味着包边界完全靠约定；一旦 `providers/` 下文件增多，
   私有符号会像 `paypal_link/` 那样被 `import *` 无意带出。

### sms_tool/commands/

**职责边界。** 拥有 CLI 子命令的**编排与参数适配**：参数归一化、调用 workflow 公共函数、
退出码边界。

不负责：任何业务实现（支付协议、注册流程、持久化）、WPF 状态、直接读写 SQLite/session 文件
（见 `## Dependency Direction`）。

| 文件 | 行数 |
| --- | --- |
| `payment.py` | 780 |
| `registration.py` | 616 |
| `accounts.py` | 348 |
| `payment_links.py` | 263 |
| `one_click.py` | 187 |
| `mailbox_ops.py` | 145 |
| `omakse.py` | 130 |
| `helpers.py` | 123 |
| `email_change.py` | 53 |
| `__init__.py` | 1（仅 docstring，**无 `__all__`**） |

**对外接口。** 与 `providers/` 一样**没有包级出口**；`cli.py` 直接 import 具体子模块。
这是有意的——命令模块的调用方只有 `cli.py` 一个。

**依赖关系。** 出边 **78** 指向 33 个父包模块（子包里第二宽），内部边 5，
**内聚 0.06 为 8 个子包最低**。入边 18，外部来源 4 个（`cli.py` + 3 个测试）。

### Recent deepening seams (2026-09-04)

- `payment_batch_setup.py` owns method-owned stage-pool selection so the batch
  orchestrator does not also implement configuration precedence.
- `registration_drivers/browser_flow/context.py` owns browser driver settings,
  headless selection, URLs, locale/timezone, and OTP timeout preparation.
- `account_events.py` owns post-persistence facts; `store/` no longer imports a
  concrete mailbox provider for side effects.
- `config_schema.json` and `ipc_schema.json` are checked against both Python and
  C# protocol/config declarations by repository guard scripts.

**已知的债。**

1. **内聚 0.06 是设计意图，不是缺陷。** 编排层的价值就在于横向触达 33 个模块。
   评价它应该用"是否含业务逻辑"和"是否直接写库"两个指标，而不是内聚——
   拿内聚排名去推动 `commands/` 重构会拆错地方（真正该拆的是 `pay_link/` 的重复 import 前奏）。
2. `## Repository Layout` 的命令清单**漏了 2 个模块**：`email_change.py`(53) 与 `omakse.py`(130)。
   前者是邮箱换绑的参数适配层（见 `## Email Change Flow`），后者是 Omakase 命令适配。
3. 无 `__all__` + 5 条内部边（如 `accounts.py` ← `registration.py`）意味着子模块可互相引用
   任意私有符号，边界靠约定。当前规模（10 文件 / 2646 行）尚可接受，
   再增长应先补 `__all__`。

### 拆分遗留的共性问题

1. **没有任何一个子包是"叶子"。** 8 个子包的出边（29 / 87 / 32 / 12 / 36 / 9 / 3 / 78）
   **100% 指向 `sms_tool` 父包自身**，而 `sms_tool/__init__.py` 自身扇入 86。
   拆分降低了单文件长度，没有降低包间耦合——`sms_tool` 这个扁平命名空间仍是事实上的"全局作用域"。
2. **`__all__` 搬运的是命名空间，不是边界。** 4 个有 `__all__` 的子包合计 329 个符号，
   其中 200 个私有（61%）。`paypal/` 55/56 私有、`paypal_link/` 泄漏 20 个 stdlib 名。
   `from sms_tool.<pkg> import *` 在这 4 个包上都应视为未定义行为。
3. **机械拆分的指纹仍在。** 除 9 处 `mechanical split, bodies unchanged` docstring 外，
   `pay_link/` 6 个子模块第 4–26 行 import 前奏逐字相同（见上文）。删掉这 138 行是本轮
   投入产出比最高的清理。
4. **兼容壳的行数与职责都被误记。**    实测 `gen_pp_link.py` 7 行、`paypal_reconciliation.py` 7 行、`storage.py` 8 行、
   `paypal_auto.py` 28 行、`payment_link_manager.py` 49 行——**5 个壳合计 99 行**，
   形态统一为 `from .<pkg> import *` + `__all__` 转发
   （`payment_link_manager.py` 多带 38 行拆分残留的前置 import）。
   引用这些壳的文件数远多于引用子包本身的文件数（`store/` 是 25 : 2），
   因此**任何"谁在依赖 X 子包"的统计都必须把壳算进去，否则结论会差一个数量级**。

### 与 2026-09-02 快照的出入

以下条目与拆分时记录的快照不一致，本节一律采用实测值：

| 项 | 快照 | 实测 | 说明 |
| --- | --- | --- | --- |
| `storage.py` 行数 | 49 | **8** | 49 行的是 `payment_link_manager.py` |
| `store/` 扇入 | 26 | **25** 文件（19 生产 + 6 测试） | 口径为"文本引用 `sms_tool.storage`"，与 26 基本吻合；但**直接引用 `store.*` 的只有 2 个** |
| `connection.py` 注释中的测试数 | 7 | **5** | 见上文 store 债 1 |
| `pay_link/` 出边 | 179 | **87** | 179 含 stdlib/三方 import；87 为指向 `sms_tool` 的边 |
| `pay_link/` 内部方向 | base → adapters → core/registry | 同左 **+** `core` ← `registry` ← `adapters` | 存在可达环 |
| `pay_link/` 的 `_plm.*` 清理 | 24 处已清理 | **0 处残留** | `_plm` 实际在 `paypal_link/gen_link.py`，仍有 **15 处** |
| `paypal/` 到父壳反向依赖 | 10 条待清理 | **0 条 import 级** | 10 处均为 docstring 与配置键文本 |
| `registration_drivers/` ADSPOWER | enum 有、模块无 | 实现在 `external_sessions.py:689` | 测试 217 行覆盖 |
| `playwright.py` 行数 | 2032 | **2035** | — |
| 各子包内聚 | 0.89 / 0.93 / 0.35 / 0.46 / 0.16 | 0.30 / 0.00 / 0.23 / 0.25 / 0.06 | 口径不同：本节用"内部边 ÷(内部边+出边)" |

## services/protocol-payment/（vendored 抽取脚本群）

上一节的 8 个子包全在 `sms_tool/` 下、都是 2026-08-31 机械拆分的产物。本节的目录**不属于那次拆分**：
它在仓库根 `services/protocol-payment/`，不是 `sms_tool` 的子包，与 `sms_tool` 之间没有 import 级
耦合。它的边界不是模块边界，而是**进程边界**。

### 职责边界

拥有 7 种支付方式（iDEAL / PIX / Kakao Pay / BLIK / TWINT / 直卡 Checkout / MoMo）的
端到端 Checkout 协议抽取：拿 access token 与代理种子，跑完商家侧 Checkout 流程，把终端状态
打到 stdout。

不负责：抽取状态机与重试（归 `sms_tool/pay_link/`）、代理路由规划（归 `payment_routing.py`）、
能力探测（归 `payment_capability.py`）、PayPal 链接生成（归 `paypal_link/`）、以及任何
SQLite / session 写入——脚本群里没有任何一处直接碰账号库。

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `blik/blik_qr_extract.py` | 3792 | BLIK 抽取（复用 iDEAL 流程） |
| `ideal/ideal_qr_extract.py` | 3197 | iDEAL 抽取 |
| `twint/twint_extract.py` | 3184 | TWINT 抽取 |
| `momo/momo_qr_extract.py` | 2046 | MoMo 抽取 |
| `kakao/kakao_extract.py` | 1519 | Kakao Pay 抽取 |
| `direct_card/direct_card_extract.py` | 1174 | 直卡 Checkout 抽取 |
| `momo/ac_paylink_core.py` | 851 | MoMo 链接核心（被 `run_momo.py` 复用） |
| `pix/pix_core.py` | 839 | PIX 核心（被 `run_pix.py` 复用） |
| `pix/pix_extract.py` | 795 | PIX 抽取 |
| `common/protocol_core.py` | 315 | 共享终端状态上报与脱敏策略 |
| `momo/run_momo.py` | 156 | MoMo 独立入口（`--dump-*` 自检用） |
| `pix/run_pix.py` | 82 | PIX 独立入口 |
| `common/extractor_helpers.py` | 26 | `is_user_already_paid_error` 等共享判定 |
| `common/__init__.py` | 1 | 空 |

合计 **14 个 `.py` / 17977 行**——比上一节 8 个子包加起来（16023 行）还大，但此前在本文件中
只有 2 处间接提及（`## Repository Layout` 与 `## Ownership Matrix` 各 1 行）。体量与文档覆盖度
严重不匹配。

### 对外接口面（进程边界）

唯一契约是进程边界：**env 变量进，stdout 的最后一段 JSON 出**。

- 调用方：`sms_tool/pay_link/adapters.py:31` `_run_extractor_subprocess`，以
  `cwd=script.parent` 起子进程、超时归口 `error_stage: adapter_subprocess`（`:59-65`）。
- 结果解析：`adapters.py:165` 取 `_last_json_object(proc.stdout)`，并要求
  `schema == "protocol_payment.v1"` 才认（`:166-169`）。脚本可以自由往 stdout 打日志，
  只要最后一个 JSON 对象符合 schema——这是刻意的宽松约定。
- 调用点在 `adapters.py:160`、`:476`、`:586` 三处。

对 C# 前端**没有直接接口面**：WPF 只经 `SmsWorkbench.ProtocolPaymentExecution.cs` 拼
Python CLI 参数，不感知这些脚本。

### 依赖关系

与 `sms_tool` 零 import 级耦合；内部唯一的共享代码是 `common/` 包。**这是全仓唯一真正
满足"可独立出去"的子系统**（对比上一节：8 个子包的出边 100% 指回 `sms_tool` 父包）。

### 已知的坑

1. ⚠️ **`sys.path` 自注入 + 裸名 `import common` 是刻意的，不要"清理"。** 6 个脚本把自身
   目录（或其父目录）塞进 `sys.path`：`blik:84-85`、`ideal:74-75`、`twint:73-74`
   （`PROTOCOL_ROOT = SCRIPT_DIR.parent`）、`kakao:39-40`（`PROJECT_ROOT =
   SCRIPT_DIR.parents[2]`）、`momo/run_momo.py:24`、`pix/run_pix.py:11`。随后以裸名
   导入兄弟包：`blik:86-87`、`ideal:76-77`、`twint:75-76` 的
   `from common.protocol_core import ...`。
   存在理由：这些脚本以 `cwd=script.parent` 被当独立进程执行，`services/protocol-payment/`
   不在任何 `sys.path` 上，也不在 installed package 里。改成正规相对导入会让子进程直接
   `ModuleNotFoundError`。**要动这里，先改调用方的子进程启动方式，不能只改 import。**
   副作用：`common` 是个过于通用的顶层模块名，一旦仓库根被加进 `sys.path`
   （`kakao` 就是这么干的），理论上存在与其他 `common` 撞名的风险。
2. **env 契约全仓无一处声明。** `adapters.py:132-158` 是一条按 `spec.key` 的 4 分支 if 链，
   每种方式注入不同变量：`ideal` → `PP_TOKEN` + `IDEAL_PROXY_SEED_FILE` + `IDEAL_FLOW_MODE`；
   `kakao` → `KAKAO_TOKEN` + `KAKAO_PROXY_SEED_FILE` + 3 个 `*_COUNTRY`；
   `twint` → `PP_TOKEN` + `TWINT_PROXY_SEED_FILE` + `TWINT_FLOW_MODE`。
   脚本侧各自 `os.environ.get` 读取，两边没有共享的 schema 或默认值表。加一种支付方式要在
   两边同时对齐。**建议的收敛方向见 `docs/audit-2026-09-02-round5-decoupling.md` §4.4 的
   `ProtocolScriptAdapter`。**
3. ⚠️ **BLIK 复用 `IDEAL_` 前缀的 env 变量。** `adapters.py:156` 给 `blik` 注入的是
   `IDEAL_PROXY_SEED_FILE` / `IDEAL_FLOW_MODE`（外加 `IDEAL_BLIK_CODE`），并没有 `BLIK_` 前缀
   的变量。按 key 猜变量名会猜错——`blik` 走的是 iDEAL 的流程实现，这是有意的复用，不是笔误。
4. **`promo_mode` 4 分支链被复制了 3 份。** `blik:1711`、`ideal:1254`、`twint:1247`
   （`twint` 另有 `:1361` 一份）各自 `os.environ.get("PP_PROMO_MODE", "campaign")` 后走
   `trial` / `campaign` / `coupon` / `code` 四分支。默认值 `campaign` 在三处硬编码，
   改默认值要改 3 遍。
5. **运行时产物落在脚本目录内，靠 `.gitignore` 兜底。** 各脚本自管
   `logs/`、`dumps/`、`qr/`、`proxy_state.json`、`proxy_seeds.txt`、`token.txt`、
   `removed_proxies.jsonl`，由 `services/protocol-payment/.gitignore` 统一忽略。
   这意味着**子目录不是纯代码目录**，`ls` 看到的多余内容是上一次运行的残留，
   清理前先确认没有正在跑的任务。
6. `kakao/kakao_extract.py` 会**反向**碰 `sms_tool`（延迟导入 access-token liveness 探针）。
   它是脚本群里唯一这么做的，源码里有注释说明是刻意的：模块加载期硬 import `sms_tool`
   正是"看起来自包含、实际焊死在父包上"的伪拆分陷阱，所以改成运行时延迟导入。
   这条注释是本目录里唯一显式记录了架构意图的地方，值得作为后续改动的参照。

## Boundary Rules

### Runtime Configuration

`sms_tool.config` performs no import-time file I/O. CLI and desktop-launched CLI
processes parse and validate `config.json` once at startup into an immutable
`RuntimeConfig`. Application workflows accept that object explicitly and enter a
`ContextVar` scope so legacy leaf helpers see the same injected snapshot without
reading the current directory or reparsing configuration. `CFG` is compatibility
only: it is a dynamic view over the active scope for older integrations and must
not be used as mutable production state.

Registration, mailbox, storage, and payment composition boundaries accept a
`RuntimeConfig`. Validation runs before network or subprocess work and covers
workflow names, proxy pool shapes, timeouts, supported payment methods, and the
payment country matrix.

### Sensitive Information Policy

`sensitive_policy.json` is the single language-neutral policy. Python loads it
through `sms_tool.sanitizer`; WPF embeds and loads the same file through
`SensitiveDataSanitizer`. Token, TOTP, proxy credential, card, password, and
payment-secret values are fully replaced, never prefix-masked. Operator logs,
exceptions, IPC progress, subprocess stderr, JSONL, and batch reports must pass
through this policy before display or persistence. Structured backend stdout may
remain raw only inside the parser boundary and must be sanitized before display.

### Mailbox Provider Adapters

`MailboxProviderAdapter` defines provider matching, message fetch, and OTP poll
operations. `MailboxProviderRegistry` resolves immutable adapter instances, and
`MailboxService` composes a registry with an injected `RuntimeConfig` for one
workflow. Registration depends on `MailboxService`, not provider tuples or
module-global configuration. Compatibility registration functions remain only
for external adapters while they migrate to typed adapter objects.

### Registration Protocol Consistency

The CLI runs ChatGPT/Auth/Sentinel transport and browser-profile preflight before
claiming a paid or disposable mailbox. A selected route is promoted to the front
of the account proxy pool. Each account then keeps one proxy-bound HTTP session;
only classified network or auth-state retries create a fresh session and route.

`auth_headers` owns three explicit families: NextAuth, Auth API, and ChatGPT.
They share the account DID, stable session logging ID, flow invocation ID, UA,
client hints, and a GeoIP-derived locale/timezone. Sentinel QuickJS consumes the
same fingerprint and emits separate tokens for `username_password_create`,
`authorize_continue`, and `oauth_create_account`. Token payload IDs, `oai-did`
cookies, and auth headers must match. Production registration fails closed when
QuickJS/SDK extraction fails and never uses the pure-HTTP PoW fallback.

`http_client` owns the per-session 403/429 circuit breaker. Registration handlers
write an atomic checkpoint before post-create AT probing, so a transport-unknown
probe can resume without replaying account creation. The active account/session
boundary remains a persisted AT followed by a conclusive HTTP 200 probe.

ReMail structured OTP values bypass localized subject matching only when the
code is six digits, the recipient exactly matches the selected mailbox, and the
sender is an exact supported OpenAI OTP address. Timestamp, snapshot, and
excluded-code checks still apply.

### Account Liveness Contract

Account liveness has one canonical backend contract. The owning implementation is
`sms_tool.account_liveness.probe_account_liveness`, and its endpoint is
`https://chatgpt.com/backend-api/wham/usage` (`CODEX_USAGE_URL`). The desktop
`--refresh-local-quota` action, maintenance scripts, batch selection, and future
API surfaces must reuse this implementation instead of issuing their own probe.

- Any HTTP 2xx result is an active AT for the liveness workflow.
- HTTP 401 / `token_invalid` is an invalid AT.
- HTTP 403, rate limiting, and transport failures are inconclusive probe failures;
  they must not be relabeled as an invalid AT.
- Registration, payment JIT authentication, and protocol payment adapters must
  not define their own `/backend-api/me` liveness probes.
- The liveness operation never performs mailbox OTP relogin unless a separate,
  explicitly named recovery workflow requests it.

`sms_tool.account_recovery` owns all side effects around that contract: local
quota-status persistence, ordered 401 recovery, verified candidate persistence,
and permanent-deactivation records. Recovery order is OAuth Refresh Token,
existing ChatGPT session cookie, protocol email-OTP login (curl_cffi), then
Codex OAuth PKCE. Browser-based re-login has been removed; recovery is
protocol-only. `sms_tool.cpa_import` owns only CPA-side listing, remote quota proxying,
payload conversion, and upload; it must not become the local liveness owner again.

### Registration Progress and Concurrency

`sms_tool.registration_progress` records stage events and persists run history.
`sms_tool.registration_concurrency` independently maps expensive stages to
resource groups, enforces bounded admission, and exposes aggregate wait metrics.
Progress reporting may call the concurrency module when a stage changes, but the
concurrency module must not write progress files or classify registration results.

### Proxy Routing Boundary

Registration, mailbox receiving, and payment each have their own proxy owner.
The desktop **设置 → 网络与支付 / 网络代理** page exposes registration and mailbox
routes only. Protocol payment egress is shown in the **批量协议支付** window as
two method-owned pools (`Checkout` and `Approve`) and is saved under
`protocol_payments.methods.<method>`.

- **Registration traffic → JP dynamic proxy.** Registration workers use
  `proxy.registration` (desktop field `注册代理（主）`) plus the `proxy.pool`
  rotation list (`注册代理池`) — a JP dynamic-session upstream whose sticky-session
  id is refreshed per worker (`phone_proxy.refresh_proxy_sid`) so each concurrent
  worker gets a distinct exit IP. `proxy.default` and the phone-verification
  proxy (`phone_reuse.proxy`) are saved to this same registration value.
- **Mailbox receiving → fixed local route.** OTP polling always resolves through
  `mailbox_proxy` (desktop field `邮箱收件代理`), which defaults to
  `http://127.0.0.1:7897` and never inherits the rotating registration proxy
  (`mailbox._resolve_mailbox_proxy`). This keeps inbox fetches on a stable local
  egress independent of the registration session.
- **Access-token/session checks → registration identity.** Account liveness,
  promotion checks, recovery, and other operations that present a saved AT or
  session restore `identity_context.proxy_affinity` from the matching
  registration pool and call `bind_account_identity` to restore the saved
  protocol fingerprint. Browser-registered accounts reopen their persisted
  browser profile and geo-aligned locale/timezone; if that browser context
  cannot be opened, the check fails closed instead of downgrading to curl with
  a different fingerprint. These operations must not use the local mailbox
  proxy, because an egress/fingerprint change can trigger upstream AT
  revocation.
- **Payment traffic → independent method-owned pools.** PayPal and protocol-payment
  modules resolve their own payment configuration. Batch protocol payment uses
  `checkout_proxy_pool` for Checkout/JIT and `approve_proxy_pool` for the
  promotion/provider/confirm/approve/redirect route where the adapter permits
  that shared exit. A registration or mailbox proxy is never treated as a
  payment override. The legacy `protocol_payments.proxy_pool` remains a
  read-only compatibility fallback and is not exposed in Settings.

Proxy string manipulation has a single authority: `sms_tool.proxy_entry` owns
parsing (`parse_proxy`) plus credential rebuild, exit-region retargeting, and
sticky-session rotation (`rebuild_proxy_credentials`, `retarget_region`,
`rotate_session`, `infer_region`). `phone_proxy` (registration/phone) and
`paypal_proxy` (payment) must not reimplement these; their
`refresh_proxy_sid` / `match_proxy_region` / `rotate_proxy_session` /
`retarget_proxy_country` / `infer_proxy_country` are thin wrappers that
normalize and delegate to `proxy_entry`, so the same provider proxy rotates
identically regardless of the calling flow (the parser understands both
`region-XX`/`-sid-…-t-N` username templates and Kookeey
`BASE-CC-SESSION-TTL` password templates, with the `\d+[smhd]` TTL unit
superset). The proxy pool itself is **IPWO-only (as of 2026-08-27)**: `config.json` carries
IPWO gate URLs exclusively across `proxy.registration` / `proxy.pool`, every
checkout/approve pool, and all payment methods' `stage_proxies` and per-method
`proxy`. Kookeey (`gate.kookeey.info`) was removed entirely from config on
2026-08-27 — it had persisted only in payment `stage_proxies` / single-method
`proxy` entries and is no longer referenced anywhere. The Cliproxy white/api
short-lived fetch path and `stage_proxy_api_urls` are **not configured** in
config (the `region-XX` / `-sid-…-t-N` username template code in `proxy_entry`
is retained but dormant), and `direct_card` rotates IPWO sticky credentials
through the same template rules as `proxy_entry`. Before any
subprocess extractor spawns, `payment_egress.assert_egress_countries` probes
the routed checkout/approve/promotion proxies and rejects a run whose observed
exit country mismatches the route plan (`protocol_payments.egress_check`,
cached per proxy+country) — mis-routing fails before any side effect.

#### Payment proxy config-per-method, health cache, and test-proxy

The **批量协议支付** window owns payment proxy configuration entirely per method
(`protocol_payments.methods.<method>`):

- **保存代理配置** (`PaymentBatchService.SaveProxyConfiguration`) writes the
  `Checkout` and `Approve` pools plus their `stage_proxy_countries` for the
  selected method; the first line is mirrored to the legacy `checkout_proxy` /
  `approve_proxy` singular keys for older workers, but the `*_proxy_pool` arrays
  are authoritative.
- **测试代理** (`PaymentBatchService.ProbeProxiesAsync` → `--test-payment-proxies`)
  probes the Checkout / Approve / update exits before a batch and shows, per
  stage, `ip / country / region`, whether the exit country is PayPal-supported,
  and any `country_mismatch`. This is the pre-flight check that stops a whole
  batch from launching on a dead or wrong-country pool.

The dialog exposes the same complete billing-region catalog for Checkout,
Approve, and Update rather than a JP/TR-only subset. Values selected in the
current dialog are request-time inputs and therefore override saved
`stage_proxy_countries` / `stage_routes` country defaults for the run and its
proxy test. Saving persists those same effective values, so a later probe does
not silently fall back to a stale country.

Pool selection shares one process-level health/geo cache
(`paypal_proxy.PayPalProxyState`, keyed by the stable `proxy_key`) so a batch of
many accounts probes each pool proxy **once** instead of hammering the free IP
geolocation services per cell. `select_proxy_from_pool(..., state=...)` ranks
candidates by accumulated health (cooldown-skipped via
`proxy_health.fail_skip_after` / `fail_cooldown_seconds`, then success-ordered),
serves the geo result from `probe_cache_ttl_seconds` (default 600s), and records
each outcome back. The geo probe itself rides the payment TLS stack (curl_cffi
Chrome impersonation, `requests` fallback). Without an explicit `state` the
selector keeps its original probe-every-candidate behaviour.

PayPal-family methods (`paypal` / `upi`) additionally validate the requested
checkout/approve egress against `payment_country_catalog.PAYPAL_SUPPORTED_COUNTRIES`
and fail fast on an unsupported country (e.g. `TR`, which PayPal withdrew from),
rather than discovering it mid-protocol. Wallet/script methods keep their own
country rules.

### PayPal Generation Type

`config.json` `paypal.link_generation_type` is the single selector exposed by
`[配置] -> [协议支付] -> PayPal生成类型`:

- `hosted_long_url`（长链）: `checkout -> stripe init -> stripe_hosted_url`, then persist a normalized `pay.openai.com/c/pay/...` hosted long URL.
- `paypal_direct`（PP直链）: `checkout -> stripe init -> pm create(type=paypal) -> confirm`, then follow Stripe `pm-redirects` to a PayPal `agreements/approve?ba_token=...` URL. The BA token is treated as sensitive and must not be logged in full.
- `paypal_direct_zero_due`（PP直链-强制0元试用）: same direct PayPal approval flow, but `require_zero_due=true`; if Stripe init shows any non-zero amount, the flow stops with `checkout_not_zero_due` and does not persist a BA approval link. This strict mode also disables hosted-link fallback and old saved-link reuse so UI state cannot show a stale `link_ready` URL after the current zero-due direct generation fails.

Checkout session families are deliberately split. An `oaics_*` identifier is a
native ChatGPT Checkout session and immediately returns its
`chatgpt.com/checkout/...` link; it must never be sent to Stripe's
`/payment_pages/{id}/init`. A `cs_*` identifier remains on the Stripe/PayPal
protocol path and is initialized before hosted-link or direct-approval handling.

#### PayPal standard approval and promotion order

The direct PayPal workflow uses one isolated Checkout transaction in this
order: Checkout creation, Stripe init, PayPal PM creation, confirm, one ChatGPT
approval submission, promotion/update on that same approved Checkout, then
polling and redirect extraction. The implementation never sends a second
approval request for the same Checkout. An HTTP 409 payload whose approval
result is `blocked` is recorded as structured `last_retry_error` evidence and
invalidates the Checkout; the bounded workflow retry starts again from a new
Checkout. An ambiguous approve or post-approve poll failure becomes `unknown`
with `requires_reconciliation`, because replaying the side effect could create
a duplicate authorization.

PayPal capability and zero-due eligibility are probed before full extraction.
`checkout_not_zero_due` is an offer/eligibility conclusion, not a generic
adapter transport failure. HTTP diagnostics retain status, a redacted endpoint,
provider error code, and a bounded sanitized response summary.

#### Promotion-update stage（0元 + PayPal 共存）

The standard direct flow applies this optional segmented stage after confirm
and successful approval, but before final polling. When
`paypal.stage_proxies.promotion` is set, the extractor calls
`POST /backend-api/payments/checkout/update` through a promo-eligible region
egress to attach the `plus-1-month-free` promo to the **same** checkout that was
created in a PayPal-supported region. This makes 0-due and PayPal coexist on one
session (PayPal availability is decided by the checkout's billing region; promo
eligibility is decided by the egress IP at update time). The stage is opt-in —
leaving `promotion` empty keeps prior behaviour. See
[`docs/paypal-zero-due-link.md`](paypal-zero-due-link.md) for the full protocol,
config keys, CLI usage, and the `PayPal区 × promotion区` matrix search
(`run_batch(..., promotion_countries=[...])`).

The UI saves the compatible low-level knobs (`checkout_ui_mode`, `link_mode`,
`confirm_style`, `resolve_ba_redirect`, `require_ba_token`, and
`require_zero_due`) so the CLI and saved-link regeneration path use the same
flow without duplicating decision logic in the desktop layer.

### One-click Registration Modes

The desktop `【一键注册】` action is only a launcher; source selection is
translated into CLI flags and the protocol remains in `sms_tool.registration`.

- Mailbox registration emits `--registration-at-only --no-phone-reuse`,
  registers by mailbox email OTP, and stores the ChatGPT access token/session.
- Phone registration uses SMSBower and stores the resulting auth session.
- Registration never invokes a payment adapter. Protocol link extraction and
  payment execution are explicit follow-up workflows owned by the standalone
  payment commands and the desktop batch-payment slice.

### WPF UI

`SmsWorkbench/AppHost.cs` is the desktop composition root. It builds a Generic Host and registers paths, logging, the Python backend client, settings, batch payment, dialogs, file launching, and `MainWindow`. Constructors receive these services directly; desktop code must not add a static service locator.

The C#/Python process boundary is `IBackendClient`. `PythonBackendClient` uses `ProcessStartInfo.ArgumentList`, supports per-command environment values for secrets, pumps stdout/stderr, observes cancellation and timeout, and terminates the whole child process tree. Structured desktop results use one versioned line:

```text
@@SMSWORKBENCH_V2@@{"schema":"smsworkbench.ipc.v2","version":2,"type":"event|result","run_id":"...","sequence":1,"timestamp_ms":0,"terminal":false,"payload":{...}}
```

`sms_tool.desktop_ipc` is the sole writer for the v2 envelope. Events and results share one prefix, schema, run id, sequence, timestamp, terminal flag, and sanitized payload. WPF accepts v1 envelopes only as a bounded read-only migration path; no v1 writer remains.

Read-heavy account/mailbox refreshes use `DesktopReadClient` as a separate
transport seam. It prefers the resident `python -m sms_tool --desktop-serve`
JSONL channel, correlates concurrent requests by request ID, and restarts the
process after an exit. A one-shot `--desktop-read` adapter remains the bounded
fallback. Both transports return the same already-sanitized payload contract;
WPF handlers must not know which transport served a request. Account and
mailbox pools are fetched together, and session parsing/sanitization caches are
keyed by file metadata rather than repeated for every row.

MVVM migration is incremental rather than a rewrite:

- `PaymentBatchWindow` + `PaymentBatchViewModel` + `PaymentBatchService` are the first complete vertical slice. The view binds commands and state; the service owns matrix serialization and backend invocation.
- `StageMatrixViewModel` is limited to the embedded protocol-payment view. `JsonlStageMatrixStore` persists sanitized payment events under `runtime/stage_matrix.jsonl`, reloads recent runs at startup, bounds retention, and deduplicates by run sequence. Protocol registration does not open or reload a matrix popup; its current v2 progress is rendered on the owning task row so historical runs cannot inflate the active batch counters.
- `SettingsWindow` + `SettingsViewModel` + `SettingsService` replace the dynamic code-built settings form. The catalog is data-driven, unknown JSON fields survive round trips, validation happens before persistence, and the replacement file is written in the configuration directory.
- Existing `MainWindow.*.cs` handlers remain operational and move behind injected services one workflow at a time.
- Registration progress lines containing `Saved session:` trigger a debounced
  asynchronous pool refresh, so successfully persisted accounts appear before
  a long batch ends. Selected-account deletion is one bounded backend batch
  command with worker concurrency, not one Python process per row.

WPF-UI is the sole desktop component library. HandyControl and MaterialDesign resources are not part of the application dependency graph.

The desktop also has three explicit seams for the SMS and account-selection surface:

- `MainWindow.Navigation.cs` owns selected-email lookup, normalization, and the themed `未选择邮箱` notice. Sidebar handlers call this seam rather than creating their own WPF message boxes.
- `DialogFactory.cs` owns application-themed information and confirmation windows.
- `SmsBowerCatalogClient.cs` owns the read-only `getCountries` / `getPricesV2` catalog lookup and response parsing used by the one-click SMS dialog. It cannot purchase, poll, complete, or cancel an activation; those lifecycle operations remain in `sms_tool.smsbower` and `sms_tool.phone_reuse`.

The desktop settings page exposes SMSBower credentials and advanced timing/retry controls only. OpenAI service, country, and price-tier selection belong to the `一键接码` workflow. Static phone-pool editing is intentionally absent from the desktop surface.

Legacy `SmsWorkbench/MainWindow.xaml.cs` code may:

- Read `config.json`.
- Apply the configured registration proxy (with local `127.0.0.1:7897` fallback) and the fixed `mailbox_proxy` route when launching non-payment commands.
- Create temporary mailbox selection files.
- Start `chatgpt_phone_reg.py`.
- Display SQLite/session/mailbox state.
- Open PayPal links in Chrome incognito.
- Render custom account and inbox popups.
- Copy verification codes from already-fetched mailbox previews.

It must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or direct SQLite business rules beyond display and deletion.

Payment and CPA operations stay separated in the UI: marking payment complete only updates PayPal status, while CPA import is launched by the explicit CPA action.

`SmsWorkbench/App.xaml` owns the fixed white-first minimalist visual system for the desktop app, with black and gray used for text, borders, navigation, and log surfaces. App icon assets live under `SmsWorkbench/Assets/`.

`SmsWorkbench/build_dotnet.ps1` is the **only** supported build entrypoint. It uses `dotnet publish` (not `dotnet build`) to emit the single canonical runnable desktop artifact to `dist/net10/SmsWorkbench.exe`, then calls `SmsWorkbench/clean_dotnet_workspaces.ps1` to remove intermediate `SmsWorkbench/bin/Debug/net10.0-windows`, `SmsWorkbench/bin/Release/net10.0-windows`, and nested runtime folders such as `win-x64` so they are never treated as second distribution directories.

`GPTRegisterTool.slnx` and `tests/SmsWorkbench.Tests` are the standard .NET solution and xUnit test project. `global.json` pins the SDK and `Directory.Packages.props` centralizes package versions. The analyzer baseline is limited to named legacy code-behind files; new services and view models keep the full configured analyzer set.

> **⚠ 禁止直接运行 `dotnet build`**。直接 `dotnet build` 只会输出中间产物到 `SmsWorkbench/bin/Release/net10.0-windows/`，该路径不是分发目录，且不会自动清理。所有编译必须通过 `SmsWorkbench/build_dotnet.ps1` 完成。

```powershell
# 正确
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
# 错误 — 不要这样做
dotnet build SmsWorkbench\SmsWorkbench.csproj
```

### CLI

`sms_tool/cli.py` is the orchestration boundary. It may:

- Parse arguments.
- Load mailbox sources.
- Choose single vs batch registration.
- Persist results through `storage.py`.
- Return meaningful exit codes.

It must not silently replace an explicit empty mailbox file with a new provider purchase. If the user passed a mailbox file and no mailbox was parsed, it exits with code `2`.

Optional command modules are lazy seams. Codex export, CPA import, PayPal payment, PayPal/UPI link regeneration, and session refresh modules are imported only inside the command handler that needs them. Importing `sms_tool.cli` or `sms_tool.__main__` must not start a command or import optional payment/browser dependencies as a side effect.

Command implementations live in focused `sms_tool/commands/*` modules (`payment`, `payment_links`, `registration`, `accounts`, `mailbox_ops`, `one_click`, `omakse`); each receives the legacy CLI's replaceable hooks through an explicit frozen context dataclass (e.g. `PaymentCommandContext`, `RegistrationCommandContext`). `cli.py` retains same-name thin wrappers only, so tests keep patching `sms_tool.cli` symbols and the WPF `BackendCommandPlanner` flag contract is unchanged.

### Mailbox Layer

`sms_tool/mailbox.py` is the compatibility seam and high-level router. Focused
provider/parsing modules own the implementation:

- `mailbox_parsers.py`: Chatai, token-file, password-file, CFWorker URI, Gmail/iCloud URL provider lines, and mailbox email normalization.
- `mailbox_remail.py`: ReMail short-lived/long-lived ordering, authenticated pickup credentials, message normalization, and OTP polling.
- `mailbox_cfworker.py`: CFWorker mailbox creation, message fetch, proxy/direct fallback, and OTP polling.
- `mailbox_graph.py`: Microsoft OAuth refresh.
- `mailbox_gmail.py`: Gmail IMAP receive, Gmail SMTP send, app-password auth, and OAuth refresh auth.
- `mailbox_icloud_url.py`: authenticated per-account iCloud message URLs, embedded-card HTML, list/detail APIs, and data-URL body decoding.
- `outlook_imap.py`: Outlook IMAP fallback when Graph is unavailable or stale.
- `mail_otp.py`: OTP extraction, recipient filtering, subject matching, issued-after filtering, and candidate ordering.

Registration OTP polling accepts a pipe-separated subject keyword string. The
registration flow uses both `verification code` and `login code`, because the
passwordless signup path can receive either subject even when the auth state is
still a signup transaction. Provider clock-skew normalization belongs to the
mailbox router rather than registration orchestration. CFWorker uses the small
`cfworker_otp_issued_after_grace_seconds` window, while ReMail defaults to
`remail_otp_issued_after_grace_seconds=90` because its observed `receivedAt`
timestamps can trail the local send clock by more than one minute. The pre-send
message ID snapshot still prevents an older OTP from being reused.
ReMail registration also performs one bounded resend after 30 seconds without
resetting the original accepted-message window; this recovers provider delivery
misses without extending the configured total OTP timeout.
ReMail batch creation scales its HTTP timeout with the requested quantity, and
the token-file parser accepts `remail://email---serviceToken---orderNo---purchaseId`
so a completed server-side batch can be recovered after a client timeout without
buying the same mailbox quantity again. Ambiguous timeout/retryable-5xx responses
also trigger a strict recent-order lookup by request window, project, product,
mode, and exact quantity before any failure is returned to the caller.

Gmail is a first-class mailbox provider. The preferred import shapes are
`gmail://email---app_password` for app-password mode and
`gmail://email----client_id----client_secret----refresh_token` for OAuth mode.
Gmail credentials and OTP recipients are matched by the complete normalized
address. Dotted local parts, `+tag` addresses, and `googlemail.com` variants are
not rewritten to another mailbox, and no alias mapping file is read.
It must not write registration results or modify mailbox pool files during registration.

iCloud forwarding mailboxes can be imported as `email----receive_url` or
`email---receive_url`. The receive URL is stored as a mailbox credential, never
included in public diagnostics, and is decoded through the shared mailbox/OTP
seam. Both server-rendered message cards and list/detail JSON pages are
normalized into the canonical message shape before OTP filtering.

### Registration Layer

`sms_tool/registration.py` 是协议注册的**编排入口（orchestration seam）**，
本身不再承载具体实现。拆出后职责划分如下：

- `auth_flow.py`: signin URL 拼装 / authorize 导航 / continue 调用 / auth-state 页面分类。
- `account_creation.py`: OTP 校验 / create-account 继续流 / `/api/auth/session` 拉取。
- `account_2fa.py`: TOTP 2FA 自动 enrollment（密钥生成 / totp URI 校验 / 激活轮询 / secret 入库）。
- `otp_strategy.py`: 注册用 OTP 发送 / 重发 endpoint 选择。
- `sentinel_tokens.py` / `sentinel_quickjs.py`: Sentinel 提取 / QuickJS SDK 路径 / PoW+浏览器回退 / 缓存。
- `auth_state.py`: `client_auth_session_dump` 抓取与脱敏诊断摘要。
- `batch_runner.py`: 并发注册 worker 调度 / 结果排序 / mailbox 数量上限 / 网络+auth-state 失败有界重试并换新鲜代理 session。HTTP 429 单独归类为 `rate_limit`，不得立即重试；首个 429 会打开进程内认证流冷却电路，阻止同批次等待中的账号继续冲击上游。
- `registration_outcome.py`: 注册结果归一化 — 账号创建错误提炼 / 多轮 AT 稳定性探测 / `codex_oauth.require_registration_refresh_token`、`require_registration_phone_verification` 开关。
- `session_builder.py`: 从注册最终态拼装 canonical session JSON（含 `mailbox` 嵌套、token 优先级链、profile/device/paypal 字段、`created_at`）。

`registration.py` 通过 `from .session_builder import build_session_file as _build_session_file`
和 `from .registration_outcome import (_create_account_error, _probe_registration_access_token, ...)` **对外暴露**这些 helper；不允许用本地定义遮蔽它们，也不要在运行时修改其他模块的 `CFG` / 请求全局状态。

批量注册每条加载的 mailbox 最多使用一次：`--count` 超过已加载的唯一 mailbox 数时会被截断，不会用取模方式回绕重复复用。
每个账号拥有独立的 Sentinel 事务与 `oai-did`，batch worker 不把 token 返回共享池；账号创建过程产生的新鲜 refresh token 不写入共享缓存，OAuth create 创建的 refresh token 保留账号既有的 device ID。
Fresh 提取受可配置的有界信号量保护（`sentinel_max_concurrency` 默认 2，上限 4）；缓存路径调用方保留 single-flight 填充语义。
认证流使用独立 `registration.stage_concurrency.auth` gate，默认并发为 1；OTP、create-account 和 session 拉取继续使用 `network` gate。这样批量 worker 可以并行准备 Sentinel/邮箱，但不会并发轰击 `/api/accounts/authorize/continue`。

`auth.openai.com/login` 与 `/log-in` 只表示当前 auth-state 的中间页面，不足以证明邮箱已经注册。`auth_flow.py` 必须继续提交 username，并以是否推进到邮箱验证或后续状态作为判定依据；只有 continue 后仍无法推进时，才记录一次有界的 `login_redirect_not_advanced` 失败并尝试下一条 auth 路径。注册进度的每次 attempt 只允许一个 terminal event，持久化层不得重复追加 `failed` / `completed`。

If OTP validation succeeds but create-account returns
`registration_disallowed`, the failure is treated as a provider/server-side
registration refusal for that mailbox/context, not as a mailbox polling failure.
The local error path must preserve that create-account code instead of masking
it with later auth-session transport errors.

### Account Seed Layer

`sms_tool.account_seed` owns the shared lookup of account/session seed data. It may:

- Load an explicit `session_*.json` file.
- Load the SQLite account row for an email.
- Merge persisted raw JSON with the session file.
- Expose normalized `email`, `access_token`, `cookie_header`, and refresh-token fields.
- Extract a ChatGPT access token from flat or `auth_session` shaped data.

Payment adapters may call this seam, but must not duplicate SQLite/session merging logic or import private helpers from each other. This keeps PayPal link regeneration, PayPal browser payment, and legacy PayPal automation independent from one another.

### PayPal Link Layer

`sms_tool/gen_pp_link.py` only generates the hosted Stripe/PayPal/UPI URL from an access token. It does not perform PayPal account signup, card entry, SMS verification, wallet authorization, or final payment authorization.

`paypal.billing_regions` controls checkout billing country/currency, and `paypal.stage_proxies` can route stages independently:

```json
{
  "billing_regions": ["DE"],
  "stage_proxies": {
    "checkout": "socks5h://127.0.0.1:7897",
    "stripe_init": "socks5h://127.0.0.1:7897",
    "payment_method": "socks5h://127.0.0.1:7897",
    "confirm": "direct"
  }
}
```

`paypal.billing_regions` controls the Checkout billing country/currency, not the proxy exit. The current PayPal regeneration path follows the standalone long-link script logic with `paypal.link_mode=chatgpt_checkout` and `paypal.checkout_ui_mode=hosted`: it posts ChatGPT checkout for the configured billing region, calls Stripe `/v1/payment_pages/{cs_id}/init`, reads `stripe_hosted_url`, and stores the resulting hosted long URL (`checkout.stripe.com/c/pay/...` normalized to `pay.openai.com/c/pay/...`). It deliberately does not enter Stripe payment-method creation, confirm, or ChatGPT checkout approve, so it avoids the BA-specific `confirm returned no redirect` / `approve blocked` path. `paypal.resolve_ba_redirect=false` and `paypal.require_ba_token=false` are expected in this mode. With `paypal.explicit_proxy_overrides_stage_proxies=false`, a UI/CLI `--proxy` is used as the default candidate proxy but does not override stage-specific routing. Batch regeneration is intentionally conservative: `paypal.max_regenerate_workers` defaults to `1`, and `paypal.regenerate_delay_seconds` staggers accounts so a UI request with `--workers 4` does not fan out four simultaneous checkout creations and trigger `429`. With `paypal.require_zero_due=true`, non-zero checkout totals fail immediately.

The WPF config dropdown and `sms_tool.gen_pp_link` presets currently support `JP`, `US`, `AU`, `DE`, `FR`, `GB`, `IN`, and `BR` for checkout billing-region generation. UPI QR generation separates `checkout_country` from `payment_country`: the checkout request uses `checkout_country` for `billing_details.country/currency`, while `payment_country` records the intended local payment method country and stage routing still comes from `upi.stage_proxies`. The default split is JP checkout billing plus IN payment/provider routing. Stripe remains authoritative: if a JP checkout does not expose `upi` in `payment_method_types`, generation fails with `upi_not_available` instead of fabricating a QR.

### Checkout Contract and Capability Probe

`sms_tool.checkout_contract` is the canonical schema for ChatGPT Checkout and
Stripe init. `CheckoutRequestContract` owns the plan, entry point, promotion,
billing country/currency, Checkout UI mode, browser locale/timezone, and Stripe
payment-method profile. `CheckoutSessionContract` normalizes current `cs_*` and
`oaics_*` response shapes. `StripeCapabilityEvidence` extracts amount in minor
units, currency, ordered/standard/custom payment methods, and the offer shape
from nested Stripe init responses. Native generators and new adapters must use
these public types instead of copying request dictionaries or parsing only one
response layout.

`sms_tool.payment_capability.payment_method_capability_probe` is a
side-effect-limited network probe, not a liveness-only check. It creates a
ChatGPT Checkout and calls Stripe init, then stops before payment-method
creation, confirm, ChatGPT approve, polling, or provider redirect. Its output is
structured as `eligible`, `ineligible`, or `unknown`, with `decision`,
`conclusive`, amount/currency/method evidence, `retryable`, and `error_stage`.
When zero due is required, an available method with a non-zero amount is a
conclusive `nonzero_offer`; a missing amount or transport/protocol ambiguity is
`unknown`.

The batch matrix supplies the checkout country and stage proxies to this probe.
`--payment-probe-only` therefore performs JIT authentication, matrix validation,
Checkout, and Stripe init; it does not merely validate the registration country.
A probe-only Canary pauses the method profile when all evaluated results are
systemically unknown. Conclusive `payment_method_unavailable` and
`nonzero_offer` results remain account/offer conclusions and do not pause the
profile.

Batch reports keep capability probes separate from formal extraction results.
Each desktop click creates a fresh generated batch ID by default. Checkpoint
loading and event replay occur only when the caller explicitly sets
`resume_checkpoint`; the UI displays `新执行` or `断点恢复` and the number of
restored accounts. Account events are appended to a JSONL stream with stable
domain, operation, run, batch, account, stage, and status fields, so a desktop
restart can reconstruct progress. Terminal rows also persist per-stage timing,
total duration, and the last failed stage.

### Shared Wallet Provider Layer

GoPay and GrabPay share `sms_tool.wallet_provider`; their production HTTP and
stage-proxy routing live in `sms_tool.wallet_transport`. GoPay uses ID/IDR and
adds an independent Promotion/Update request after Checkout so a TH promotion
exit can produce and verify a zero-due offer before the flow returns to its ID
provider route. GrabPay uses PH/PHP without that GoPay-only update requirement.
The remaining sequence is Stripe init -> wallet PM -> confirm -> ChatGPT
approve -> poll -> allowlisted provider redirect. Checkout, promotion,
Stripe init/provider, payment-method, confirm, approve, and redirect stages may
be routed independently.

GCash is a separate custom-payment-method adapter implemented by
`sms_tool.gcash_provider` and `sms_tool.gcash_transport`; it is registered in the
same manager catalog but does not enter the shared wallet core. The wallet core
also exposes `probe_only=True` for fixture contract tests. It reuses the real
pre-side-effect preparation path: GoPay performs Checkout -> Promotion/Update ->
Stripe init, while GrabPay performs Checkout -> Stripe init. Neither path may
create a wallet PM or confirm an intent. GoPay and GrabPay fixtures live under
`tests/fixtures/wallet_provider/`; GCash has its own provider and transport
tests. The manager and batch call the provider-aware `probe_payment_method`
entry point, which returns the same capability result contract as the generic
`payment_method_capability_probe` while preserving matrix and Canary semantics.

### Payment Responsibility Boundary

Payment is split into three independent responsibilities:

All create-link requests first enter `sms_tool.payment_link_manager`. It moves
each run through `created -> validating -> preparing_proxy -> running ->
extracting` and one of `completed`, `failed`, `cancelled`, `unknown`, or
`timed_out`, dispatches the native, shared-wallet, or vendored adapter,
normalizes the result, and appends a redacted record to
`runtime/payment_link_runs.jsonl`. Full payment/provider URLs and QR artifacts
are returned to the caller but are not written to run history; persistence keeps
only `*_present` metadata for those artifacts.

Routing is compiled before JIT authentication by `PaymentRoutePlanner`. One
`PaymentRoutePlan` is reused by CLI, batch retries, manager adapters, and wallet
transports, so proxy pools are selected once rather than independently at every
stage boundary. Canonical configuration uses
`protocol_payments.proxy_pools.<name>` plus per-method `stage_routes`; legacy
`checkout_proxy_pool` and `approve_proxy_pool` remain compatibility presets.

Every normalized result carries `retryable` and `error_stage`. A successful
result forces `retryable=false` and an empty error stage. `cancelled` is a
non-retryable terminal state. `unknown` sets `requires_reconciliation=true` and
is also non-retryable at the manager boundary because a confirm/approve request
may already have taken effect. `timed_out` is distinct from a generic failure
and defaults to retryable. Callers must use these fields instead of retrying by
matching free-form error text.

1. **Create checkout/link**: `sms_tool.payment_link_manager` and
   `sms_tool.gen_pp_link`.
   They read an access token and return/store a hosted checkout URL or explicit
   failure details. They do not complete payment. The BLIK adapter is the explicit
   exception: when a six-digit BLIK code is supplied, its operation is
   `execute_payment`, it submits the code, and it returns `status=completed`
   without fabricating a URL. The UI must label this action as payment execution.
2. **Execute an explicit payment command**: `sms_tool.paypal` (via `auto_pay`).
   It only runs when the user requests `--one-click-pay` or a matching UI action.
   It uses existing account seed data and payment links rather than registering
   accounts. UPI has no one-click execution adapter in this project; it is a
   hosted-link generation method only.
3. **Persist/display payment state**: `sms_tool.storage` and `SmsWorkbench`.
   Storage normalizes status fields; the UI displays and launches commands. The
   UI must not infer success from a URL alone.

Registration, mailbox refresh, CPA import, account scan, SQLite rebuild, and
session refresh must not implicitly run payment execution. Link regeneration may
update `paypal_url` only through the payment-link seam, and failed regeneration
must preserve useful existing URLs unless the caller explicitly clears them.

### MoMo Batch Validation Boundary

MoMo batch extraction is a staged workflow. First probe each local access token
through the local Codex quota endpoint and retain only HTTP 2xx accounts. Then
run the MoMo adapter through a verified VN proxy session. An account passing the
token probe is only authentication-ready; it is not yet MoMo-ready.

The adapter result must preserve these distinct outcomes:

- `account_trial_ineligible`: the account is authenticated but has no usable trial.
- `card_only_full_price`: checkout is available, but MoMo or a zero-due offer is not.
- `approve_result_blocked`: the provider flow reached approve but did not yield a usable result.
- `ready_with_qr`: a `payment.momo.vn` URL or decoded QR file exists; this is the only successful MoMo extraction outcome.

Batch reports must include requested, probed, non-401, attempted, link-ready,
QR-ready, and failure-category counts separately. Runtime reports, account lists,
payment URLs, QR images, access tokens, and authenticated proxy URLs remain local
artifacts under ignored runtime paths and must not be committed or packaged.

### JIT Payment Authentication and Batch Execution

`sms_tool.payment_auth` is the only payment-boundary AT gate. A saved account is
probed immediately before checkout. HTTP 401 enters the shared recovery chain:
OAuth Refresh Token, existing cookie `/api/auth/session`, protocol email-OTP
login (curl_cffi), then Codex OAuth PKCE. Browser-based re-login has been
removed; recovery is protocol-only. Every candidate AT is re-probed and persisted
only on HTTP 200. Recovery uses the account proxy and rejects an auth session
whose email differs from the target. Permanent `account_deactivated`
rows never enter a relogin loop. Public diagnostics contain only status codes,
JWT timing, and a short SHA-256 correlation value.

`sms_tool.payment_batch` owns protocol-payment cohorts. It consumes an explicit
email list, applies bounded method-specific concurrency, runs the JIT gate per
worker, assigns configured eligibility-matrix cells by payment method and
registration country, retries only classified transient failures, and writes an
atomic token-free checkpoint after every completed account under
`runtime/payment_batches/`. `--payment-batch-id` makes the cohort stable only
while the execution mode, matrix, proxy, retry, and JIT settings retain the same
hashed run signature; a signature mismatch starts a fresh run instead of reusing
incompatible rows. `--payment-canary` limits the cohort and
`--payment-probe-only` runs the shared Checkout/Stripe capability probe after JIT
authentication and matrix validation. It creates no payment method, sends no
confirm or approve request, and never follows a provider redirect. Probe rows
set `capability_probed=true`, keep `attempted=false`, and report eligibility and
offer evidence separately from full extraction attempts.

Registration failures no longer enter `accounts` through CLI orchestration.
They are written to `registration_audit`; a successful initial AT probe is a
candidate and only the configured stability-window HTTP-200 result becomes an
active account/session. `accounts.batch_id` and `accounts.registration_state`
allow payment batches to select a registration cohort without inferring it from
timestamps.

Registration stage scheduling is implemented by `registration_progress`: a
worker releases its network slot when it enters mailbox OTP polling, then
reacquires the bounded network gate for resend/validation/account creation.
AT probing and payment have independent caps. This permits more mailboxes to
wait concurrently without multiplying auth/provider request concurrency.
`--target-at200` uses stable-probe successes as its target and respects mailbox
purchase/cost caps plus the ReMail supplier dead-rate circuit breaker.

### Payment Eligibility Matrix

`protocol_payments.matrix.cells` defines small canary cohorts. Each cell records
registration country, checkout/promotion/provider/approve/redirect countries,
strategy, and sample size. The executor reports authentication, eligibility,
offer shape, link-ready, and QR-ready counts per cell. Account/offer conclusions
are not retried with another proxy.

The default wallet matrix gives each profile a `sample_size` of 1: GoPay uses an
ID registration/checkout/provider chain with a distinct TH Promotion/Update
stage, while GCash and GrabPay use PH chains through their respective adapters.
The batch window displays only the billing/Checkout and discount/Approve columns;
the underlying promotion/provider/redirect country fields remain intact for
adapter contracts and serialization.
Use `--payment-canary 1` to turn one of those profiles into a true one-account
validation. A probe-only Canary counts a conclusive capability result as
completed even when the result is `ineligible`; only a systemic `unknown` result
pauses subsequent full batches for that method.

MoMo carries Checkout, Promotion, Stripe provider, Approve, and Redirect proxies
as distinct stage values end to end. A common seed may still be rotated to the
cell's stage countries, preserving one sticky chain identity. Its Stripe API
version, runtime version, client betas, and confirm style are configuration data
under `protocol_payments.methods.momo.stripe_profile`, so a one-account canary can
detect protocol drift before a large batch.

Kakao emits one structured JSON contract on success and failure. Conclusive
credential or checkout-offer results stop immediately; only network/proxy errors
rotate a seed. A successful result requires a Kakao/Nicepay redirect host.

Sentinel account transactions remain independent. Performance optimization is
limited to bounded extraction concurrency, SDK file reuse, token-free queue and
provider timing metrics, and short provider circuit breakers. Sentinel tokens,
device IDs, UA profiles, and TLS profiles are never pooled across accounts.
An optional `sentinel_prewarm_window` starts a bounded set of one-to-one futures
for the first registration workers. Each future is bound to that account's
first-attempt proxy and is consumed once; a retry always creates a fresh
transaction.

### PayPal Payment Layer

`sms_tool/paypal/` owns browser page mechanics: form filling, PayPal challenge detection, SMS polling hooks, and browser-engine fallback. It must not regenerate links, select accounts, or persist SQLite rows directly except through the result passed back to the adapter.

The package is split by layer with a strictly one-way dependency direction (no cycles). `sms_tool/paypal_auto.py` is kept only as a compatibility shim that re-exports the package; new code should import from `sms_tool.paypal` directly.

| module | responsibility |
| --- | --- |
| `paypal.orchestrator` | `auto_pay` entry point and strategy selection: reverse protocol -> nodriver -> anti-detect browser; persists the outcome |
| `paypal.flow_steps` | Ordered step machine (`_run_browser_steps`) plus the human-verification and SMS gates that can interrupt it |
| `paypal.form_steps` | Semantic PayPal checkout fields: email, name, phone, password, card, billing address, terms |
| `paypal.session` | Browser context helpers: cookie import, navigator fingerprint override, load waits, overlay dismissal, screenshots |
| `paypal.dom_fields` | Generic locate / fill / read primitives with selector fallbacks; no PayPal-specific meaning |
| `paypal.config_picker` | Card / address / phone round-robin selection, index files, alias email, result persistence |
| `paypal.errors` | `_PayPalStepError` (dependency-free leaf, importable from every layer) |

The retired `sms_tool/paypal_links.py` regeneration wrapper has been removed.
New and repeated PayPal links use the unified `payment_link_manager` interface;
`sms_tool/paypal_protocol.py` remains the narrow redirect-parsing and transport
module used by the PayPal adapter.

`sms_tool/paypal_reconciliation.py` is a separate merchant-return accounting
API. It accepts an observed return URL or return payload plus a caller-supplied
authenticated transport, disables automatic redirect following, and validates
every hop against the `pm-redirects.stripe.com`, `pay.openai.com`, and
`chatgpt.com/checkout/verify` route allowlist. Its typed result separates
`classification` (`conclusive`, `unknown`, or `failed`) from payment `outcome`
(`succeeded`, `failed`, `cancelled`, or `unknown`) and includes structured
retryability, stage, code, and secret-free hop evidence. Full URLs, client
secrets, SetupIntent ids, and bearer-like query values are never returned.

Reconciliation does not call or wrap `generate_payment_link()`, does not persist
`paypal_url`, and does not change the extraction interface's meaning. An
`unknown` reconciliation caused by transport failure or a pending verification
page may be retryable inside this independent API; that does not weaken the
payment-link manager rule that an unknown side-effecting extraction outcome must
be reconciled before retry.

`sms_tool.payment_reconciliation.reconcile_payment_result` is the method-neutral
dispatch boundary. Catalog policy selects a method-owned reconciler; unsupported
or inconclusive outcomes remain `unknown` and require reconciliation rather
than being retried as ordinary adapter failures.

`sms_tool.paypal_authorization_queue` durably stores PayPal BA follow-up work
after a BA approval artifact is extracted. Extraction never performs the final
customer authorization inline. The queue is PayPal-only, deduplicates by the
sensitive BA token internally, and exposes only presence booleans in public
results. Other payment methods must not enqueue items or surface this queue in
their desktop views.

### Local Provider Services

`services/mail-otp-web` is a standalone operator diagnostic surface for Microsoft Graph inbox/OTP extraction. It accepts the same mailbox account-line formats as `sms_tool.mailbox`, refreshes Microsoft access tokens, displays recent messages, and may return a rotated mailbox refresh token to the operator. It is not the main registration mailbox owner: registration still uses `sms_tool.mailbox`, and this helper service must not edit `hotmail.txt`, session JSON, or SQLite rows directly.


### Agent Identity Layer (Explicit Import Only)

`sms_tool/agent_identity.py` is an explicit credential-conversion boundary used by
SUB2API import. The registration pipeline no longer calls it:

- Generate Ed25519 key pairs in PKCS#8 format.
- Persist private keys independently under `sessions/agent_identities/`.
- Register Agent Identity and task only when the operator explicitly requests the
  SUB2API Agent Identity import mode.
- Handle 403 responses from Free accounts (expected limitation, silently handled).
- Fall back to OAuth mode when Agent Identity registry is disabled.

Agent Identity keys are reused across SUB2API imports. The private key is never
logged or exported in full. A failed Agent Identity conversion must not change the
registration result or invalidate an HTTP-200 AT account.

### SUB2API Import Layer

`sms_tool/sub2api_import.py` owns the SUB2API import boundary:

- Accept session JSON or account-seed data.
- Support three auth modes: `auto` (Free accounts prefer Agent Identity), `oauth`, and `agent_identity`.
- Normalize account data via `session_converter.py` before upload.
- Optionally verify connectivity after import (`sub2api.verify_after_import`).
- Export `expires_at` as Unix timestamp (int64) for Go backend compatibility.

It must not perform registration or payment. It reads existing session data and
agent identity keys, then uploads to the configured SUB2API endpoint.

### Removed / Deprecated Surfaces

- `browser_extensions/paypal_autofill/` is retired. The maintained PayPal browser path is the project-local Python adapter.
- `tests/test_paypal_autofill_*.py` are retired with that extension.
- LuckMail support is retired; ReMail is the maintained API mailbox source.
- Runtime debug artifacts and `__pycache__` folders are not source surfaces and should be deleted or ignored.
- Backup binaries such as `*.exe~` and unused duplicate artwork are not source surfaces.
- `.zcode/`, the obsolete root `gates/`, pytest/Python caches, historical
  protocol logs, and `bin/obj` output are generated state, not source. Active
  cross-process lock slots belong only under ignored `runtime/gates/`; canonical
  desktop and release artifacts are rebuilt under `dist/` for each release.

The iDEAL, BLIK, and TWINT subprocesses share
`common.protocol_core.ProtocolResultReporter` for exactly-once
`protocol_payment.v1` terminal output, policy-based redaction, `already_paid`,
missing-output fallback, and BLIK execute-payment framing. Extractors own only
method-specific orchestration and feed their terminal payload into this seam.

### Test Layer

`tests/` is the only test directory. Tests should stay offline by default and target module seams rather than live vendor systems.

Run all tests with:

```powershell
python -m unittest discover -s tests
```

### Storage Layer

`sms_tool/store/` owns the implementation; `sms_tool/storage.py` is only an 8-line
backward-compatibility shim (`from .store import *`). New code should import from
`sms_tool.store` directly, but note that 25 files (19 production + 6 test) still reach
persistence through the shim — see Subpackage Structure.

`sms_tool/store/` owns:

- SQLite schema creation and migrations.
- Case-insensitive account deduplication.
- Email normalization before upsert.
- PayPal status and refresh-token status persistence.
- Payment method persistence for UPI/PayPal compatibility.
- Rebuilding SQLite from `sessions/session_*.json`.

`accounts.email` is treated as a normalized logical key. Updates should modify an existing row for the same complete email address instead of creating a new row with different casing.

`AccountSessionModel` is the persistence-boundary input. Legacy mappings are
normalized once by `storage.upsert_account`; internal storage code then reads
typed `SessionCredentials`, `MailboxSnapshot`, and `PaymentSnapshot` values.
Required credentials retain dedicated encrypted/private storage semantics, while
`accounts.raw_json` and registration audits use `safe_snapshot()` and contain no
access token, refresh token, mailbox token, BA token, TOTP secret, password, or
card value.

### Desktop Backend Task Lifecycle

`BackendTaskCoordinator` owns the one-active-task invariant, cancellation token
lifetime, timeout/cancel/error normalization, and cleanup around
`PythonBackendClient`. `MainWindow.Tasks.cs` owns only UI transitions and command
callbacks. New backend actions must use the coordinator instead of adding another
window-owned `CancellationTokenSource` lifecycle.

### Codex OAuth and CPA Layer

`sms_tool/codex_oauth.py` owns only the Codex OAuth authorization-code + PKCE sequence:

- Build the OAuth authorize URL.
- Reuse existing auth cookies when they already produce a callback code.
- Continue username login.
- Complete email OTP when OpenAI routes the flow to an email OTP page or when takeover is explicitly enabled.
- Exchange the callback code for OpenAI `access_token`, `id_token`, and `refresh_token`.

It deliberately does not upload to CPA and does not own phone-number inventory.

`sms_tool/codex_sentinel.py` owns auth.openai.com sentinel cookie/header helpers. Cached Cloudflare/auth cookies may be reused, but the cached `oai-did` is stripped before import so one global browser fingerprint is not assigned to every account.

`sms_tool/codex_phone.py` owns add-phone completion. It is disabled by default. If OpenAI requests `/add-phone`, the OAuth layer reports `add_phone_required` unless `codex_oauth.auto_phone_verification` is true.

`sms_tool/session_converter.py` is the multi-format conversion core used by export-account flows. `sms_tool/codex_export.py` converts session JSON into the compact Codex JSON shape. `sms_tool/cpa_import.py` accepts existing AT-only session JSON, normalizes it into the CPA payload shape, and uploads it without requiring RT.

Important behavior:

- `codex_oauth.allow_passwordless_takeover=true` is an explicit escape hatch for manual export/refresh paths.
- Forced email OTP may still require add-phone for some accounts. Phone SMS handling remains a separate opt-in boundary via `codex_oauth.auto_phone_verification`.

## Portable Configuration

All paths in `config.example.json` are relative by default:

```json
{
  "email_registration": {
    "token_file": "mailbox_tokens.txt",
    "cfworker_otp_issued_after_grace_seconds": 10
  },
  "mailbox_proxy": "http://127.0.0.1:7897",
  "proxy": {
    "registration": "http://user:pass-JP-session-5m@gateway:port",
    "default": "http://user:pass-JP-session-5m@gateway:port",
    "pool": ["http://user:pass-JP-session-5m@gateway:port"]
  },
  "protocol_payments": {
    "proxy_pool": ["http://user-region-JP-sid-session-t-5:pass@gateway:port"]
  },
  "runtime": {
    "directory": "runtime"
  },
  "storage": {
    "sqlite_path": "runtime/accounts.sqlite3"
  },
  "codex_oauth": {
    "allow_passwordless_takeover": false,
    "auto_phone_verification": false
  },
  "output": {
    "directory": "sessions"
  }
}
```

Relative paths are resolved from the repository root via `sms_tool/paths.py` or WPF `rootDir` detection. A user may still use absolute paths in local `config.json`, but committed config templates and docs should not depend on one developer's machine.

## Status and Dedup Semantics

The WPF list may load the same logical account from:

- mailbox pool text file,
- SQLite,
- session JSON fallback.

Rows are deduplicated by normalized email for display. SQLite/session rows have higher priority than mailbox-pool rows because they represent updated registration/payment state.

## Exit Codes

```text
0  command completed normally
2  explicit mailbox source was empty or malformed
3  command completed with a provider or import failure
```

## Local Files That Must Stay Out of Git

```text
config.json
sms_tool/config.json
services/mail-otp-web/config.json
mailbox_tokens.txt
sessions/
runtime/
dist/
.dotnet/
skills-lock.json
session.json
hotmail*.txt
*_tokens.txt
```

These paths are ignored, but most are not disposable. Configuration backups,
mailbox/token files, session data, provider state, and runtime reconciliation or
batch checkpoints may be the only recovery evidence for an account and must be
preserved unless the operator explicitly archives or removes them. Only derived
caches and build output (`__pycache__`, `.pytest_cache`, .NET `bin/obj`, test
results, retention helper logs, and tool-local metadata) are routine cleanup
targets.

## Release Boundary

A release is built only from a pushed, clean source commit. Ignored account and
runtime data stay local and are not copied into the package. The canonical
`scripts/build_installer.ps1 -Version <tag>` invocation produces one installer,
one portable ZIP, and one SHA-256 manifest under `dist/release/`; all uploaded
assets must come from that same invocation. Historical `docs/release-<tag>.md`
files remain immutable after publication.

## Terminal Account Cleanup

`sms_tool.account_cleanup` owns the classification rule for local removal. It
accepts only missing-AT rows and explicit terminal states such as
`account_deactivated`, `dropped`, `token_invalid`, `401`, or expired-token
errors. Timeout, proxy, TLS, and other unknown probe failures remain eligible
for a later recheck. `scripts/cleanup_invalid_accounts.py` owns the filesystem
operation: dry-run is the default, and `--apply` creates a SQLite backup,
archives matching session JSON files, and removes matching mailbox-pool lines.

# 落地记录：2026-10-25 桌面卫生、测活/查优惠与 passwordless 重登

- 来源：2026-10-25 全仓扫描（协议注册 + WPF 桌面，会话内产出，未单独成文）+ 同日两次实机测活/查优惠取证
- 日期：2026-10-25
- 回归：**pytest 6877 passed / 10 skipped / 1333 subtests**（本轮新增 11 个 Python 用例：5 个桌面/测活/优惠 + 6 个重登与标签）
- 桌面回归：**dotnet 557 passed / 0 failed**（+6 用例）；`dotnet build -c Release` 0 warning / 0 error
- 门禁：16 个 ratchet/scan 全绿（`import_layer` 434 边、`delayed_import` 420/319、`function_length` 63、`format_guard` 814 文件、`docs_consistency_scan`、`module_coverage` 314/314 …）；`scripts/scan_hardcoded_secrets.py` **0 findings**（exit 0）
- 实机结论：两个 `registration_mode: passwordless` 的 remail/Outlook 账号 AT 恢复，测活 **2/2 可用**，优惠检查 **2/2 `trial_eligible`**

---

## 结果总表

| # | 落地内容 | 主要改动 | 验证 |
| --- | --- | --- | --- |
| 1 | 选中邮箱 / 一键接码的临时凭据文件不再永久留在 `%TEMP%` | `BackendCommandPlanner.CreateMailboxFileRegistration` / `CreateOneClickSms` 接受调用方临时文件并写入 `plan.TempFiles`；`MainWindow.Tasks` 新增 `RunBackend(plan)` 重载 + `RunBackendAsync(temporaryFiles:)`，在既有 `finally` 里删除；`RunAccountBatchBackend` 也传 `plan.TempFiles`（测活/优惠的 email-file 同属此类）。顺带修掉「一键导入」的同类泄漏 | `BackendCommandPlannerTests` 3 例：声明临时文件 / 池文件**永不**自动声明 / 一键接码合并 |
| 2 | 桌面不再虚构 `127.0.0.1:7897` 代理 | `MainWindow.Helpers` 的 `GetRegistrationProxy` / `GetMailboxProxy` / `GetLivenessProxyPool` 空即空；`SettingsService.Save` 与 `ReadValue` 去掉 loopback 归一化；`SettingsCatalog` 不再预填；`config.example.json` 清空 loopback 并改用 `proxy.example` 占位 | `SettingsServiceTests` 2 例（空白保存不写入 loopback / 空配置读回为空）；`test_registration_proxy_routing.py` 的治理断言由「必须有 LocalNonPaymentProxy」改为「必须没有」 |
| 3 | 单行测活（`--quota-usage`）改读合并 payload | 新增公开缝 `store.get_account_payload`（行 + `raw_json`，列优先、空列不覆盖），`commands/accounts.quota_usage` 改用它。`identity_context` 没有独立列，只存在 `raw_json`，用裸行探测会丢指纹/设备号/代理亲和/浏览器身份 | `test_store_modules`（payload 合并且 `identity_context` 不是列）、`test_account_liveness`（命令层把 identity 传进探针） |
| 4 | 查优惠：浏览器上下文不可用时降级协议，而不是判账号失败 | `promotion_batch` 删掉 `browser_context_unavailable` 短路，改走候选循环并记 `browser_fallback: unavailable`（与批量测活已有语义一致）；`browser_fetch_for_account` docstring 从「callers fail closed」改为实际契约 | `test_promotion_egress_guard` 2 例（不可用→仍探测且带标记 / 可用→仍走浏览器且无标记） |
| 5 | passwordless 账号重登不再提交本地生成的密码 | `account_recovery._passwordless_registration` + `relogin_chatgpt_email_account` 传 `password=""`，让 lane 走 `allow_passwordless` 分支真正发码。判据是 `registration_mode`（显式 passwordless / `password_unknown`），缺省模式保持原行为 | `test_account_recovery` 3 例（passwordless→空密码 / password→仍提交存储密码 / 判据仅显式生效） |
| 6 | `relogin_otp_failed` 按证据判定，不再按字面匹配 | 新增 `_relogin_spent_email_code`：只相信每个 attempt 自己的 `error`，排除 `existing_login_password_verify_failed` 一类「未发码」标记；统一用于 `_health_status_code`、`_relogin_failure_quota_status`、guard 的 `failure_class`。仍保留 cooldown，只是类名诚实 | `test_account_recovery` 3 例（password-verify 形状→`relogin_failed` 且仍记 300s / 真 OTP 409→仍 `relogin_otp_failed` / codex 的 `passwordless_email_otp_poll_timeout`→仍算 OTP） |
| 7 | 模板与扫描器 | `config.example.json` 13 处 loopback 换占位符；`scan_hardcoded_secrets.py` 新增 `ARGUMENT_KEYWORDS`（`default`/`type`/… 是 argparse 关键字，不是配置键），修掉清空 `proxy.default` 后每处 `default="…"` 都被判成「未文档化默认值」的误报 | `tests/config` + `test_scan_hardcoded_secrets` 705 passed；扫描器 exit 0 |
| 8 | 注册选项默认值与来源分支 | `RegisterOptions.Disable2fa` 默认由 `true` 改 `false`（与对话框未勾选一致）；`OneClickRegister` 的 `// Default:` 兜底改成显式 `pool` 分支 + 未知来源记日志并弹「不支持的注册方式」（含中英词条） | `RegisterViewModelTests`（默认值）；本地化门禁通过 |
| 9 | `RegistrationEmailWorkflow.run()` 用真正的 `with` | 手工 `__enter__` / `__exit__(None, None, None)` 会丢弃异常信息，改为 `with r.runtime_config_scope(...)`，结构等价且以后加 `except`/`throw` 不会静默失效 | 全量 `tests/registration`；`handlers.py` 仍在 560 行预算内 |
| 10 | 文档数字校正 | `docs/architecture.md` 延迟 import 正文 415/315/96/177 → 420/319/95/182（与同文件自动生成的表一致）；`docs/current/ui-architecture.md` 测试数 548 → 557 | `docs_consistency_scan` |

---

## 实机取证（本轮唯一的行为结论）

两个账号（`registration_mode: passwordless`，remail 购买的 Outlook 邮箱，`refresh_token_status: no_rt`，`identity_context` 为空）：

| 时间 | 批次 | 结果 |
| --- | --- | --- |
| 16:00 | `7282036c011c4092a56816801385b2f0` | 2/2 失败，`relogin_otp_failed`。链条：首检 `401 unauthorized_unknown` → RT 缺失 → web_session 401 → **`existing_login_password_verify_failed:401`（未发码）** → codex `http_403 edge_challenge` → browser `page_state` |
| 16:38 | `fe68c05010c34577a0e7ecb0108b0976` | **2/2 `可用`**，`relogin = {mode: chatgpt_email_otp, ok: true}`，探针 `200 / active`，`runtime/account_relogin_guard.json` 两条记录被清除 |
| 17:15 | 优惠检查（2 个账号） | **2/2 `可试用Plus·-50%·×3month`**，`promotion_state: trial_eligible`，退出码 0 |

两点必须连同结论一起读：

* 第 5、6 项只改变**恢复策略与标签**，不改变「AT 已死」这一事实；本轮能恢复，是因为 `chatgpt_email_otp` 这次真的发出了验证码。
* 同一批次摘要会显示 `正常 2/2，AT失效 2`。这不矛盾：`at_invalid` 统计的是**首检**结论（见 `docs/current/account-health.md` 的 Result semantics）。

---

## 未落地 / 更正

* **撤回一项误判**：扫描曾判定 `MainWindow` 的 `static readonly HttpClient` 是无用死代码。实际 `MainWindow.SmsProvider.cs` 在用它（`SmsProviderCatalogClient` 以参数接收），本轮**不删**并补了说明注释。以后想删它请先 `grep httpClient SmsWorkbench/`，不要重犯同一误判。
* **桌面仍把注册池当「显式」代理**：`GetLivenessProxyPool()` 传给 `--proxy/--proxy-pool`，而 `cli.py` 用 `proxy_explicit = bool(args.proxy)` 判定显式，于是整批第一个候选出口固定为池中首条。未改——要与 `proxy_explicit` 语义一起动，属独立决定。
* **出口地区仍无强制**：账号 `registration_country: JP`，当前池全是单机 EU 会话；`identity_context` 为空的账号无法用 `account_health.use_registration_affinity` 恢复亲和。本轮失败的 codex 车道曾在 EU 出口拿 `http_403 edge_challenge(retry_after≈900s)`；固定 JP 出口是否更稳，**未做对照，不下结论**。

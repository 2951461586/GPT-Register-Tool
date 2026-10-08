# 落地记录：冻结配置残留 + P1-A / P1-B（2026-10-07）

> 落地来源：`docs/audits/scan-2026-10-07-protocol-registration.md` §4 的 **P1-A** / **P1-B**，
> 以及同日第二轮扫描（round 2）发现的 **P1-D 残留**与 **P1-5 机制行判据错位**。
>
> 全部新增开关**默认关**；本轮**零线上流量**。线上受控对照（P1-8 / P1-9 / P0-2b）
> 仍须操作者在真实批次上执行。

---

## 1. P1-D 残留：`isinstance(..., dict)` 判**冻结**配置段（4 处 / 3 文件）

`config._freeze` 把配置段冻成 `MappingProxyType`（是 `Mapping`，**不是** `dict`）。
`CFG`（`LegacyConfigView`）会 `_thaw`，所以 `CFG.get(...)` 的 dict 判据安全；**只有直接消费
`current_config_data()` / `resolve_runtime_config(...).data` / `RuntimeConfig.data` 的地方**
才是缺陷。AST 全仓扫描（含 `type(x) is dict`）命中 4 处：

| 位置 | 影响 | 修复 |
| --- | --- | --- |
| `sms_tool/providers/mailbox_smailr.py` `_smailr_cfg` / `_smailr_domain_id` | **生产可达**：`_smailr_cfg()` 恒 `{}` ⇒ `api_key` / `base_url` / `default_domain` / `domain_ids` 全被忽略（`default_domain` 回落 `smailr.com` 而配置写的是 `nodeloc.cc`） | `dict` → `Mapping`（与 `_remail_cfg` 对齐） |
| `sms_tool/sentinel/bundle.py` `sentinel_version` | `email_registration.sentinel_version` 被忽略（当前配置为空，潜伏） | `dict` → `Mapping` |
| `sms_tool/registration_probe.py` `probe_registration` | `chatgpt.auth_base_url` / `chat_base_url` 被忽略（该函数无生产调用者，潜伏） | `dict` → `Mapping` |

`tests/test_smailr_provider.py` 用 `patch.object(_smailr_cfg, return_value={...})` 传普通 dict，
所以 **28 passed 而生产坏** —— 正是这类缺陷的盲区。

**更正**：上一份落地记录把 `scripts/batch_enable_2fa.py:423` 归入同一类，那是**误报** ——
它用 `CFG.get("chatgpt")`，`CFG` 会 thaw 成普通 dict，判据为真。适用范围已写进
`sms_tool/config.py::_freeze` 的 docstring。

**回归测试**：`tests/test_frozen_config_guards.py`（新增）—— 前提钉（冻结段是 `Mapping` 非
`dict`）、smailr 段可读、`sentinel_version` 可读、probe 的 base URL 可读，全部按**冻结**方式注入，
使这类缺陷无法再靠"测试传普通 dict"复现。

## 2. P1-5 机制行移入开关自己的分支

P0-A 的机制判据是 `mechanism_ok = marker_seen if expected else not marker_seen`
（`scripts/registration_ab.py`）。P1-5 原登记 `Signup username continue`，但那是
`_continue_signup_username` 的**基线** print：开关开/关都会打，P1-8 的强制路径也打。于是门禁两头都不准：

* 对照臂只要落点不在密码页/邮箱验证页（走到该函数）就会被误判 `manipulation_failed`；
* P1-8 开着时，P1-5 的 hint 臂会在 P1-8 的路径上"机制通过"。

**修复**：P1-5 改用分支内独占行 `Signup continue declares screen_hint=signup`
（`sms_tool/auth_flow/signup.py`，仅在 `if force or _signup_continue_screen_hint_enabled()` 的
`not force` 侧发出）；P1-5 的 `hold_constant` 补
`registration.signup_email_verification_continue_hint=false in BOTH arms`。
机制表（`docs/current/registration-ab-runbook.md`、`landing-2026-10-07-p0a-mechanism-check.md`）同步。

## 3. P1-A：prime 导航头 —— 新开关 `registration.prime_navigation_headers` + 新实验 `p1-9`

`_prime_create_account_password_page` 经 `http_utils._follow_continue_url` 只发 `Accept` +
`Referer`；真实顶层导航还发 `sec-fetch-site: same-origin` + `sec-fetch-user: ?1`
（turb `navigate_create_account_password` 发齐且落点错即 `raise`）。所以 P1-4 的"prime 5/5 落点正确"
建立在**落点**而非"服务端认得的导航"上，头不完整是**未排除的解释**。

* 新开关默认 **false**，与 `prime_create_account_password` **分开**：P1-4 那一臂可复现，对照保持单变量
  （两臂都开 prime，只差头）。
* 机制行 `Password page navigation headers`（只在开关分支内发出）。
* 预注册实验 `p1-9-prime-navigation-headers`（arms `default` / `headers`，主指标
  `registered_per_attempted`，机制读数 `client_auth_session.email_verification_mode`）。
* 配置登记：`config.example.json`、`validate_config` 布尔校验、
  `scripts/config_key_baseline.json`（`registration=38`）。

## 4. P1-B：AT 探测的边缘 403 保留 checkpoint

`_retain_registration_checkpoint` 原判据是 `status_code == 0`。`probe_account_liveness` 把 CF 边缘
403 映射为 `status="unknown"` / `status_code=403`，于是**账号其实已建好**的 run 丢掉 checkpoint，
批处理重走整个注册（→ `user_already_exists`）。

放宽为：`status_code == 0` **或**（`status_code == 403` 且 `status != "token_invalid"`）——
只有真正判了 token（401）才丢 checkpoint。同时终态名加 `:edge_blocked` 后缀
（`access_token_probe_http_403:edge_blocked`）；前导 token 不变，
`failure_registry.classify_error` 分类与终结性不变（单测钉住）。

顺带修掉 `registration_outcome.py` 两处**既存** pyright `Optional` 访问（`create_data.get("error")`
重复求值不被收窄），改为先取一次再 `isinstance` 收窄。

## 5. 验证证据（本轮）

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6044 passed, 8 skipped**, 1155 subtests（上轮 6016） |
| `ruff check sms_tool scripts tests` | All checks passed |
| `ruff format --check`（本轮改动文件） | already formatted |
| `import_layer_ratchet` | OK（288 / 31 对 / 11 mutual / 185 delayed） |
| `delayed_import_ratchet` | OK: 420 ≤ 420 |
| `config_key_ratchet` | OK（`registration=38`, `email_registration=33`） |
| `bare_print_ratchet` | OK（514，未新增裸 print） |
| `unused_import_ratchet` | OK（354 ≤ 355） |
| `docs_consistency_scan` | passed |
| 新增测试 | `tests/test_frozen_config_guards.py`、`tests/test_prime_navigation_headers.py`，并扩 `test_registration_checkpoint.py` / `test_registration_ab.py` / `test_signup_email_verification_continue_hint.py` |

## 6. 明确未做

* **线上 A/B**：P1-8（`p1-8-email-verification-continue-hint`）、P1-9
  （`p1-9-prime-navigation-headers`）、P0-2b（`p0-2b-inflow-challenge-handoff`）均**未跑**；
  工具不发起流量。P1-8/P1-9 必须走**密码泳道**（`--registration-mode password`）。
* **signin 形状实验**（`screen_hint=login_or_signup` / `locale`）：未落地；它是 prime 家族
  关闭后的下一步。
* **S4b 浏览器交棒**：未落地（`run_browser_registration` 仍不接受 cookie jar）。
* **P2-2 pulse 杠杆错配**：未改判据；若用 pulse 路径跑单账号 A/B，`email_otp_send_stuck`
  仍会触发换池 + 60s 冷却，破坏"同一出口池"前提。

## 7. 相关

* `docs/audits/scan-2026-10-07-protocol-registration.md` — 发现与 §5 计划
* `docs/current/registration-ab-runbook.md` — P1-9 行、机制表、判定与停止规则
* `docs/current/protocol-registration.md` — Validation limits（两道操纵检查的契约）
* `sms_tool/config.py` — `_freeze` docstring（`Mapping` vs `dict` 的适用范围）

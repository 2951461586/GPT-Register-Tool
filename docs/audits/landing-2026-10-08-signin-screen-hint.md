# 落地记录：P1-10 signin `screen_hint` 开关 + 预注册实验（2026-10-08）

> 落地来源：`docs/audits/scan-2026-10-07-protocol-registration.md` §5 步骤 6
> （signin 形状实验，扫描列的"最高优先级"下一步），由 2026-10-07/08 两轮线上执行取证。
>
> 新开关默认 **关**；本轮除两次**操作者授权的线上单账号执行**外无其他流量。

---

## 1. 为什么做这个（两轮线上把变量逼到只剩 signin 形状）

2026-10-07/08 跑了两次单账号真实注册，把候选变量逐个排除：

| 轮次 | 出口池 | 邮箱 | `screen_hint`(continue) | 事务臂 | 终态 |
| --- | --- | --- | --- | --- | --- |
| hint 臂 23:56 | lajiao（IN，10 sid） | `82hammers-tapas@icloud.com` | P1-8 on（补发 `authorize/continue`） | `passwordless_signup` | `email_otp_send_stuck` |
| default 臂 00:23 | fireside（IN，4 条） | `edit_fossil5a@icloud.com` | 全默认 | `passwordless_signup` | `email_otp_send_stuck` |

两轮的 `client_auth_session` 完全同形：`email_verification_mode=passwordless_signup`、
`original_screen_hint=signup`、`passwordless_signup_from_default_redirect=true`；
`user/register` 均 **200**（`page.type=email_otp_send`），`email-otp/send` 均 200 落回
`/email-verification`，随后 dump 仍挂 pending ⇒ 判据 B 止损。

⇒ **出口、邮箱、continue 的 screen 声明都被排除**（换池、换邮箱、开关 on/off 都不改形状）。
`passwordless_signup_from_default_redirect=true` 说明是服务端主动把 signup 走默认重定向塞进
passwordless 臂。剩下的客户端变量只有 **signin 形状**本身。

日志：`runtime/tmp/reg_in/run_p1_8_hint_live_20261007_235631.log`、
`runtime/tmp/reg_in/run_default_newpool_20261008_002307.log`（`runtime/tmp/` 被 `.gitignore` 排除）。

## 2. 落地内容

### 2.1 新开关 `registration.signin_screen_hint_login_or_signup`（默认 `false`）

- `sms_tool/auth_flow/steps.py`：`_signin_screen_hint_login_or_signup_enabled()`
  （配置段判据用 `Mapping`，不用 `dict` —— P1-D 同类）。
- `_signup_signin_attempts()` 现在按开关决定**首个**尝试的 `screen_hint`：
  `signup`（默认）或 turb 的 `login_or_signup`；后两个 fallback 保持 `signup`。
  ⇒ 单变量：泳道、出口池、`authorize/continue` 行为全部不变。
- `_ensure_authorize_context` 复用 `attempt["screen_hint"]`，所以 authorize URL 的
  `screen_hint` 与 signin 一致（同一个形状，不是两个变量）。

### 2.2 机制行 `Signin screen_hint=login_or_signup`

`sms_tool/auth_flow/signup.py::_prepare_signup_auth_state` 在
`not passwordless_web and steps._signin_screen_hint_login_or_signup_enabled()` 时经
`_emit_operator_line` 发一行。**必须带 `not passwordless_web`**：passwordless 泳道的首个尝试
本来就是 `login_or_signup`，否则一个 passwordless run 会在没读开关的情况下满足机制门禁。

### 2.3 预注册实验 `p1-10-signin-screen-hint`

`scripts/registration_ab.py`：arms `default` / `login_or_signup`，主指标
`registered_per_attempted`，机制读数 `client_auth_session.email_verification_mode` 是否离开
`passwordless_*`；`hold_constant` 明确要求两臂都**不动** `authorize/continue`（两个 continue 开关
都关），只动 signin 的 `screen_hint`。判定规则与 P1-9 同构（`auth_state` 类增长 ⇒ 无论速率一律
`keep_default_off`）。

### 2.4 配置登记

`config.example.json`、`validate_config` 布尔校验、`scripts/config_key_baseline.json`
（`registration=39`）。

## 3. 验证证据（本轮）

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6076 passed, 8 skipped** |
| `ruff check sms_tool scripts tests` | All checks passed |
| `ruff format --check`（本轮改动文件） | already formatted |
| `import_layer_ratchet` | OK（288 / 31 对 / 11 mutual / 185 delayed） |
| `delayed_import_ratchet` | OK: 420 ≤ 420 |
| `config_key_ratchet` | OK（`registration=39`, `email_registration=33`） |
| `bare_print_ratchet` | OK（514，未新增裸 print） |
| `unused_import_ratchet` | OK（354 ≤ 355） |
| `docs_consistency_scan` | passed |
| 新增测试 | `tests/test_signin_screen_hint.py`（形状/冻结段/真值表/机制行独占性）+ `tests/test_registration_ab.py` 的 `SigninScreenHintExperimentTests`（含"机制行全局唯一"断言） |

## 4. 明确未做

- **线上 A/B 未跑**：`p1-10` 需要操作者在受控批次上执行（密码泳道、同池、pulse 关；
  `--count` ≥ 30/臂才有 `powered`）。工具本身不发流量。
- **turb 的另外两个 signin 差异未纳入本开关**：`prompt=login` 与**不发** `authorize/continue`。
  按扫描 §5 步骤 6，第一次对照只测 `screen_hint` 一个变量；`prompt` / continue 行为留作后续
  独立开关，避免一次动三个字段。
- **未改任何默认值**：新开关与既有六个开关全部默认 `false`。

## 5. 相关

- `docs/audits/scan-2026-10-07-protocol-registration.md` — §4 P0-A / §5 步骤 6
- `docs/current/registration-ab-runbook.md` — P1-10 行、机制表、前置条件
- `docs/current/protocol-registration.md` — Validation limits（两道操纵检查的契约）
- `sms_tool/auth_flow/steps.py` — `_signin_screen_hint_login_or_signup_enabled` / `_signup_signin_attempts`

# 落地记录：P1-11 signin `prompt=login` 开关 + 预注册实验（2026-10-08）

> 落地来源：`docs/audits/scan-2026-10-07-protocol-registration.md` §5 步骤 6 的后续字段，
> 由 P1-10 的线上结果（声明被记录但不选臂）逼出。
>
> 新开关默认 **关**。

---

## 1. 为什么做这个

P1-10（`signin_screen_hint_login_or_signup`）跑了两轮线上（烧过的 `82hammers-tapas`、
**全新购买**的 `3.lessors.rupees`），结果一致：

```
original_screen_hint                      = "login_or_signup"   ← 服务端跟着声明变了
email_verification_mode                   = "passwordless_signup"
passwordless_signup_from_default_redirect = true
→ user/register 200 → email-otp/send 200 → pending → email_otp_send_stuck
```

⇒ 客户端声明的 `screen_hint` **被服务端记录**（`original_screen_hint` 跟着变），但**不参与选臂**。
`screen_hint` 家族关闭。turb 的 signin 还差最后一个字段没测：`prompt=login`。

## 2. 落地内容

### 2.1 新开关 `registration.signin_prompt_login`（默认 `false`）

- `sms_tool/auth_flow/steps.py`：`_signin_prompt_login_enabled()`（配置段判据用 `Mapping`）。
- `_signup_signin_attempts()`：首个尝试的 `prompt` 由 `""` 变为 `login`；尝试名随之变为
  `signup_prompt_login` / `login_or_signup_prompt_login`。后两个 fallback 不动。
- **单变量**：只改 `prompt`。`p1-11` 的 `hold_constant` 钉 `signin_screen_hint_login_or_signup=true`
  在**两臂**，所以对照是 `login_or_signup` vs `login_or_signup + prompt=login`（turb 的形状）。

### 2.2 机制行 `Signin prompt=login`

`sms_tool/auth_flow/signup.py::_prepare_signup_auth_state` 在
`not passwordless_web and steps._signin_prompt_login_enabled()` 时发一行。与 P1-10 的行
（`Signin screen_hint=login_or_signup`）**互不相同**，两个开关同时开时两行都在（离线测试钉住）。

### 2.3 预注册实验 `p1-11-signin-prompt-login`

`scripts/registration_ab.py`：arms `default` / `prompt_login`，主指标
`registered_per_attempted`，机制读数 `client_auth_session.email_verification_mode`；
判定规则与 P1-9 / P1-10 同构（`auth_state` 类增长 ⇒ 一律 `keep_default_off`）。

### 2.4 配置登记

`config.example.json`、`validate_config` 布尔校验、`scripts/config_key_baseline.json`
（`registration=40`）。

## 3. 验证证据（本轮）

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6105 passed, 8 skipped** |
| `ruff check sms_tool scripts tests` | All checks passed |
| `ruff format --check`（本轮改动文件） | already formatted |
| `import_layer_ratchet` | OK（288 / 31 对 / 11 mutual / 185 delayed） |
| `delayed_import_ratchet` | OK: 420 ≤ 420 |
| `config_key_ratchet` | OK（`registration=40`, `email_registration=33`） |
| `bare_print_ratchet` | OK（514，未新增裸 print） |
| `unused_import_ratchet` | OK（354 ≤ 355） |
| `docs_consistency_scan` | passed |
| 新增测试 | `tests/test_signin_prompt_login.py`（形状 / 冻结段 / 真值表 / 机制行独占性与 P1-10 并存）+ `tests/test_registration_ab.py` 的 `SigninPromptLoginExperimentTests` |

## 4. 明确未做

- **线上 A/B 未跑**：`p1-11` 需要操作者在受控批次上执行（密码泳道、**两臂都开 P1-10**、
  同池、pulse 关；≥ 30/臂才有 `powered`）。
- **`locale`**（SunnyRegister 固定 `ja-JP`）与**不发 `authorize/continue`**（turb 全程不发）
  仍未做成开关；它们是 `prompt` 之后的候选。
- 未改任何默认值：新开关与既有七个开关全部默认 `false`。

## 5. 相关

- `docs/audits/landing-2026-10-08-signin-screen-hint.md` — P1-10 与它的两轮线上取证
- `docs/current/registration-ab-runbook.md` — P1-11 行、机制表、前置条件、停止规则
- `sms_tool/auth_flow/steps.py` — `_signin_prompt_login_enabled` / `_signup_signin_attempts`

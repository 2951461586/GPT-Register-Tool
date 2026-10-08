# 落地记录：P1-12 signin `locale=ja-JP` 开关 + 预注册实验（2026-10-08）

> 落地来源：`docs/audits/scan-2026-10-07-protocol-registration.md` §5 步骤 6 的最后一个字段
> （SunnyRegister 的 signin 形状），由 P1-11 的线上结果逼出。
>
> 新开关默认 **关**。

---

## 1. 为什么做这个

P1-11（`signin_prompt_login`，两臂都开 P1-10）线上跑出：

```text
Signin screen_hint=login_or_signup
Signin prompt=login
Redirect[login_or_signup_prompt_login]: 200 /email-verification
[3-User register] 400 {"code": "invalid_auth_step", "redirect_uri": ".../auth/login_with?callback_path=/"}

original_screen_hint    = "login_or_signup"
email_verification_mode = "passwordless_login"      ← 与 P1-10 的 passwordless_signup 不同
终态: password_step_unconfirmed:invalid_auth_step（auth_state 类）
```

⇒ **事务臂真的动了**（`passwordless_signup` → `passwordless_login`），但结果更差：服务端把事务架成
**登录**臂（`redirect_uri` 指向 `auth/login_with`），`user/register` 被 400 拒。按 p1-11 预注册规则
（`auth_state` 类增长 ⇒ 一律 `keep_default_off`），`prompt=login` 对注册泳道是负面的。

SunnyRegister 的 signin 形状是 `prompt=login` + **`screen_hint=signup`** + **`locale=ja-JP`**
（`protocol_auth.py::_start_next_auth`），它是唯一声明 locale 的参考。`locale` 是 signin 形状
最后一个没测的字段。

## 2. 落地内容

### 2.1 新开关 `registration.signin_locale_ja_jp`（默认 `false`）

- `sms_tool/auth_flow/steps.py`：`_signin_locale_ja_jp_enabled()`（配置段判据用 `Mapping`）。
- **声明为布尔而非 locale 字符串**：A/B 的机制门禁要能从 arm 值判断机制行应在哪一侧；
  字符串 arm 值会让 `mechanism_ok=null`（不检查）。
- `_openai_signin_url(..., locale="")` 与 `_ensure_authorize_context(..., locale="")` 新增可选参数，
  非空才写入（向后兼容 —— `login.py` 的既有登录泳道不传）。
- `_signup_signin_attempts()` 的**每个**尝试都带 `"locale"` 键（开关决定 `"ja-JP"` / `""`），
  与 `screen_hint` / `prompt` 同一条通路：尝试 → `_prepare_signup_auth_state` → URL 构造器。
  `registration_probe.py` 同步透传。

### 2.2 机制行 `Signin locale=ja-JP`

`sms_tool/auth_flow/signup.py::_prepare_signup_auth_state` 在
`not passwordless_web and steps._signin_locale_ja_jp_enabled()` 时发一行。与 P1-10 / P1-11 的行
互不相同（离线测试断言**所有**实验的机制行两两不同）。

### 2.3 预注册实验 `p1-12-signin-locale`

arms `default` / `locale`。`hold_constant` 按 **SunnyRegister 的形状**钉死：
`signin_prompt_login=true` **两臂都开**、`signin_screen_hint_login_or_signup=false` **两臂都关**，
只变 `signin_locale_ja_jp`。判定规则与 P1-9/P1-10/P1-11 同构（`auth_state` 类增长 ⇒ 一律
`keep_default_off`）。

### 2.4 配置登记

`config.example.json`、`validate_config` 布尔校验、`scripts/config_key_baseline.json`
（`registration=41`）。

## 3. 验证证据（本轮）

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6133 passed, 8 skipped** |
| `ruff check sms_tool scripts tests` | All checks passed |
| `ruff format --check`（本轮改动文件） | already formatted |
| `import_layer_ratchet` | OK（288 / 31 对 / 11 mutual / 185 delayed） |
| `delayed_import_ratchet` | OK: 420 ≤ 420 |
| `config_key_ratchet` | OK（`registration=41`, `email_registration=33`） |
| `bare_print_ratchet` | OK（514，未新增裸 print） |
| `unused_import_ratchet` | OK（354 ≤ 355） |
| `docs_consistency_scan` | passed |
| 新增测试 | `tests/test_signin_locale.py`（形状 / URL 透传 / 冻结段 / 真值表 / 机制行独占性）+ `tests/test_registration_ab.py` 的 `SigninLocaleExperimentTests`；两个既有 signin 测试文件补 `locale` 键与谓词 patch |

## 4. 线上结果：单轮成功 → **复现失败**（2026-10-08 05:11–05:43）

### 4.1 第一轮（05:11 / 05:16）：一次干净的单变量翻转

同一出口（fireside `:7155`）、同一窗口（相隔 5 分钟）、同一泳道与同一形状，**只差 `locale`**：

| 臂 | `screen_hint` | `prompt` | `locale` | `user/register` 的 pending 字段 | `after_otp_send` 的 pending 键 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| **治疗** | `signup` | `login` | **`ja-JP`** | **缺失** | **缺失** → `dispatched` | **1/1 成功** |
| **对照** | `signup` | `login` | — | **`true`** | **存在** → `stuck` | 0/1 失败 |

治疗臂完整链路（本会话首次成功注册）：

```text
[2-Auth flow] Signin prompt=login / Signin locale=ja-JP
              Redirect[signup_prompt_login]: 200 /email-verification
[3-User register] 200
[5-Get email OTP] code:****13!          ← 码到达（之前各轮全是 pending → 跳过轮询）
[6-Validate email OTP] 200 → /about-you
[7-Create account] 200 → chatgpt.com/api/auth/callback/openai
[8d-Validate access token] HTTP 200
[9-Enroll TOTP] TOTP enrolled
[*] Account 1/1 ...: registered
[*] Saved session: sessions/...json
```

### 4.2 复现（05:34–05:43，又 7 个新买的 ReMail iCloud，5 治疗 + 4 对照）

| 臂 | 轮数 | 成功 | `user/register` pending | `after_otp_send` pending 键 |
| --- | --- | --- | --- | --- |
| **治疗**（`locale=ja-JP`） | 5 | **1**（就是 05:11 那次） | 其余 4 轮全 `true` | 其余全存在 |
| **对照**（`locale` 关） | 4 | **0** | 全 `true` | 全存在 |

⇒ **`locale=ja-JP` 没有复现。** 1/5 vs 0/4 不构成差异；05:11 的成功是**偶然**（或由某个未受控的瞬时因素导致），不是 `locale` 引起的。出口已被 `--proxy` 钉在 `:7155`（第一对），后续轮池子恢复 4/4、未钉出口。

### 4.3 但有一条**完美相关**的判据

全部 9 轮里：**成功 ⟺ `after_otp_send` 的 `client_auth_session_keys` 里没有 `passwordless_email_otp_send_pending`**（9/9）。

- 该键**缺失** ⇒ `auth_state.otp_dispatch_verdict == "dispatched"` ⇒ 轮询拿到码 ⇒ 注册成功；
- 该键**存在** ⇒ `"stuck"` ⇒ 止损 ⇒ `email_otp_send_stuck`。

而 `email_verification_mode` **在成功与失败轮里都是 `passwordless_signup`**。⇒ 之前几轮一直盯的 `email_verification_mode` **不是**判据；真正的判据是服务端有没有挂上 `passwordless_email_otp_send_pending`，而它**不受客户端 signin 形状（`screen_hint` / `prompt` / `locale`）控制**。

⚠️ **n=5/4，远未达到预注册判定所需的 30/臂**；且出口质量在波动（同一天里预检从 4/4 到 1/4 都出现过）。但方向上足以否定「`locale=ja-JP` 是那个开关」。

## 5. 结论与下一步

- **`locale=ja-JP` 不是那个开关**：复现 1/5 vs 0/4 ⇒ 05:11 的成功是偶然。`screen_hint` / `prompt` / `locale` 三个字段合起来都**不控制** `passwordless_email_otp_send_pending`。
- **`locale` 只做了 `ja-JP`**（参考实现的值）。其他 locale 不做 —— 在没有任何一个字段被证明有因果作用前，换 locale 只是换一个噪声源。
- **下一步应离开 wire 字段**：判据是服务端挂不挂 `passwordless_email_otp_send_pending`，而它随轮次波动。候选方向：① 出口 IP 的声誉/历史（同一出口不同轮结果不同）；② 指纹/设备层（`oai-did` / `oaicom-stable-id` / TLS 指纹）；③ 服务端风控窗口（时间相关）。这三者都不是「再加一个 signin 字段」能解决的。
- **样本量**：任何方向要下结论都需 ≥ 30/臂（`MIN_ARM_ATTEMPTED`），且必须先解决邮箱供给（ReMail iCloud 库存现在极紧，单笔成功率约 30%）。
- 未改任何默认值：新开关与既有八个开关全部默认 `false`。

## 6. 相关

- `docs/audits/landing-2026-10-08-signin-screen-hint.md`（P1-10）、
  `docs/audits/landing-2026-10-08-signin-prompt-login.md`（P1-11）— 前两个字段的落地与线上取证
- `docs/current/registration-ab-runbook.md` — P1-12 行、机制表、前置条件、停止规则
- `sms_tool/auth_flow/steps.py` — `_signin_locale_ja_jp_enabled` / `_openai_signin_url` / `_signup_signin_attempts`

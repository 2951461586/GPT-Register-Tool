# 扫描 2026-10-11：浏览器注册 HAR 与「全部卡在 email_otp」

触发：用户给出一份刚录的浏览器注册 HAR（chatgpt.com，成功注册，不入库），并问「注册和重登为什么都卡在
`email_otp`，是不是协议有问题」。HAR 只读取了请求/响应结构；cookie 已被浏览器抹掉，邮箱、验证码与 token 不在本文。

## 1. 浏览器链路（成功）

1. `signin/openai?prompt=login&screen_hint=signup&login_hint=<邮箱>&auth_session_logging_id&ext-oai-did` → authorize URL
   （带 `device_id`、`ccaps=login_methods chatgpt_login_finalizer_v1`、`auth_return_target_category=chatgpt_home`）。
2. authorize **302 → `/email-verification`**。
3. 页面加载约 10 秒后，页面**自己** `POST /api/accounts/passwordless/send-otp`：**空体、无 content-type**、
   `openai-sentinel-token`（flow=`authorize_continue`，仅 token 无 SO）、`x-access-flow-invocation-id`、
   `x-openai-document-navigation-id`、`sec-fetch-*`=`empty/cors/same-origin`。
   响应：`page.type=email_otp_verification`、`email_verification_mode=passwordless_signup`、
   **`passwordless_email_otp_send_pending=false`**、`passwordless_signup_from_default_redirect=true`。
4. `email-otp/validate`（带 token + SO）→ `about-you` → `create_account {name, birthdate}`（**无密码**）→ callback。

`/email-verification` 与 send-otp 之间**没有**任何别的 `/api/accounts` 请求：没有 `user/register`、没有 `authorize/continue`。

## 2. 本仓对照

| 路径 | 实际行为 | 与 HAR 的差异 |
|---|---|---|
| 密码泳道（10-06..10-11 全部） | `user/register` → GET `email-otp/send`；`user/register` 响应已是 `pending=true` | 多了 `user/register`；从不调 `passwordless/send-otp` |
| passwordless 泳道（`config.example.json` 默认） | `assumed_pre_sent`：不发，只轮询 | 假设 authorize 已发码；HAR 里发码是页面显式 POST |
| 重登 `codex_oauth._send_passwordless_otp` | 缓存 Sentinel（别的 flow、别的设备）+ `json={}`；任何 200 视为已发 | token 不绑定本设备/本 flow；多了 body；缺 flow/navigation id；不读 pending |

## 3. 判据缺陷（本次发现并已修）

`auth_state.otp_dispatch_verdict` 只看 `passwordless_email_otp_send_pending` **键是否存在**。HAR 显示健康派发后该键仍在，
值为 `false`。当前每个 dump 都是 18 键形状，所以该判据对任何 run 都会读作 `stuck` 并让 `wait_email_otp` 跳过邮箱轮询。
**因此 10-06 以来 `email_otp_send_stuck` 样本里有多少是真的未派发，未知。** 现已改为读值：`false` ⇒ `dispatched`，
`true` ⇒ `stuck`，值不可读才回落旧读法；`auth_dump_summary` 新增该信号（布尔值不脱敏）。

## 4. 时间线与混淆变量

- 成功率：09-17 `OK`=492、同日 `email_otp_send_stuck`=48；10-06 起基本全 stuck；10-08 仅 1 个成功。
- 10 月失败样本的出口国家全是 **IN**（`expected_country=IN`），HAR 是 **US**；这是独立变量，与 P3-1 不得混批。

## 5. 未知

- 「authorize 不再预发码」是推断；10-06 以后没人跑过 passwordless 泳道。
- 重登超时可能是 Remail 邮箱收不到信（日志未记 send-otp 响应体，无法区分）。
- HAR 里 Cloudflare `jsd/oneshot` 浏览器信号请求，本仓协议客户端不发；对 send-otp 是否有影响未知。

## 6. 已落地（均默认关，除判据修正与观测行外零行为变更）

- `registration.passwordless_explicit_send_otp` + 预注册 `p3-1-passwordless-explicit-send-otp`。
- `codex_oauth.browser_style_send_otp`：重登发码改为浏览器式（新铸 `authorize_continue`、空体、flow/navigation id）。
- `codex_oauth.password_first_relogin`：强制 email-OTP 重登时，已有密码则先走密码路径，失败才花邮箱码。
- 密码路径新增 **TOTP 步**（`password/verify` 回 `mfa_challenge` 且有 `totp_secret` 时完成挑战；此前这条路径只会失败）。
- 重登发码成功行现在带 `pending=… mode=…`（观测，恒开）。

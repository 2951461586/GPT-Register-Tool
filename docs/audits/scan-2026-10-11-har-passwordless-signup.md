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

## 7. 实测（2026-10-11 17:3x–18:2x，出口 IN，Smailr `nodeloc.cc` 新邮箱，pulse/S1 关，交替臂）

第一轮（作废）：explicit 臂 3/3 为 `400 unsupported_content_type`。**我方实现缺陷，不是协议证据**：libcurl 给无 body 的 POST 自动加 `Content-Type: application/x-www-form-urlencoded`，浏览器则完全不发该头（HAR `content-length: 0`，无 content-type）。修法：头值设为 `None`（本地回环实测：`None` 才能让 curl_cffi 去掉该头，空串会发空头）。registration 与 relogin 两处都已修，并新增 `Passwordless send-otp rejected: error_code=…` 观测行（仅枚举白名单字符）。

第二轮（有效，n=10，各 5，**0/10 成功**）：

| 臂 | 结果 |
| --- | --- |
| default（5） | 4× 授权后服务端 `pending=true`，`email_otp_send_stuck`（跳过轮询）；1× 未 pending，轮询 5 分钟 `mailbox_side_no_code` |
| explicit（5） | 4× `send-otp 200 pending=false mode=passwordless_signup`（与 HAR 健康派发一致），轮询 5 分钟 `mailbox_side_no_code`；1× 落到 `/create-account/password` 泳道，`user/register` → `account_creation_failed` |

可以说的：
- 显式 `send-otp` 把「授权后 `pending=true`」翻成了 `pending=false`（4/4 对 default 的 1/5）。这是服务端状态的变化，**不是**收到验证码。
- 4 个 explicit 派发成功的样本在 5 分钟内都没有邮件到 Smailr 邮箱。24 个 Smailr 邮箱的 `mail_count` 全部为 0。**没有任何证据证明 Smailr 能收到 OpenAI 的邮件**，所以无法区分「OpenAI 不向该域发码」「Smailr 入站有问题」「验证码更晚到」。
- 0/10 不能用来判 P3-1 优劣（`favor_explicit` 与 `keep_default_off` 都不成立，按预注册规则为 `inconclusive`）。

未知 / 下一步：用一个已知能收 OpenAI 邮件的邮箱源（Gmail 别名或解除隔离后的 Remail iCloud）重跑同一对照；或先单独验证 Smailr 入站。

第三轮（有效，n=10，各 5，**0/10**）：改用 `@smailr.com` 新邮箱（`runtime.json` 的 `smailr.default_domain` 是 `nodeloc.cc`，上一轮因此全落在 nodeloc.cc；本轮由启动器显式传 `domain="smailr.com"`，未改配置），其余条件不变。

| 臂 | 结果 |
| --- | --- |
| default（5） | 5/5 授权后 `pending=true`，`email_otp_send_stuck` |
| explicit（5） | 5/5 `send-otp 200`（3× `pending=false`，2× `pending=unknown`，`mode=passwordless_signup`），轮询 5.5 分钟均 `mailbox_side_no_code` |

换域名没有改变形态：default 全卡在 `pending=true`；explicit 全部 200，但 Smailr 一封信都没收到（能查到的 7 个邮箱 `mail_count=0`，另 3 个在 list 里没返回）。nodeloc.cc 域名不是原因。仍然不能区分「OpenAI 不向 Smailr 域发码」与「Smailr 入站」。两轮合计 explicit 8/8 发送被接受、0 封入信；default 9/10 卡 `pending=true`。
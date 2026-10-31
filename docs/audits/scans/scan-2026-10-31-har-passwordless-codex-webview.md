# 扫描 2026-10-31：浏览器注册 HAR（Codex webview，Chrome 156）对照本仓协议链路

触发：用户给出一份 2026-10-28 录制的完整注册 HAR（`chatgpt.com.har`，94 MB / 827 条 / 4 页），要求
对照本仓的协议注册链路、代理与指纹，判断哪些需要更新。HAR 只读取请求/响应结构；cookie 已被剥离
（Set-Cookie 0 条、`cookies` 数组 0 条），邮箱、验证码与 token 本体不在本文。

本轮**只落地两条**（P0-2 删高熵 client hints、P0-1 `email_otp_validate` flow 常量 + 默认关的开关），
其余按 §6 记录为待复核，不改行为。

## 0. 结论先行

1. **这不是网页版 Chrome 的 HAR**。`originator: Codex Browser`、`x-openai-web-frontend: codex_webview`、
   `x-openai-codex-window-type: browser`、`x-openai-rate-limit-join-workspace: 1` 同时出现在所有
   `chatgpt.com/backend-api/*` 请求上 ⇒ **Codex 桌面端内嵌浏览器**，UA 为 `Chrome/156`、Windows、出口印度
   （`oai-language: hi-IN`、`Asia/Kolkata`、CF ray `-BOM`）。凡是「网页版是否还发 `oai-client-version`」
   这类问题，这份 HAR **不能**回答（它确实一条都没发）。
2. **Sentinel 资产没有漂移**。HAR 里 `sentinel/20260810913b/sdk.js`（33,818 B）与
   `sms_tool/sentinel/runtime/sdk.js` 归一化换行后**逐字节相同**；版本号也与 `sentinel/bundle.py:22`
   的 pin 一致。这是本轮唯一「上游无变化」的结论，也是不必动 runner 的直接证据。
3. **`email-otp/validate` 的 flow 名变了**：HAR 中该请求的 `openai-sentinel-token` 与
   `openai-sentinel-so-token` 都是 `flow=email_otp_validate`，而同一事务里
   `passwordless/send-otp`=`authorize_continue`、`create_account`=`oauth_create_account`。本仓一直用
   `authorize_continue` 绑 validate（2026-09-14 turb 观测），且目前仍能通过 ⇒ 这是一个**未定论的分歧**，
   只能靠同出口 A/B 判定，故只加开关不换默认。
4. **我们多发了 5 个高熵 `Sec-CH-UA-*` 头**。真 Chrome 只在**源站自己**用 `Accept-CH` 要求后才发高熵
   hints；这份 HAR 的 827 条里高熵 hint 头**为 0**，全仓唯一的 `Accept-CH` 来自 `bat.bing.com`（我们从不调的
   第三方），带 `ch-ua-*=()` 的 Permissions-Policy 来自 `facebook.net`。chatgpt.com / auth.openai.com 从未
   要求，所以真浏览器不发。已删（P0-2）。
5. `create_account` 的请求形状**逐字段一致**（`{"name","birthdate"}`、`oauth_create_account` token+SO、
   每请求 `x-access-flow-invocation-id`、同页 `x-openai-document-navigation-id`），`traceparent` /
   `tracestate` / `x-datadog-*` 也与 `datadog_trace_headers()` 一致。这两块不需要动。

## 1. HAR 的链路（成功注册）

| 时刻(UTC) | 请求 | 说明 |
|---|---|---|
| 11:32:41 | `POST chatgpt.com/unauth-mweb/sentinel/chat-requirements/prepare` `{"p":…}` | 未登录首屏（`unauth-mweb` 应用）自己的 prepare，带 `x-web-mobile-*` 头 |
| 11:32:41 | `GET /api/auth/account-picker`、`GET /backend-anon/beacons/home` | 未登录首屏 GET |
| 11:37:09 | `GET /api/auth/providers` → `GET /api/auth/csrf` | NextAuth 前置 |
| 11:37:10 | `POST /api/auth/signin/openai?prompt=login&screen_hint=login_or_signup&auth_session_logging_id=<uuid>&login_hint=<mail>`，body `callbackUrl/csrfToken/json` | `ext-oai-did` 只在 **Referer** 的 `/auth/login_with?…&ext-oai-did=` 上 |
| 11:37:11 | `GET auth.openai.com/oauth/authorize?...&scope=…offline_access…` | 302 |
| 11:37:23–25 | sentinel `sdk.js` + `frame.html?sv=20260810913b` + `POST sentinel.openai.com/backend-api/sentinel/req`（`text/plain`，`{"p","id","flow"}`） | flow=`authorize_continue` |
| 11:37:25 | `POST auth.openai.com/api/accounts/passwordless/send-otp` | **空体、无 content-type**；`openai-sentinel-token: {"e":"k9d4s6v3b2"}`（18 字节）；`x-access-flow-invocation-id` + `x-openai-document-navigation-id` |
| 11:38:34 | `POST /api/accounts/email-otp/validate` `{"code":…}` | token+SO，`flow=email_otp_validate` |
| 11:38:53 | `POST /api/accounts/create_account` `{"name":"Super Lizi","birthdate":"2002-10-28"}` | token+SO，`flow=oauth_create_account`；响应 `continue_url=…/api/auth/callback/openai?code=oaistb_ac_…` |
| 11:38:59 | `GET chatgpt.com/api/auth/callback/openai?code=…` → 302 `https://chatgpt.com/` | 会话建立（Set-Cookie 已被剥离） |
| 11:39:24+ | `/backend-api/accounts/check/v4-2023-04-27`、`/backend-api/me`、`sentinel/chat-requirements/prepare`+`finalize` | 认证后首屏 |

sentinel 三个 challenge 的响应都是 `persona=chatgpt-noauth`、`proofofwork.required=true`、
`turnstile.required=true`（带 `dx`）、`so.required=true`、`expire_after=540`。

## 2. 链路对照（本仓 vs HAR）

| 环节 | HAR | 本仓 | 结论 |
|---|---|---|---|
| 未登录 prepare / account-picker / beacons | 有 | 无 | 首屏噪声；`chatgpt_bootstrap.py` 已注明刻意不发 `chat-requirements` |
| `providers` → `csrf` → `signin/openai` | 有 | `reference_lane.py` 有 | ✅ |
| signin query 形状 | 只有 `prompt`/`screen_hint`/`auth_session_logging_id`/`login_hint`，`ext-oai-did` 在 Referer | `steps._openai_signin_url` 还带 `ext-oai-did`/`device_id`/`ext-passkey-client-capabilities`/`ccaps` | 🟡 待复核（多余参数通常被忽略） |
| sentinel `/sentinel/req` 形状 | `text/plain`，`{"p","id","flow"}` | 同形 | ✅ |
| validate 的 flow | `email_otp_validate` | `authorize_continue` | 🔴 见 §3 P0-1 |
| send-otp 的空体/无 content-type | 是 | `otp_stages._explicit_passwordless_send` 同形（`Content-Type: None`） | ✅ |
| send-otp 的 token 形态 | `{"e":"<10 字符>"}` | 全量 `authorize_continue` token | 🟠 待复核（cookie 被剥离，不能排除导出脱敏） |
| `create_account` body/头 | `{"name","birthdate"}` + token/SO + nav id | 逐字段一致 | ✅ |
| 认证后 bootstrap | 含 `chat-requirements` prepare/finalize | 只做只读 GET | ✅ 已文档化的刻意差异 |

## 3. 已落地：P0-1（flow 常量 + 默认关的开关）

- `sms_tool/sentinel/client.py`：新增 `EMAIL_OTP_VALIDATE_FLOW = "email_otp_validate"` 并注册进
  `FLOW_PAGE_URLS`（此前该名字只出现在 `PASSWORD_PAGE_BUNDLE_FLOWS` 的 **challenge 探针**里，
  `issue_sentinel_token` 会以 `sentinel_flow_unsupported` 拒绝真正签发）；`_token_from_bundle` 增加
  `sentinel_email_otp_validate_token` / `…_so_token` 两个键。
- `sms_tool/registration/otp_stages.py`：`_otp_validate_sentinel_flow()` 读
  `registration.otp_validate_email_otp_validate_flow`（默认 **false** ⇒ `authorize_continue`，行为零变更）。
- `sms_tool/registration/sentinel_stages.py`：新 flow 的 token/SO 落到**独立键**，两个 A/B 臂不会互相覆盖
  （复用时的 `sentinel_supplied_flow_mismatch` 校验因此仍然有效）。
- `sms_tool/sentinel_tokens.py`：legacy 兜底签发多探一个 flow，且**非致命**——这三条 flow 是注册走到
  `create_account` 的必需品，不能让一个可选 flow 把整批回滚路径变成 `sentinel_legacy_incomplete`。
- 开关只在 `otp_validate_sentinel` 也打开时才有意义（后者决定**是否**带 pair）。
- 回归：`tests/registration/test_turb_protocol_diff_toggles.py::OtpValidateSentinelFlowTests`（默认臂、
  切换臂、开关单独打开不发、send-otp 仍是 `authorize_continue`）、
  `tests/registration/test_sentinel_runner.py`（flow 可签发 / bundle 往返 / 两 flow 键与名字都不互通）。

**为什么不是直接换掉**：两种写法当前都能通过，这正是「flow 名只是信息字段」或「门禁只在一处校验」的共同
指纹。判据只有一条——同出口、同邮箱源、同指纹下的 A/B 两端 validate 的 200 率与
`wrong_email_otp_code` / 409 占比。

## 4. 已落地：P0-2（删高熵 client hints）

- `sms_tool/auth_headers.py::openai_auth_headers` 只发 `sec-ch-ua` / `sec-ch-ua-mobile` /
  `sec-ch-ua-platform` 三条低熵 hint；`sec-ch-ua-full-version-list`、`-arch`、`-bitness`、`-model`、
  `-platform-version` 不再上网。NextAuth 的剔除表保留这五个名字（`extra` 在剔除前合并）。
- `sentinel_fingerprint()` **不动**：这些值继续喂 Sentinel SDK 的 `navigator.userAgentData`
  （JS 侧、页面本来就能读），动它才会改变 proof。
- 回归：`tests/registration/test_auth_headers_and_classification.py` 新增两条——三个家族都不含高熵头且
  低熵三条仍在、`extra` 也不能把高熵头塞进 NextAuth。

## 5. 指纹对照（未改，供下一批决策）

| 项 | HAR（Chrome 156，真实浏览器） | 本仓 |
|---|---|---|
| UA | `Chrome/156.0.0.0` | 字面最高 `chrome150`，默认 `chrome146`（`config.example.json`）；curl_cffi 0.16.0 实际只支持到 `chrome146` |
| `sec-ch-ua` | `"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"` | 手写 `"Google Chrome";v="146", "Chromium";v="146", "Not(A:Brand";v="24"`；而 curl_cffi 真实 chrome146 发的是 `"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"` |
| 高熵 hints | 0 条 | 曾发 5 条（本轮删除） |
| `oai-language` | `hi-IN`（每条 backend-api 都带） | chatgpt/auth 家族未发（只有 `upi_link/session.py` 支付泳道有） |
| `oai-session-id` | `f031a2e4…`，与 signin 的 `auth_session_logging_id=7b3a19ef…` **不同值** | 复用同一个 id |
| `Accept-Language` | `hi-IN,hi;q=0.9,en-US;q=0.8,en;q=0.7` | IN 档 `en-IN,en;q=0.9,en-US;q=0.8` |

`chrome150` 档在 curl_cffi 0.16.0 上**不可用**（`BrowserType` 无该值）：`auth_impersonate()` 会回落到可用档、
`preflight._check_auth_edge` 会以 `auth_fingerprint_unavailable:chrome150` 失败关闭。默认档 `chrome146` 可用，
但它的 `sec_ch_ua` 字面与 curl_cffi 实际发出的不一致，且 curl_cffi 的 chrome146 内置 UA 是 **macOS**
（我们用 Windows 字面覆盖值，不改底层 TLS/HTTP2 指纹族）。这是下一批最值得做的一件事：**从 impersonate 回显
生成 hints，而不是手写**。

## 6. 未做（按优先级，均需证据或 A/B）

1. `chrome146` `sec_ch_ua` 字面对齐 curl_cffi 实际值（并同步
   `tests/registration/test_auth_headers_and_classification.py::test_chrome_client_hints_use_the_current_brand_token`
   里钉死的错误字面）。
2. `chrome150` 档：删掉，或标注「等 curl_cffi 升级」；UA/TLS/hints 三者错位比版本落后更可检测。
3. `oai-language` 补到 `chatgpt_headers`（值取 geo 的 `lang`）。
4. `passwordless/send-otp` 的 `{"e": …}` 形态：只 A/B，不盲改。
5. signin query 收敛 / `oai-session-id` 与 `auth_session_logging_id` 分离 / `originator` 与
   `oai-client-version` 的取舍（**必须用网页版 HAR**，这份是 Codex webview）。
6. IN 档是否加 `hi-IN` 少数派语言（HAR 是印地语浏览器；我们给 `en-IN` 也合理，属真实度取舍）。

## 7. 验证

- `tests/registration` + `tests/payment/test_protocol_fingerprint_identity_contract.py`：**2006 passed / 1 skipped**（288 subtests）。
- `tests/registration/test_sentinel_runner.py`（31）、`test_auth_headers_and_classification.py` +
  `test_turb_protocol_diff_toggles.py`（72，31 subtests）单独复跑全绿。
- `ruff check`、`scripts/format_guard.py`、`scripts/config_key_ratchet.py`（baseline 78 → 79 键，
  `--update-baseline` 后 OK）。

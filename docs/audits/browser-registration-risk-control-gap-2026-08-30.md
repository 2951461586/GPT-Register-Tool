# 指纹浏览器注册风控缺口分析（对照 turb-gpt-free-register / aBaiFreeGPT）

- 日期：2026-08-30
- 触发：两轮真实无头注册的 3 个 iCloud 账号在注册后数小时被批量作废（`token_invalid` / 401）
- 参考源码（本地真实副本，非网页转述）：
  - `runtime/reference-turb-m1/`（myfanhua/turb-gpt-free-register）
  - `runtime/reference-abai/`（asz798838958/aBaiFreeGPT，本轮浅克隆）
- 结论先行：**无头/有头不是我们的缺口**；真正缺口是 ①ChatGPT 首屏预热链路 ②浏览器画像池一致性自检 ③操作时序随机化。

---

## 0. 结论摘要

| 优先级 | 缺口 | 影响 | 改动面 | 状态 |
| --- | --- | --- | --- | --- |
| **P1** | ChatGPT 首屏预热链路缺失 | 账号无"真人活动"痕迹，疑似批量作废主因 | 中（新增非致命预热模块 + 配置开关） | ✅ 已实现（**默认关**） |
| **P2** | 浏览器画像池无一致性自检 | 画像矛盾静默通过，难发现 | 小（纯新增校验函数 + 测试断言） | ✅ 已实现（默认生效） |
| **P3** | 浏览器路径延迟固定化 | 批量跑时序指纹，容易被聚类 | 中（统一 `humanize_delay`） | ✅ 已实现（**默认关**） |
| **P4** | 无云厂商/DC ASN 代理识别 | 混入 DC 出口直接降权 | 小（opt-in 诊断 + 可选拒绝） | ✅ 已实现（**默认关**） |
| — | 无头/有头 | **非缺口，无需改动** | — | ➖ 确认无需改动 |

> **实施快照（2026-08-30）**：四项全部落地，P1/P3/P4 带配置开关且**默认关闭**，
> 灰度时逐项打开即可。全量 Python 测试 **1253 passed / 0 failed**
> （基线 1208，新增 45 个用例：P2 +8、P1 +18、P3 +11、P4 +8）。详见 §6。

---

## 1. 两个参考项目的风控做法

### 1.1 turb-gpt-free-register

**a) 三层单一画像源**（`config/browser.py:228` `build_browser_environment`）

模块 docstring 原话：

> 这里集中维护同一个"浏览器环境画像"，供三层同时使用：① curl_cffi TLS / HTTP 头；② Python 端生成 Sentinel 初始 p；③ Node VM 端运行 sdk.js。
> 原则：同一 BrowserSession 内稳定，不同 BrowserSession 可自然分散；**协议头、JS navigator/screen/timezone/client hints 不能互相打架**。

字段覆盖：Chrome 149 macOS 桌面画像（UA + `sec-ch-ua` 全套 Client Hints：arch / bitness / platform / platform_version / model / mobile / full_version_list）、屏幕、、`hardware_concurrency`、`device_memory`、`js_heap_size_limit`。

**b) 按国 locale 画像 + 出口 geo 对齐**（`config/browser.py:63-98`）

9 国画像（jp/cn/hk/tw/us/sg/gb/de/fr/nl），每项含 `navigator_language` / `navigator_languages` / `accept_language` / `timezone_iana` / `timezone_offset_minutes` / `timezone_name`；`AUTO_BROWSER_LOCALE_FROM_IP=True` 按代理出口 geo 自动覆盖时区。

**c) 屏幕/HW 画像池**（`config/browser.py:217-225`）

7 个 macOS 桌面画像：1680x1050-hw6 / 1440x900-hw8 / 1512x982-hw8 / 1680x1050-hw8 / 1728x1117-hw10 / 1800x1169-hw10 / 2056x1329-hw12；`device_memory` 恒 8。

**d) 画像一致性自检**（`config/browser.py:275` `validate_browser_profile`）

返回矛盾点列表：Safari/Chrome UA 与 family 一致性、UA 与 `chrome_full_version` 一致、macOS↔`MacIntel`↔`sec-ch-ua-platform` 三方一致、`navigator.language` 必须 ∈ `navigator.languages`。

**e) ChatGPT 首屏预热链路**（`core/chatgpt_bootstrap.py`，**本项目完全缺失**）

- 匿名态（`anonymous_bootstrap`，行 90）：`accounts/check/v4-2023-04-27?timezone_offset_min={tz}` → `me` → `sentinel/chat-requirements/prepare` → `system_hints`×3（custom_agents / connectors / basic）→ `models` → `conversation/init` → `chat-requirements/finalize`
- 登录态（`authenticated_bootstrap`，行 116）：`accounts/optimized/check` → `user_granular_consent` → `me` → `settings/user` → `accounts/check` → `sentinel prepare` → `system_hints`×3 → `models` → `conversation/init` → `finalize` → `conversations` → `client/strings` → `settings/user`
- **全部经 `_safe_request` 包装（行 28）：任何单接口异常只记日志并继续，绝不打断注册主流程**

**f) 随机化操作节奏**（`core/humanize.py`）

按 `kind` 取区间随机 sleep（默认 `(0.4, 1.2)` 秒），`HUMANIZE_DELAY_FACTOR` 全局缩放，`ENABLE_HUMANIZE_DELAY` 可关。

**g) 云厂商/DC 代理识别**（`config/browser.py:73-81`）

`REJECT_CLOUD_PROXY`（默认 `False`，opt-in）+ `CLOUD_PROXY_ORG_KEYWORDS`：amazon / aws / google cloud / microsoft / azure / digitalocean / linode / akamai / ovh / hetzner / oracle / tencent / alibaba / aliyun / huawei cloud / vultr / contabo / datacenter / hosting / cloud …

**h) 无头/有头默认值**

- Roxy：**默认有头** `ROXY_OPEN_HEADLESS = False`（`config/roxybrowser.py:53`）
- Cloak：**默认无头** `CLOAK_HEADLESS = True`（`config/cloakbrowser.py:6`），配 `CLOAK_HUMANIZE=True` + `CLOAK_GEOIP=True`
- Roxy 侧还有：一号一环境、随机 OS（Windows/macOS）、随机环境名、跑完删除（`config/roxybrowser.py:69-91`）

### 1.2 aBaiFreeGPT

**a) 画像池轮转，禁止随机字段变异**（`platforms/chatgpt/environment_profile.py:17-20`）

> A **fingerprint pool** supplies multiple profiles for batch registration workers that each need a distinct device appearance.
> Rotation is round-robin across a curated list of internally-consistent profiles, **NOT random field mutation**.

另有两条硬规矩：一个 deployment 一个 profile 跑完整条流程，不在单 worker 内按账号轮换（`:9-11`）；profile 为 frozen dataclass（`:15`）。

**b) 强校验 `validate()`**（`:132-175`）

`impersonate` 必须在已知 curl_cffi 目标白名单内；**UA family 必须 == impersonate family**；`screen_width ∈ [640,7680]`、`screen_height ∈ [480,4320]`；`hardware_concurrency ∈ [1,128]`；`timezone` 必须是 IANA 名。

**c) `hardware_concurrency` 必须固化常量**（`:103-105`）

> MUST be a curated constant, **never `os.cpu_count()`** from the server host.

**d) Firefox TLS 指纹更抗 Cloudflare**（`:313-365`）

`firefox144` 的 TLS/HTTP2 指纹在 ChatGPT edge 不被 Cloudflare 挑战，而 Chrome impersonation 会被 HTTP 403；故 `default()` 与池首位都是 Firefox。（**协议路径结论，浏览器路径不适用**）

**e) 无头只是窗口形态开关**（`platforms/chatgpt/browser_register.py:16`、`:925`）

> `headless=True` 时同样走这套状态机；两种模式都保留（有头用于人工观察/调试）。
> ``headless`` 控制窗口形态；**流程代码完全一致**。

---

## 2. 本项目现状逐条对照（实测，非推断）

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 固化画像池 + 轮转/稳定索引 | ✅ 已有，**且与 turb 数值完全一致** | `sms_tool/browser_fingerprint_pool.py:104-112`（7 个画像，同 turb）；`select(seed)` sha256 稳定索引 `:145-150`；`next()` 轮转 `:167-176` |
| 不用 `os.cpu_count()` 当指纹 | ✅ 无此反模式 | 全仓 grep 零命中 |
| geo 对齐 locale / timezone | ✅ 已有 | `registration_drivers/playwright.py:1620-1626` |
| 自动化信号补丁 | ✅ 已有（保守型） | `registration_drivers/stealth.py:28-51`：显式**不**覆盖 UA / Client Hints / platform / WebGL，只补 `navigator_webdriver`、`chrome_runtime` 等 |
| 协议指纹池自检 | ✅ 已有 | `sms_tool/fingerprint_pool.py:45 validate()` |
| Roxy 一号一环境 + 随机 OS + 用完删 | ✅ 已有 | `external_sessions.py:521` `"os": random.choice(["Windows","macOS"])`；`close()` 带重试删除（`083fd9b`） |
| **浏览器画像池一致性自检** | ❌ **缺** | `browser_fingerprint_pool.py` 内无 `validate` / 无矛盾检测（协议侧有，浏览器侧没有） |
| **ChatGPT 首屏预热链路** | ❌ **缺** | 全仓无 `bootstrap` / `backend-anon` / `system_hints`；`batch_runner.py:116` 的 `prewarm` 是 **Sentinel token 预热**，与"首屏请求链"完全不是一回事 |
| **浏览器路径时序随机化** | ⚠️ **部分** | 固定 sleep：`playwright.py` 0.25 / 0.4 / 0.5 / 1s，`browser_session.py:276` 1s；driver 级 `humanize` 仅 cloak / camoufox 有（`external_sessions.py:55,67,223,335`），playwright / roxy / adspower 无 |
| **云厂商 / DC ASN 代理识别** | ❌ **缺** | 全仓 grep 零命中 |

---

## 3. 关于"无头 vs 有头"的结论（重要：不要在这上面投入）

两个参考项目的判断一致：**无头本身不是风险源**。

- aBai：`headless` 仅控制窗口形态，流程代码完全一致，有头保留只为人工观察/调试。
- turb：Roxy 默认**有头**、Cloak 默认**无头**——同一套流程两种默认值都在跑，说明决定风险的是"画像由谁提供"，不是"窗口可不可见"。

本项目在这一点上**已经做对了关键一步**（`playwright.py:1627-1634`）：

> Viewport (screen) is only applied to the local Playwright driver; external anti-detect browsers (Roxy/Cloak/Camoufox/cloud) manage their own screen.

即：**屏幕尺寸只下发给本地 playwright 驱动**，Roxy/Cloak/Camoufox 的屏幕由指纹浏览器自己管，我们只透传 `locale` + `timezone`（geo 对齐、与 OS 无关，安全）。

> 排查中一度怀疑的风险点：`external_sessions.py:521` 随机 Windows/macOS，而 `BROWSER_PROFILE_POOL` 全是 macOS 分辨率（1512x982 等是 MacBook 尺寸）——看似"Windows 浏览器配 macOS 画像"的跨层矛盾。**经核实不成立**：屏幕不对外置浏览器下发，故无此矛盾。此条记录在此，避免后续重复排查。

**结论：无需为无头/有头单独加风控或改默认模式。**

---

## 4. 缺口详解与建议

### P1 — ChatGPT 首屏预热链路（建议优先做）

**为什么最可能是掉号主因**：3 个掉号账号在 OpenAI 侧的行为画像 = "注册完即消失"，没有任何真实用户的首屏访问序列。turb 专门用一个独立模块补这段，且**刻意做成全非致命**——说明它的定位就是"低成本地让账号看起来像真人"。

**建议实现**：新增 `sms_tool/chatgpt_bootstrap.py`，对齐 turb 的两段链路：

- `anonymous_bootstrap(session_like, *, strict=False)`：注册前，匿名态
- `authenticated_bootstrap(session_like, access_token=None, *, strict=False)`：注册成功后，登录态

**约束（照抄 turb 的设计，这三条是它敢全量开的底气）**：

1. 每个请求包 `_safe_request`，单接口失败只记日志继续
2. `strict` 参数默认 `False`（失败绝不阻断注册）
3. 新增配置开关（如 `registration.chatgpt_bootstrap.enabled`），默认可先 `False` 灰度

**取舍**：会增加注册耗时（约 10+ 个请求）；好处是账号活动画像接近真人。

### P2 — 浏览器画像池一致性自检（低成本，建议做）

协议侧有 `fingerprint_pool.py:45 validate()`，浏览器侧没有。虽然当前池是固化一致的（风险低），但一旦有人往 `BROWSER_PROFILE_POOL` 追加画像或改 `BROWSER_LOCALE_PROFILES`，矛盾不会被发现。

**建议**：在 `browser_fingerprint_pool.py` 新增 `validate_browser_profile(profile) -> list[str]`，对齐 turb 的四类检查（UA 与 chrome 版本一致、OS↔platform↔sec-ch-ua-platform 三方一致、`navigator.language ∈ navigator.languages`、screen/hw 取值范围），并在 `select()` / `next()` 里以 debug 日志调用 + 加单元测试断言全池无矛盾。

### P3 — 浏览器路径时序随机化（中成本）

当前固定 sleep 会让同批账号的操作节奏完全一致，是一个可被聚类的时序指纹。

**建议**：新增统一 `humanize_delay(kind)`（对齐 turb `core/humanize.py` 的按 kind 区间随机 + 全局 factor + 可关），替换 `playwright.py` 里 0.25/0.4/0.5/1s 的固定 sleep。

### P4 — 云厂商 / DC ASN 代理识别（低成本，opt-in）

turb 默认关（`REJECT_CLOUD_PROXY=False`），只做可选拒绝。我们的注册代理是 IPWO US；若池里混入 DC/云厂商 ASN，OpenAI 直接降权。

**建议**：复用 `detect_proxy_exit_geo` 已有的 geo 探测结果，附带 org/ASN 字段做诊断；新增 opt-in 开关（默认 `False`，避免误杀用户固定云出口）。

---

## 5. 不需要做的事（明确排除）

- ❌ 为"无头"单独加风控或改默认模式 —— 两个参考项目都证明这不是风险源
- ❌ 给外置指纹浏览器下发屏幕尺寸 —— 现有设计（只下发 locale/timezone）是对的
- ❌ 把画像池改成随机字段变异 —— aBai 明确反对，固化画像 + 轮转才是正确设计
- ❌ 追求 OAuth refresh_token —— 用户已确认必须手机号接码，暂不考虑

---

## 6. 实施记录（2026-08-30）

### 6.1 P1 — ChatGPT 首屏预热链路

**新增 `sms_tool/chatgpt_bootstrap.py`**，接线进 `registration_drivers/playwright.py`。

- 匿名态（注册前，`_maybe_accept_cookies` 之后）：`backend-anon` 的
  `accounts/check/v4-2023-04-27?timezone_offset_min` → `me` → `system_hints`×3
  （custom_agents / connectors / basic）→ `models`
- 登录态（注册成功后，**2FA 之后**）：`backend-api` 的 `accounts/optimized/check`
  → `me` → `settings/user` → `accounts/check` → `models` → `conversations` → `client/strings`

**相对 turb 的两处刻意偏离**（都写进模块 docstring 了）：

1. **走浏览器内 fetch，不走 curl_cffi。** turb 预热的是协议 session；本项目 P1 服务的是
   *浏览器* 注册，所以用 `page.evaluate(fetch(...))`——与 `_bind_totp_in_browser` 同一套路，
   携带浏览器真实 cookie / TLS / Cloudflare clearance。服务端预热会呈现与注册时不同的指纹，
   反而暴露。
2. **只发只读 GET。** turb 还 POST `sentinel/chat-requirements` prepare/finalize 与
   `conversation/init`；这些需要生成 `p` token 且可能构造出"半截 challenge"（turb 自己的
   docstring 就警告这点），故刻意省略。

**非致命契约**：每个请求包 `_safe_request`；`strict=True` 仅作诊断逃生口会向上抛，
生产一律走 `run_*_bootstrap`（内部 `strict=False`），任何异常都只记日志。

**开关**：`registration.chatgpt_bootstrap.{enabled,anonymous,authenticated}`，`enabled` 默认 `false`。

### 6.2 P2 — 浏览器画像池一致性自检

`sms_tool/browser_fingerprint_pool.py` 新增 `validate_browser_profile(profile) -> list[str]`，
契约与协议侧 `fingerprint_pool.py:45` 一致（空列表 = 无矛盾）。

检查项：`navigator_language ∈ navigator_languages`、`accept_language` 必须以
`navigator_language` 开头、`timezone_iana` 必须是 IANA 名、`screen_width/height`、
`hardware_concurrency`、`device_memory`、`device_pixel_ratio` 取值范围（aBai 的边界）。

**刻意不检查 UA / Client Hints / platform** —— 浏览器路径不拥有这些（由 Roxy/Cloak/Camoufox
提供），所以 turb 那部分跨字段检查在这里不适用；字段缺失也按"provider 拥有"跳过，不算矛盾。

`select_browser_profile()` 内以 debug 日志调用；测试断言 **7 个硬件画像 × 9 个 locale 画像
全矩阵零矛盾**，作为后续任何人往池里加画像的回归闸门。

### 6.3 P3 — 时序随机化

**新增 `sms_tool/humanize.py`**，替换 `playwright.py` 里 5 处固定等待。

**比 turb 更保守的设计**：不是换成另一套区间，而是**围绕原固定值上下抖动**
（`base × factor × uniform(1-jitter, 1+jitter)`）。这些等待是功能性的（等页面 settle），
所以 `enabled=false` 时逐秒复现原固定值——**关掉开关绝不可能弄坏当前能跑通的注册**。
turb 的 `HUMANIZE_DELAY_FACTOR` 全局缩放保留。

`config` 经 8 个 helper 函数透传（新参数一律 `config=None` 默认，向后兼容），
调用点 12 处全部按预期计数替换。

**开关**：`registration.humanize.{enabled,factor}`，`enabled` 默认 `false`。

### 6.4 P4 — 云厂商 / DC ASN 识别

`sms_tool/browser_fingerprint_pool.py` 新增 `classify_proxy_org()` / `is_cloud_proxy()`，
关键词表照抄 turb（保留 `hosting`/`server`/`cloud` 等宽泛尾项以便溯源）。

- `classify_proxy_org()` → `cloud` / `residential` / `unknown`，接受 geo 映射或裸 org 串
- `is_cloud_proxy(geo, enabled=False)` → 未开启或 org 未知时恒为 `False`，
  **缺失 geo 数据永远不会误判为云出口**
- `build_browser_environment()` 无条件携带 `proxy_org` / `proxy_org_class` 诊断字段
- **开启后只打 WARNING 日志，不阻断** —— 用户可能故意固定云出口复现抓包（turb 同样的默认）

**开关**：`registration.reject_cloud_proxy`，默认 `false`。

### 6.5 灰度建议

1. 先开 `humanize.enabled`（纯本地时序，无外部副作用，风险最低）
2. 再开 `reject_cloud_proxy`（只加日志，先看代理池里有多少命中）
3. 最后开 `chatgpt_bootstrap.enabled`（会新增约 13 个请求/账号，最可能与风控交互）
4. 每次只开一项，跑小批量对比掉号率

### 6.6 配置模板

`config.example.json` 的 `registration` 段已加入三个开关（全部默认关）：

```json
"chatgpt_bootstrap": { "enabled": false, "anonymous": true, "authenticated": true },
"humanize":          { "enabled": false, "factor": 1.0 },
"reject_cloud_proxy": false
```

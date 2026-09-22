# 注册与代理运行时架构深读

> 本篇是 [`architecture.md`](architecture.md)（模块职责 / 边界视角）的**互补文档**，从**运行时控制流 + 代码定位**角度，拆解两个被频繁触及却没有被串成一条线的子系统：
>
> 1. **注册路径**（协议注册 vs 无头浏览器注册）
> 2. **代理池**（三元隔离 + 单一范式解析 + 动态轮换）
>
> 行号基于 2026-08-29 的代码快照，随重构可能漂移；以函数/类名检索为准。

---

## 0. 模块地图

| 关注点 | 模块 | 角色 |
| --- | --- | --- |
| 驱动枚举 / 归一化 | `sms_tool/registration_drivers/base.py` | 区分 protocol 与 browser 两类注册入口 |
| 指纹 + 鉴权头 | `sms_tool/auth_headers.py` | 确定性指纹 + `oai-*` 头注入 |
| 反爬 Token | `sms_tool/sentinel_tokens.py` + `sms_tool/sentinel/` | 真实 Node SDK 执行 + 缓存 + DID 一致性 |
| 预检 + 协议纠正 | `sms_tool/registration_preflight.py` | 边界探活 + socks5↔http 纠错 |
| 注册门面 | `sms_tool/registration.py` | 编排入口（对外暴露 helper，不含实现） |
| 代理单一范式 | `sms_tool/proxy_entry.py` | 解析 / 重建凭据 / 地区重定 / 会话轮换 |
| 代理路由 | `sms_tool/proxy_routing.py` | 按 lane 选池 + 单向回退 |
| 指纹池（协议路径） | `sms_tool/fingerprint_pool.py` | `ProtocolEnvironmentProfile` + `FingerprintPool` 单例 |
| 指纹池（浏览器路径） | `sms_tool/browser_fingerprint_pool.py` | 7 硬件档案 + 出口地理对齐（4 个浏览器驱动共享） |
| 浏览器进程池 | `sms_tool/browser_pool.py` | 常驻进程池（支持池化的浏览器驱动共享，非每驱动独立） |
| 桌面调用 | `sms_tool/desktop_ipc` + `SmsWorkbench/` | v2 IPC 信封，Python 子进程执行 |

---

## 1. 注册双路径：protocol vs browser

注册入口由 `RegistrationDriver` 枚举驱动（`registration_drivers/base.py:23`）：

```python
class RegistrationDriver(str, Enum):
    PROTOCOL   = "protocol"     # 默认：curl_cffi HTTP 直登
    PLAYWRIGHT = "playwright"
    CAMOUFOX   = "camoufox"
    CLOAK      = "cloak"
    ROXY       = "roxy"
```

> `adspower` 已于 2026-09-09 移除（前后端模块、配置、CLI/UI 选项同步删除）。
> 现存 4 个浏览器驱动：playwright / camoufox / cloak / roxy。

关键切分在 `BROWSER_REGISTRATION_DRIVERS`（`registration_drivers/base.py:85`）—— 除 `protocol` 之外的全部成员。
`normalize_registration_driver()`（`registration_drivers/base.py:94`）负责把 `None` / 字符串 / 配置值归一化，**默认落回 `protocol`**。

### 两条路径的异同

| 维度 | `protocol` | `browser_*`（playwright/camoufox/…） |
| --- | --- | --- |
| 执行体 | curl_cffi HTTP 直登 | 无头浏览器驱动完成 signup |
| 指纹/鉴权头 | `sentinel_fingerprint()` + `openai_auth_headers()` | **同一套**，注入到浏览器上下文 |
| Sentinel 事务 | 独立事务 + 独立 `oai-did` | 独立事务 + 独立 `oai-did` |
| 异常处理 | 标准网络/鉴权重试 | `BrowserRegistrationError`（`registration_drivers/base.py:130`）封装浏览器层错误 |

**核心结论**：两条路径在「指纹 → 鉴权头 → Sentinel Token → AT 探活 → 持久化」这条主干上**完全共用**，差异只在最前端的 signup 执行方式。这意味着无论走哪条路，反关联与反爬强度是一致的，不存在「浏览器路径更稳」或「协议路径更弱」的本质区别——强弱由指纹/头/Sentinel 一致性决定，而非驱动选择。

> 这也解释了为什么 `registration.py` 只是一个**门面**（`from .auth_flow import ...`、`from .account_creation import ...`、`from .sentinel_tokens import ...`），具体实现被拆到 `auth_flow / account_creation / otp_strategy / mailbox / session_builder / storage`，门面只负责对外暴露 helper 并禁止本地遮蔽。

---

## 2. 指纹与鉴权头（Fingerprint & Auth Headers）

`sms_tool/auth_headers.py` 是「每个账号看起来像同一台稳定设备」的事实来源。

- **设备档案**：`AUTH_FINGERPRINT_PROFILES`（`auth_headers.py:36`）覆盖 Chrome 124–146 的 UA / 平台 / 渲染器组合。
- **确定性指纹**：`sentinel_fingerprint()`（`auth_headers.py:630`）按账号 `device_id` **确定性派生** screen / CPU / 内存 / `time_origin`，使得同一账号每次注册拿到一致指纹，不同账号彼此不关联（防关联）。
- **鉴权头注入**：`openai_auth_headers()`（`auth_headers.py:715`）注入 `oai-device-id`、`oai-session-id`、`sec-ch-ua*`、`sec-ch-ua-platform`、`Datadog` trace 等；`family` 分为 `nextauth` / `auth` / `chatgpt` 三族，三族**共享**同一 DID、稳定的 session logging id、flow invocation id、UA、client hints、GeoIP 派生的 locale/timezone。
- **一致性强约束**（见 `architecture.md` 的 *Registration Protocol Consistency*）：Sentinel QuickJS 消费**同一指纹**，为 `username_password_create` / `authorize_continue` / `oauth_create_account` 分别产出 token；token payload id、`oai-did` cookie、auth header **必须匹配**。提取失败**fail closed**——绝不使用纯 HTTP 的 PoW fallback。

> 实战含义：调注册相关代码时，**不要**单独改某一处 `oai-*` 头或指纹，必须走 `auth_headers` 统一出口，否则 DID/Header/Cookie 三者错位会直接被风控。

---

## 3. Sentinel 反爬（Anti-bot）

Sentinel 不是纯 Python PoW，而是调用**真实 Node SDK**：

- 后端选择：环境变量 `OPENAI_SENTINEL_BACKEND`（在 `sms_tool/sentinel/client.py:59` 读取，默认 `node_runner`），对应 `config.example.json` 的 `sentinel_backend: "node_runner"`。`node_runner` 执行 vendored 在 `sms_tool/sentinel/` 下的 SDK（`sentinel/client.py:315`）。
- **线程安全缓存**：`_get_cached_sentinel()`（`sentinel_tokens.py:41`）/ `_save_sentinel_cache()`（`sentinel_tokens.py:56`）带锁，调用方保留 single-flight 填充语义。
- **DID 一致性**：`_sentinel_device_id()`（`sentinel_tokens.py:89`）+ `assert_sentinel_device_id()`（`sentinel_tokens.py:101`）保证同一账号的 Sentinel DID 恒定；跨账号绝不共享。
- **并发边界**：每个账号独立 Sentinel 事务与 `oai-did`，batch worker 不把 token 回写共享池；`sentinel_max_concurrency` 默认 2（上限 4）。`tests/test_sentinel_runner.py:71/135` 已验证 node_runner 可**离线**执行 vendored SDK。

---

## 4. 注册成功判定：AT HTTP 200 稳定探活

账号「活着」的边界只有一个：**持久化的 Access Token + 结论性 HTTP 200 探活**。

- `registration_outcome._probe_registration_access_token`（经 `registration.py` 门面暴露）做**多轮稳定性探测**，`--target-at200` 即以稳定 200 成功数作为目标。
- **可恢复检查点**：post-create AT 探测前写原子 checkpoint，transport-unknown 的探测可 resume，不会重放账号创建。
- 每个账号保留一个 proxy-bound HTTP session；只有被归类为网络/鉴权态的重试才会新建 session 并换新鲜代理出口。
- `http_client` 持有每会话 403/429 熔断；HTTP 429 单独归类为 `rate_limit`，不立即重试，首个 429 打开进程内认证流冷却电路，阻止同批次等待账号继续冲击上游。

---

## 5. 代理三元隔离（Proxy Three-lane Isolation）

代理按用途分 lane；只有文档化的兼容回退允许跨 lane 候选（详见
`architecture.md` 的 *Proxy Routing Boundary*）：

| Lane | 用途 | 出口来源 |
| --- | --- | --- |
| ① 注册代理 | 注册 worker（全部 6 驱动） | `proxy.registration` + `proxy.pool`（动态 sticky session，**单地区 IN、rola 10 条**；9http 已于 2026-09-18 从活动注册池移除） |
| ② 邮箱/OTP 代理 | OTP 轮询收件 | `mailbox_proxy` / `mailbox_proxy_pool` 优先；`email_registration.mailbox_proxy_fallback_to_operation_proxy=true` 时把本次 operation proxy 追加为故障回退 |
| ③ 协议支付代理 | Checkout/Approve | **随用户选择的 checkout/approve 出口动态选择**：取 `protocol_payments.methods.<method>.checkout_proxy_pool` / `approve_proxy_pool` 持有的候选池（如 IPWO US/JP/GB），**非固定 JP/US/GB 混用** |

- **按 lane 选池**：`proxy_pool_for()`（`proxy_routing.py:41`）返回 lane 专属池 + 单向回退。已知 lane：`browser_registration`、`protocol_registration`、`liveness`、`promotion`、`health_browser`。
- **统一操作代理候选**：`select_operation_proxy()`（`proxy_routing.py:219`）按显式输入、可选 registration affinity、operation pool 和文档化回退取首项，并保留非敏感来源标签。

> 实战含义：本地用 Clash/代理软件把 `127.0.0.1:7897` 作为 OTP 收件专用出口，注册与支付各走独立上游；不要把同一个 session 出口同时喂给注册和健康探测。

---

## 6. ProxyEntry 单一范式与动态轮换

代理字符串操作**只有一个权威来源**：`sms_tool/proxy_entry.py`。`phone_proxy` 与 `paypal_proxy` 的 `refresh_proxy_sid` / `match_proxy_region` / `retarget_proxy_country` 等都是**薄封装**，归一化后委托给 `proxy_entry`，保证同一供应商代理在不同流程中轮换方式完全一致。

- **单一模型**：`ProxyEntry` frozen dataclass（`proxy_entry.py:67`）—— 全项目的规范代理表示。
- **六形式解析**：`parse_proxy()`（`proxy_entry.py:128`）处理 6 种代理 URL 写法。
- **重建 / 重定 / 轮换**：
  - `rebuild_proxy_credentials()`（`proxy_entry.py:384`）
  - `retarget_region()`（`proxy_entry.py:405`）—— 重定出口地区
  - `rotate_session()`（`proxy_entry.py:444`）—— 刷新 sticky session id
- **供应商模板**：
  - **Cliproxy**：username `region-XX` + `-sid-<id>-t-<n>`
  - **9http / 9proxy**：username `geo-XX` + `-sid-<id>-ttl-<n>`。地区标签是 `geo` 而非 `region`，`_INFER_USER_REGION_RE` / `_USER_REGION_RE` 同时接受两种拼写；**重定地区时保留原标签**，否则 `geo-VN` 会被改写成供应商不认的 `region-US`。
  - **IPWO**：`custom_zone_XX`
  - **rola**：username `..._<sid>-country-XX`。地区标签是 `country`，`_REGION_TAG` 必须包含它，否则重定与会话轮换对它**静默 no-op**；`_ROLA_SID_RE` 用 lookbehind 取 `_` 与 `-country-XX` 之间的 sid。国家码**大小写不敏感**（实测 `country-US` 拿到 US 出口），重定后会被规范成大写。
  - **Kookeey**：password `BASE-CC-SESSION-TTL`，TTL 单位 `\d+[smhd]` 超集
- **选池**：`load_proxy_pool()`（`proxy_entry.py:535`）/ `choose_proxy_entry()`（`proxy_entry.py:601`）。
- **脱敏**：`masked` 去除凭据，日志/报告只显示脱敏串。
- **池形态约束（以 `proxy.json` 为准）**：
  🔴 **2026-09-18 现状：单地区 IN、rola 10 条。** `proxy.registration` /
  `default` / `proxy.pool` 均使用 rola `country-in` 模板。9http 曾短暂作为第二供应商
  接入，但本轮实测的最终失败集中在该供应商，现已从活动注册池移除。通用
  `ProxyEntry` 仍保留 9http 模板解析能力，供历史配置和其他独立代理池兼容使用。

  **IN 地理档案继续保留**：规范表 `geo/profiles.MARKET_PROFILES` 使用
  `en-IN` / `Asia/Kolkata`，浏览器映射使用 `COUNTRY_LOCALE_PROFILE_MAP["IN"]="in"`，
  Windows 时区名为 `India Standard Time`。守卫位于
  `tests/test_registration_protocol_geo.py`。

  批次启动前会对候选路由逐条执行 OpenAI 边界预检，只把成功路由传给 batch。
  批次内的 IP/国家探测不再被当作 OpenAI 可达性的替代判据。

  <details><summary>历史（2026-09-13 / 09-17 的 VN 与 IPWO 实测，保留供追溯）</summary>

  顶层 `proxy.registration` / `default` / `pool` 曾是 **30 条**，三家供应商混编。**主出口 VN（20 条）**：9http（`global.9http.com:9091`，`geo-VN` 模板，10 条）与 IPWO（`us.ipwo.net:7878`，`custom_zone_VN` 模板，10 条），出口实测落在 FPT Telecom / VNPT / Viettel 等本地 ISP；**第二出口 PH（10 条）**：rola（`gate.rola.vip:2000`，`country-PH` 模板）。加第二出口的原因是原池 30 条**地区维度零冗余（全是 VN）**，一个地区被拒即整批同时死；⚠️ 注意别把「主机有三家」当成冗余——主机维度确实有冗余，**地区维度没有**。🔴 **2026-09-13 实测（用活跃池凭据，已推翻上一版的保留意见）**：`us.ipwo.net:7878` 用**活跃池里的** `custom_zone_VN` 凭据在 CONNECT 阶段回 `403 {"code":403,"msg":"access denied,china IP is not allow"}`——被拒的是**本机自己的中国出口 IP**（响应头 `X-Client-Remote-Addr: 115.197.164.253`），与地区标签、账号余额、凭据有效性都无关 ⇒ 这条出口**从这台机器上不可能通**，除非先经非中国 IP 中转。`global.9http.com:9091` 则是 TCP 连通后**在 `recv` 阶段被 RST**（明文与 TLS 包裹结果相同、无任何 HTTP 响应）⇒ 不是 scheme/端口错配；原因无法坐实，**推断**同为客户端 IP 封锁。结论：**当前唯一可用出口是 rola PH**，所以"VN 主 / PH 备"在实际可用性上是反的——这也正是加第二出口的价值所在。要判定活跃池健康度请用 `runtime/_probe_egress_regions.py` 实测（它会按 `(host, 声明地区)` 分组抽测并对账）。`proxy.registration` / `default` 仍指向 9http 的 VN 条目，所以 VN 保持主出口，PH 靠 `ProxyHealthTracker` 的健康排序在主出口冷却时才被优先选中。**PH 的地理档案已一并补齐**：手改 **3 处**——规范表 `geo/profiles.MARKET_PROFILES`（`en-PH` / `Asia/Manila`）+ `browser_fingerprint_pool.COUNTRY_LOCALE_PROFILE_MAP["PH"]="ph"` + `TIMEZONE_NAME_BY_IANA["Asia/Manila"]="Singapore Standard Time"`（Windows 无菲律宾专属时区，走 CLDR）；`auth_headers._GEO_PROFILES` 与 `BROWSER_LOCALE_PROFILES` 是**派生视图，自动生效**。缺任一手改项都会静默降级：协议路径回退 UTC、浏览器路径回退 `'us'` 档案，出现"菲律宾出口 + 美国指纹"。对账手段见 `runtime/_probe_egress_regions.py`（实测地区 vs 凭据声明）与 `registration.fingerprint_pool.verify_hint`。协议支付 / PayPal 的 `checkout_proxy_pool`、`approve_proxy_pool`、`proxies`、`stage_proxy_pools`、`proxy_pool` **仍按用户选择的 checkout/approve 出口动态选取**（候选为 IPWO US/JP/GB，见 §7 代理 lane 表），**不是固定 JP/US/GB 混用**，且**不随注册池切换到 VN**。**Kookeey（`gate.kookeey.info`）只保留在支付方法的 `stage_proxies` / 单方法 `proxy` 字段**（各 payment method 的 stage 拉取那一步），不参与注册与 checkout/approve；`direct_card` 等仍通过同一 ProxyEntry 模板规则旋转 Kookeey sticky 密码。**Cliproxy 用户名处理（`region-XX`）在 `proxy_entry.py` 中仍保留**，但未配置 Cliproxy URL，属未启用状态。
  
  </details>
- **配置真源提醒**：`config.json` 在 `proxy.json` / `runtime.json` / `payment.json` 任一分片存在时即退化为 legacy 死文件（`config.py:163-181`），且其 `proxy.registration` 仍是历史的 100 条列表形态，与分片不一致。**改代理只改 `proxy.json`**。

> 实战含义：新增任何代理供应商支持，**只改 `proxy_entry`**（解析 + 重建 + 重定 + 轮换），不要让 `phone_proxy` / `paypal_proxy` 重新实现字符串操作。

---

## 7. 各驱动 → 指纹池 / 代理池 映射（环境配置矩阵）

两条注册路径（§1）的**指纹来源**和**代理 lane**并不相同。下表为 2026-08-29 代码快照结论；行号随重构漂移，以符号检索为准。

### 7.1 指纹池：两个，非每驱动一个

| 路径 | 指纹池类型 | 单例入口 | 内容 | 地理对齐 |
| --- | --- | --- | --- | --- |
| `protocol` | `FingerprintPool`（`fingerprint_pool.py:121`） | `shared_fingerprint_pool(config)`（`fingerprint_pool.py:328`） | TLS/UA 档案 `ProtocolEnvironmentProfile`（`fingerprint_pool.py:37`） | `next(proxy)`（`fingerprint_pool.py:249`）按 `_GEO_PROFILES`（`auth_headers.py:313`）覆盖 locale/timezone |
| `browser_*`（4 个） | `BrowserProfilePool`（`browser_fingerprint_pool.py:198`） | `shared_browser_profile_pool(config)`（`browser_fingerprint_pool.py:234`），经 `select_browser_profile(...)`（`browser_fingerprint_pool.py:537`）取档 | 7 个内置桌面硬件档案 `BROWSER_PROFILE_POOL`（`browser_fingerprint_pool.py:148`） | `detect_proxy_exit_geo(proxy)`（`browser_fingerprint_pool.py:317`）经共享 `geo.resolver`（Cloudflare trace 优先）→ `BROWSER_LOCALE_PROFILES`（`browser_fingerprint_pool.py:79`，经 `COUNTRY_LOCALE_PROFILE_MAP` 把 `VN` 映射到 `vn`） |

**核心结论**：浏览器路径的 7 个硬件档案是**进程级单例、被全部 4 个浏览器驱动共享**——playwright / camoufox / cloak / roxy 都走 `run_browser_registration`（`registration_drivers/browser_flow/orchestrator.py:69`）→ `_browser_session_scope`（`registration_drivers/browser_flow/flow_steps.py:155`）→ `select_browser_profile(_browser_geo, seed=device_id, config=config)`（`browser_fingerprint_pool.py:537`）取同一池。协议路径用独立的 `FingerprintPool`，两者**互不复用**。硬件档案只来自内置池；已退休的 `registration.browser_profile_pool` 会在配置校验时报错，不再制造“配置已生效”的假 Interface。

> **地区覆盖（2026-09-11）**：两侧都已收录 `VN`——协议路径 `_GEO_PROFILES["VN"]` = `Asia/Ho_Chi_Minh` / `vi-VN`；浏览器路径 `BROWSER_LOCALE_PROFILES["vn"]` + `COUNTRY_LOCALE_PROFILE_MAP["VN"] = "vn"`，并在 `TIMEZONE_NAME_BY_IANA` 补了 `Asia/Ho_Chi_Minh`。**未收录的国家不会报错，而是静默回退**：协议路径回退到"实测时区 + 档案原语言"，浏览器路径 `locale_profile_key_from_geo` 直接回退 `"us"`。因此**新增出口地区时必须同步补这三张表**，否则会出现"出口在 A 国、语言是 en-US"的隐性矛盾。

### 7.2 代理 lane：三元隔离的运行时落地

| Lane | 选取入口 | 落地池（config 键） | 备注 |
| --- | --- | --- | --- |
| 注册（全部 6 驱动） | `proxy_pool_for(config, "protocol_registration"` / `"browser_registration")`（`proxy_routing.py:40`） | `proxy.registration` + `proxy.pool` → 回退 `proxy.default`（现为 **10 条 rola `country-in`**） | `browser_registration` 先查 `browser_pool`/`browser_registration_pool` 别名，空则回退注册主池（`proxy_routing.py:53`、`:68`）。**两条注册路径共用同一个池**，且活体/健康 lane 也回退到它——改注册池会连带改变健康探测出口 |
| 邮箱/OTP | `mailbox._mailbox_proxy_candidates` | `mailbox_proxy` → `mailbox_proxy_pool` → 可选 operation proxy | mailbox 配置保持第一优先；operation proxy 仅在开关启用时作为尾部故障回退 |
| 协议支付 | 方法配置 `protocol_payments.methods.<method>.checkout_proxy_pool` / `approve_proxy_pool`（`config.json:650` 起） | **随用户选择的 checkout/approve 出口动态选择**，候选池形如 IPWO US/JP/GB | **不是固定 JP/US/GB 混用**（见 §6 池形态约束更正） |
| 活体/推广/健康 | `operation_proxy_candidates(...)` / `select_operation_proxy(...)` | 显式输入 → 可选 registration affinity → operation pool → 文档化回退 | 候选来源随诊断记录；调用方不得自行重排 |

### 7.3 浏览器进程池：跨驱动共享

全部 4 个浏览器驱动共享同一个**常驻进程池**（`browser_pool.py`），非每驱动各开各的：

- `PoolConfig`（`browser_pool.py:62`）：`max_concurrent`（默认 4）/ `max_uses_per_process`（默认 10）/ `recycle_on_error`（默认 true）。
- config 键**故意叫 `registration.browser_process_pool`**（`config.json:293`），**不叫 `browser_pool`**——后者是 `proxy_routing` 里的代理别名，二者无关（`browser_pool.py:70` 注释明确）。
- 进程级回收：达到 `max_uses_per_process`、出错（`recycle_on_error`）或代理变更时，该槽位进程回收重建（`browser_pool.py:156`）。默认 `max_concurrent:4` 与脉冲 `wave_size:4` 对齐。

### 7.4 各驱动环境配置键一览

| 驱动 | 路径 | 关键 config 键（`registration.drivers.<name>`，`config.json`） |
| --- | --- | --- |
| `protocol` | 协议直登 | 无浏览器键；用 `proxy.registration` + `fingerprint_pool` |
| `playwright` | 浏览器 | `registration.drivers.playwright.start_url`（`:227`） |
| `roxy` | 浏览器（Roxy CDP） | `registration.drivers.roxy.api_base`=50000 / `api_token` / `workspace_id` / `project_id`（`:229`） |
| `cloak` | 浏览器 | `registration.drivers.cloak.humanize` / `geoip` / `use_proxy` / `license_key`（`:243`） |
| `camoufox` | 浏览器（默认 `registration.driver`） | `registration.drivers.camoufox.humanize` / `geoip` / `max_width` / `max_height` / `locale` / `timezone`（`:253`） |
| 全部浏览器 | — | `registration.browser_process_pool`（`:293`）、`registration.browser_headless` / `browser_timeout_seconds` / `browser_locale` / `browser_timezone`（`:219`） |

> 实战含义：改指纹/代理行为先判明驱动走协议池还是浏览器池——协议池改 `fingerprint_pool` + `_GEO_PROFILES`，浏览器池改 `browser_fingerprint_pool` 的 `BROWSER_PROFILE_POOL` / `BROWSER_LOCALE_PROFILES`，且改动影响**全部 4 个浏览器驱动**（单例共享）。代理出口严格按 lane 隔离，注册/邮箱/支付/健康四路互不复用同一 session。

---

## 8. 预检与代理协议纠正

注册真正开始前，先跑网络边界预检（在认领邮箱之前）：

- `registration_network_preflight()`（`registration_preflight.py:104`）探测 chatgpt / auth / sentinel 边界，使用 `impersonate` 模拟。
- `_resolve_proxy_scheme()`（`registration_preflight.py:74`）纠正被标错的 socks5↔http；并可用 `proxy_scheme_fallback=off` **钉死** scheme，避免运行时被自动回退到错误协议。

---

## 9. 桌面端如何调用（IPC）

WPF 桌面端（`SmsWorkbench/`）通过 `PythonBackendClient` 启动 `python -m sms_tool` 子进程，结构化结果走 `smsworkbench.ipc.v2` 信封（见 `architecture.md` 的 *WPF UI* 节）。注册逻辑**始终在 `sms_tool.registration`**，桌面端只负责启动 + 读取已脱敏结果，绝不回流 ChatGPT 注册生命周期。

编译唯一入口：`SmsWorkbench/build_dotnet.ps1`（使用 `dotnet publish` 输出到 `dist/net10/SmsWorkbench.exe`，随后清理中间产物）。**禁止**直接 `dotnet build`——只会产出非分发目录的中间物且不自动清理。

---

## 10. 敏感数据边界

- `.gitignore` 已保护：`config.json`、`sms_tool/config.json`、`mailbox_tokens.txt`、`sessions/`、`runtime/`、`dist/`、`.dotnet/`、`logs/`、`*_tokens.txt` 等。
- `sensitive_policy.json` 是唯一的语言中立脱敏策略；Python 经 `sanitizer`，WPF 经 `SensitiveDataSanitizer` 加载同一文件。token / TOTP / 代理凭据 / 卡号 / 密码 / 支付密钥在日志、异常、IPC、报告里**全部替换**（非前缀遮罩）。

---

## 附：关键符号索引（file:line）

| 符号 | 位置 | 作用 |
| --- | --- | --- |
| `RegistrationDriver` | `registration_drivers/base.py:23` | 驱动枚举 |
| `BROWSER_REGISTRATION_DRIVERS` | `registration_drivers/base.py:85` | browser vs protocol 切分 |
| `normalize_registration_driver` | `registration_drivers/base.py:94` | 归一化，默认 protocol |
| `BrowserRegistrationError` | `registration_drivers/base.py:130` | 浏览器层错误封装 |
| `AUTH_FINGERPRINT_PROFILES` | `auth_headers.py:36` | Chrome 124–146 设备档案 |
| `sentinel_fingerprint` | `auth_headers.py:630` | 确定性指纹派生 |
| `openai_auth_headers` | `auth_headers.py:715` | `oai-*` 头注入 |
| `OPENAI_SENTINEL_BACKEND` | `sentinel/client.py:59`（默认 `node_runner`） | Sentinel 后端选择 |
| `_get_cached_sentinel` / `_save_sentinel_cache` | `sentinel_tokens.py:41` / `:56` | 线程安全缓存 |
| `_sentinel_device_id` / `assert_sentinel_device_id` | `sentinel_tokens.py:89` / `:101` | DID 一致性 |
| `proxy_pool_for` | `proxy_routing.py:41` | 按 lane 选池 + 单向回退 |
| `select_operation_proxy` | `proxy_routing.py:219` | 从统一候选序列选择操作代理 |
| `ProxyEntry` | `proxy_entry.py:67` | 规范代理模型 |
| `parse_proxy` | `proxy_entry.py:128` | 6 形式解析 |
| `rebuild_proxy_credentials` | `proxy_entry.py:384` | 凭据重建 |
| `retarget_region` | `proxy_entry.py:405` | 地区重定 |
| `rotate_session` | `proxy_entry.py:444` | 会话轮换 |
| `load_proxy_pool` / `choose_proxy_entry` | `proxy_entry.py:535` / `:601` | 选池 |
| `registration_network_preflight` | `registration_preflight.py:104` | 边界探活 |
| `_resolve_proxy_scheme` | `registration_preflight.py:74` | socks5↔http 纠错 |
| `shared_fingerprint_pool` | `fingerprint_pool.py:347` | 协议路径指纹池单例 |
| `FingerprintPool` / `ProtocolEnvironmentProfile` | `fingerprint_pool.py:121` / `:37` | 协议路径 TLS/UA 档案 |
| `shared_browser_profile_pool` | `browser_fingerprint_pool.py:246` | 浏览器路径内置指纹池单例 |
| `select_browser_profile` | `browser_fingerprint_pool.py:549` | 取浏览器硬件档案（seed 稳定） |
| `detect_proxy_exit_geo` | `browser_fingerprint_pool.py:329` | 穿透代理查出口地理 |
| `BrowserProfilePool` / `BROWSER_PROFILE_POOL` | `browser_fingerprint_pool.py:210` / `:160` | 7 个内置桌面硬件档案（4 个浏览器驱动共享） |
| `run_browser_registration` | `registration_drivers/browser_flow/orchestrator.py:69` | 5 浏览器驱动统一入口 |
| `PoolConfig`（进程池） | `browser_pool.py:62` | `registration.browser_process_pool` 解析 |

---

*本文档与 `architecture.md` 互为补充：边界/归属看 `architecture.md`，运行时控制流看本文。两者冲突时以 `architecture.md` 的边界规则为准。*

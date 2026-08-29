# 注册与代理运行时架构深读

> 本篇是 [`architecture.md`](architecture.md)（模块职责 / 边界视角）的**互补文档**，从**运行时控制流 + 代码定位**角度，拆解两个被频繁触及却没有被串成一条线的子系统：
>
> 1. **注册路径**（协议注册 vs 无头浏览器注册）
> 2. **代理池**（三元隔离 + 单一范式解析 + 动态轮换）
>
> 行号基于 2026-08-27 的代码快照，随重构可能漂移；以函数/类名检索为准。

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
| 桌面调用 | `sms_tool/desktop_ipc` + `SmsWorkbench/` | v2 IPC 信封，Python 子进程执行 |

---

## 1. 注册双路径：protocol vs browser

注册入口由 `RegistrationDriver` 枚举驱动（`registration_drivers/base.py:9`）：

```python
class RegistrationDriver(str, Enum):
    PROTOCOL   = "protocol"     # 默认：curl_cffi HTTP 直登
    PLAYWRIGHT = "playwright"
    CAMOUFOX   = "camoufox"
    CLOAK      = "cloak"
    ROXY       = "roxy"
    ADSPOWER   = "adspower"
```

关键切分在 `BROWSER_REGISTRATION_DRIVERS`（`base.py:19`）—— 除 `protocol` 之外的全部成员。
`normalize_registration_driver()`（`base.py:29`）负责把 `None` / 字符串 / 配置值归一化，**默认落回 `protocol`**。

### 两条路径的异同

| 维度 | `protocol` | `browser_*`（playwright/camoufox/…） |
| --- | --- | --- |
| 执行体 | curl_cffi HTTP 直登 | 无头浏览器驱动完成 signup |
| 指纹/鉴权头 | `sentinel_fingerprint()` + `openai_auth_headers()` | **同一套**，注入到浏览器上下文 |
| Sentinel 事务 | 独立事务 + 独立 `oai-did` | 独立事务 + 独立 `oai-did` |
| 异常处理 | 标准网络/鉴权重试 | `BrowserRegistrationError`（`base.py:57`）封装浏览器层错误 |

**核心结论**：两条路径在「指纹 → 鉴权头 → Sentinel Token → AT 探活 → 持久化」这条主干上**完全共用**，差异只在最前端的 signup 执行方式。这意味着无论走哪条路，反关联与反爬强度是一致的，不存在「浏览器路径更稳」或「协议路径更弱」的本质区别——强弱由指纹/头/Sentinel 一致性决定，而非驱动选择。

> 这也解释了为什么 `registration.py` 只是一个**门面**（`from .auth_flow import ...`、`from .account_creation import ...`、`from .sentinel_tokens import ...`），具体实现被拆到 `auth_flow / account_creation / otp_strategy / mailbox / session_builder / storage`，门面只负责对外暴露 helper 并禁止本地遮蔽。

---

## 2. 指纹与鉴权头（Fingerprint & Auth Headers）

`sms_tool/auth_headers.py` 是「每个账号看起来像同一台稳定设备」的事实来源。

- **设备档案**：`AUTH_FINGERPRINT_PROFILES`（`auth_headers.py:17`）覆盖 Chrome 124–146 的 UA / 平台 / 渲染器组合。
- **确定性指纹**：`sentinel_fingerprint()`（`auth_headers.py:279`）按账号 `device_id` **确定性派生** screen / CPU / 内存 / `time_origin`，使得同一账号每次注册拿到一致指纹，不同账号彼此不关联（防关联）。
- **鉴权头注入**：`openai_auth_headers()`（`auth_headers.py:348`）注入 `oai-device-id`、`oai-session-id`、`sec-ch-ua*`、`sec-ch-ua-platform`、`Datadog` trace 等；`family` 分为 `nextauth` / `auth` / `chatgpt` 三族，三族**共享**同一 DID、稳定的 session logging id、flow invocation id、UA、client hints、GeoIP 派生的 locale/timezone。
- **一致性强约束**（见 `architecture.md` 的 *Registration Protocol Consistency*）：Sentinel QuickJS 消费**同一指纹**，为 `username_password_create` / `authorize_continue` / `oauth_create_account` 分别产出 token；token payload id、`oai-did` cookie、auth header **必须匹配**。提取失败**fail closed**——绝不使用纯 HTTP 的 PoW fallback。

> 实战含义：调注册相关代码时，**不要**单独改某一处 `oai-*` 头或指纹，必须走 `auth_headers` 统一出口，否则 DID/Header/Cookie 三者错位会直接被风控。

---

## 3. Sentinel 反爬（Anti-bot）

Sentinel 不是纯 Python PoW，而是调用**真实 Node SDK**：

- 后端选择：环境变量 `OPENAI_SENTINEL_BACKEND`（在 `sms_tool/sentinel/client.py:59` 读取，默认 `node_runner`），对应 `config.example.json` 的 `sentinel_backend: "node_runner"`。`node_runner` 执行 vendored 在 `sms_tool/sentinel/` 下的 SDK（`client.py:312`）。
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

代理出口**绝不可混用**，这是硬边界（详见 `architecture.md` 的 *Proxy Routing Boundary*）：

| Lane | 用途 | 出口来源 |
| --- | --- | --- |
| ① 注册代理 | 注册 worker | `proxy.registration` + `proxy.pool`（动态 JP session） |
| ② 邮箱/OTP 代理 | OTP 轮询收件 | `mailbox_proxy`，**固定** `http://127.0.0.1:7897`，从不继承旋转注册代理（`mailbox._resolve_mailbox_proxy`） |
| ③ 协议支付代理 | Checkout/Approve | `checkout_proxy_pool` / `approve_proxy_pool`（按方法 `protocol_payments.methods.<method>`） |

- **按 lane 选池**：`proxy_pool_for()`（`proxy_routing.py:40`）返回 lane 专属池 + 单向回退。已知 lane：`browser_registration`、`protocol_registration`、`liveness`、`promotion`、`health_browser`。
- **探测避开注册出口**：`select_operation_proxy()`（`proxy_routing.py:86`）在做活体/健康探测（`liveness` / `health_browser`）时**故意使用与注册不同的出口**，降低出口复用被风控的概率。

> 实战含义：本地用 Clash/代理软件把 `127.0.0.1:7897` 作为 OTP 收件专用出口，注册与支付各走独立上游；不要把同一个 session 出口同时喂给注册和健康探测。

---

## 6. ProxyEntry 单一范式与动态轮换

代理字符串操作**只有一个权威来源**：`sms_tool/proxy_entry.py`。`phone_proxy` 与 `paypal_proxy` 的 `refresh_proxy_sid` / `match_proxy_region` / `retarget_proxy_country` 等都是**薄封装**，归一化后委托给 `proxy_entry`，保证同一供应商代理在不同流程中轮换方式完全一致。

- **单一模型**：`ProxyEntry` frozen dataclass（`proxy_entry.py:67`）—— 全项目的规范代理表示。
- **六形式解析**：`parse_proxy()`（`proxy_entry.py:128`）处理 6 种代理 URL 写法。
- **重建 / 重定 / 轮换**：
  - `rebuild_proxy_credentials()`（`proxy_entry.py:347`）
  - `retarget_region()`（`proxy_entry.py:368`）—— 重定出口地区
  - `rotate_session()`（`proxy_entry.py:405`）—— 刷新 sticky session id
- **供应商模板**：
  - **Cliproxy**：username `region-XX` + `-sid-<id>-t-<n>`
  - **IPWO**：`custom_zone_XX`
  - **Kookeey**：password `BASE-CC-SESSION-TTL`，TTL 单位 `\d+[smhd]` 超集
- **选池**：`load_proxy_pool()`（`proxy_entry.py:473`）/ `choose_proxy_entry()`（`proxy_entry.py:539`）。
- **脱敏**：`masked` 去除凭据，日志/报告只显示脱敏串。
- **池形态约束（以 `config.json` 为准，2026-08-27 更正）**：**IPWO 是主代理池**——顶层 `proxy.registration` / `default` / `pool` 全部是 `us.ipwo.net` / `eu.ipwo.net`，且协议支付 / PayPal 的 `checkout_proxy_pool`、`approve_proxy_pool`、`proxies`、`stage_proxy_pools`、`proxy_pool` 也全是 IPWO 三条（US/JP/GB）。**Kookeey（`gate.kookeey.info`）只保留在支付方法的 `stage_proxies` / 单方法 `proxy` 字段**（各 payment method 的 stage 拉取那一步），不参与注册与 checkout/approve；`direct_card` 等仍通过同一 ProxyEntry 模板规则旋转 Kookeey sticky 密码。**Cliproxy 用户名处理（`region-XX`）在 `proxy_entry.py` 中仍保留**，但 `config.json` 未配置 Cliproxy URL，属未启用状态。

> 实战含义：新增任何代理供应商支持，**只改 `proxy_entry`**（解析 + 重建 + 重定 + 轮换），不要让 `phone_proxy` / `paypal_proxy` 重新实现字符串操作。

---

## 7. 预检与代理协议纠正

注册真正开始前，先跑网络边界预检（在认领邮箱之前）：

- `registration_network_preflight()`（`registration_preflight.py:99`）探测 chatgpt / auth / sentinel 边界，使用 `impersonate` 模拟。
- `_resolve_proxy_scheme()`（`registration_preflight.py:69`）纠正被标错的 socks5↔http；并可用 `proxy_scheme_fallback=off` **钉死** scheme，避免运行时被自动回退到错误协议。

---

## 8. 桌面端如何调用（IPC）

WPF 桌面端（`SmsWorkbench/`）通过 `PythonBackendClient` 启动 `python -m sms_tool` 子进程，结构化结果走 `smsworkbench.ipc.v2` 信封（见 `architecture.md` 的 *WPF UI* 节）。注册逻辑**始终在 `sms_tool.registration`**，桌面端只负责启动 + 读取已脱敏结果，绝不回流 ChatGPT 注册生命周期。

编译唯一入口：`SmsWorkbench/build_dotnet.ps1`（使用 `dotnet publish` 输出到 `dist/net10/SmsWorkbench.exe`，随后清理中间产物）。**禁止**直接 `dotnet build`——只会产出非分发目录的中间物且不自动清理。

---

## 9. 敏感数据边界

- `.gitignore` 已保护：`config.json`、`sms_tool/config.json`、`mailbox_tokens.txt`、`sessions/`、`runtime/`、`dist/`、`.dotnet/`、`logs/`、`*_tokens.txt` 等。
- `sensitive_policy.json` 是唯一的语言中立脱敏策略；Python 经 `sanitizer`，WPF 经 `SensitiveDataSanitizer` 加载同一文件。token / TOTP / 代理凭据 / 卡号 / 密码 / 支付密钥在日志、异常、IPC、报告里**全部替换**（非前缀遮罩）。

---

## 附：关键符号索引（file:line）

| 符号 | 位置 | 作用 |
| --- | --- | --- |
| `RegistrationDriver` | `registration_drivers/base.py:9` | 驱动枚举 |
| `BROWSER_REGISTRATION_DRIVERS` | `registration_drivers/base.py:19` | browser vs protocol 切分 |
| `normalize_registration_driver` | `registration_drivers/base.py:29` | 归一化，默认 protocol |
| `BrowserRegistrationError` | `registration_drivers/base.py:57` | 浏览器层错误封装 |
| `AUTH_FINGERPRINT_PROFILES` | `auth_headers.py:17` | Chrome 124–146 设备档案 |
| `sentinel_fingerprint` | `auth_headers.py:279` | 确定性指纹派生 |
| `openai_auth_headers` | `auth_headers.py:348` | `oai-*` 头注入 |
| `OPENAI_SENTINEL_BACKEND` | `sentinel/client.py:59`（默认 `node_runner`） | Sentinel 后端选择 |
| `_get_cached_sentinel` / `_save_sentinel_cache` | `sentinel_tokens.py:41` / `:56` | 线程安全缓存 |
| `_sentinel_device_id` / `assert_sentinel_device_id` | `sentinel_tokens.py:89` / `:101` | DID 一致性 |
| `proxy_pool_for` | `proxy_routing.py:40` | 按 lane 选池 + 单向回退 |
| `select_operation_proxy` | `proxy_routing.py:86` | 探测避开注册出口 |
| `ProxyEntry` | `proxy_entry.py:67` | 规范代理模型 |
| `parse_proxy` | `proxy_entry.py:128` | 6 形式解析 |
| `rebuild_proxy_credentials` | `proxy_entry.py:347` | 凭据重建 |
| `retarget_region` | `proxy_entry.py:368` | 地区重定 |
| `rotate_session` | `proxy_entry.py:405` | 会话轮换 |
| `load_proxy_pool` / `choose_proxy_entry` | `proxy_entry.py:473` / `:539` | 选池 |
| `registration_network_preflight` | `registration_preflight.py:99` | 边界探活 |
| `_resolve_proxy_scheme` | `registration_preflight.py:69` | socks5↔http 纠错 |

---

*本文档与 `architecture.md` 互为补充：边界/归属看 `architecture.md`，运行时控制流看本文。两者冲突时以 `architecture.md` 的边界规则为准。*

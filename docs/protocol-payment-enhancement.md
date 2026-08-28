# 协议支付模块补强设计文档

> 目标：将外部项目 [paypal-agreement-protocol](https://github.com/1537271403/paypal-agreement-protocol)
> 的三块**通用工程能力**——代理解析器、代理桥、身份提升流程中的上下文校验/结果归一化——
> 逐一映射到当前项目的对应模块，给出移植方案、集成点与依赖清单，并直接落地代码。
>
> 范围边界：仅做**架构级 / 通用工程**移植（代理解析、代理桥、响应结果归一化、上下文校验）。
> 不移植、不增强外部项目中任何"协议逆向 / 绕过风控"的具体实现（如 DataDome 绕过、身份提升
> guest→member 的具体反风控交互）。凡涉及此类逻辑，文档只做标注，不提供实现。

---

## 0. 结论摘要

当前项目（`F:\epsoft\GPT-Register-Tool`）在协议支付链路上已经具备相当完整的代理与结果契约基础设施，
但与外部项目相比仍有三处可补强，且均为**通用工程能力**：

| # | 外部项目能力 | 当前项目现状 | 补强结论 |
|---|-------------|-------------|----------|
| 1 | `paypal/proxy.py` 代理解析器（`ProxyEntry.parse` / `load_proxy_pool` / `choose_proxy_entry`） | 已有 `phone_proxy.normalize_proxy_url` 与 `proxy_pool.UpstreamProxy.from_url`，但**逻辑重复、覆盖不全**（IPv6 裸格式、socks5/socks5h scheme 传播、端口默认值、无认证格式、池随机/轮询选择未统一） | **新增**统一 `ProxyEntry` 解析器 + 池加载/选择（`proxy_entry.py`） |
| 2 | `session.py` / `flow.py` 把单条认证代理桥到本地会话 | 已有 `proxy_pool.Socks5Server`（多上游手动池），但需外部配置文件启动 | **新增**进程内单条认证代理 → 本地 `socks5://127.0.0.1:<port>` 桥（供 nodriver / 手动浏览器路径使用） |
| 3 | `elevation_flow.py` 中"上下文校验 + 结果归一化"的工程模式（`checkoutSessionType` 校验、`BuyerFundingContext` 查询、fatal contingency 分类） | 已有 `PaymentResult.from_mapping` 结果契约，但**缺少从 PayPal GraphQL/JSON 响应解析授权上下文字段并归一化的通用辅助** | **新增**`PayPal 授权结果上下文归一化器`（`paypal_authorization.py`），只解析、不参与反风控交互 |

依赖：全部零新增第三方依赖（复用项目已有的 `requests` / `curl_cffi` / `asyncio`）。

---

## 1. 现状盘点（recon 结果）

### 1.1 当前项目的代理相关模块

| 模块 | 职责 | 关键接口 |
|------|------|----------|
| `sms_tool/phone_proxy.py` | 手机注册链路的动态代理 | `normalize_proxy_url`（已含 `host:port:user:pass` → URL）、`probe_proxy`、`select_phone_proxy`、`redact_proxy_url` |
| `sms_tool/paypal_proxy.py` | 支付链路的 provider 动态会话旋转 / 健康探测 / 状态排名 | `rotate_proxy_session`、`probe_proxy`、`select_proxy_from_pool`、`PayPalProxyState` |
| `sms_tool/proxy_pool.py` | 本地 SOCKS5 池服务器（多上游、健康检查、轮询） | `Socks5Server`、`UpstreamProxy.from_url`（已支持 4 段裸格式） |
| `sms_tool/paypal_protocol.py` | BA/EC token 提取、Stripe redirect 追踪 | `extract_ba_token`、`extract_ec_token`、`_make_session` |

**发现的问题**：

1. **解析逻辑重复**：`phone_proxy.normalize_proxy_url` 与 `proxy_pool.UpstreamProxy.from_url` 各自实现了一遍裸 `host:port:user:pass` 解析，行为不一致。
2. **覆盖不全**：两处解析都不处理 IPv6 裸地址（`[::1]:port:user:pass`）、不统一 socks5/socks5h scheme 归一、裸格式缺省端口时无默认值、`host:port` 无认证格式在 `phone_proxy` 里会被误判为 4 段格式失败。
3. **缺少池加载与选择**：没有从**环境变量 + 配置文件**统一加载代理池并做随机/轮询选择的公共入口（外部项目 `load_proxy_pool` / `choose_proxy_entry` 所解决的）。
4. **认证 socks5 → 浏览器桥**：当前只能靠手动启动 `proxy_pool.Socks5Server`，缺少一个"给一条代理，进程内即可用"的轻量桥。

### 1.2 当前项目的结果契约与授权链路

- `sms_tool/payment_contracts.py`：`PaymentResult` / `PaymentError` / `StageOutcome` / `payment_retry_allowed`，统一归一化。
- `sms_tool/payment_link_manager.py`：统一状态机 + adapter registry。
- `sms_tool/omakse_client.run_us_payment`：把 BA token + 代理发给远程 omakse 服务器（外部授权引擎）。
- `sms_tool/gen_pp_link.py`：`PPLinkExtractor` / `generate_pp_link`（三段式提链）。
- `sms_tool/paypal_protocol.py`：BA/EC 提取、Stripe redirect。

**发现**：当前项目从 PayPal 授权响应中解析上下文（`checkoutSessionType`、`billingAgreementId`、fundingContext、EUAT 等）并归一化为 `PaymentResult` 的通用辅助缺失——`paypal_protocol.py` 只做 token/URL 提取，不解析授权结果结构。

### 1.3 外部项目能力对照

| 外部项目文件 | 能力 | 是否移植 |
|-------------|------|----------|
| `paypal/proxy.py` | `ProxyEntry.parse`（URL + 裸 `host:port:user:pass`）、`load_proxy_pool`（env 优先）、`choose_proxy_entry`（随机/index）、`build_proxy_config` | ✅ 移植（重构为项目统一抽象） |
| `paypal/session.py` | `PayPalSession`（curl_cffi/httpx 引擎、EUAT cookie 原子同步、graphql 非 JSON warm-up 重试） | ⚠️ 部分（EUAT cookie 同步可作参考，但当前项目授权走 omakse，暂不落地） |
| `paypal/elevation_flow.py` | `IdentityElevationPayPalFlow`（guest→member 提升，**含反风控交互**） | ⚠️ 仅移植其**结果归一化 / 上下文校验 / fatal contingency 分类**的工程模式，不移植反风控交互 |
| `paypal/proxy.py` `build_proxy_config` | socks5 认证代理 → 本地桥 | ✅ 移植为本地桥（供 nodriver/浏览器） |

---

## 2. 映射与移植方案

### 2.1 能力 1：统一代理解析器（→ 新模块 `sms_tool/proxy_entry.py`）

**目标**：提供**单一权威**的代理解析/加载/选择入口，被 `phone_proxy.py`、`paypal_proxy.py`、`proxy_pool.py` 复用。

**数据结构**（对齐外部 `ProxyEntry`，但补齐 IPv6 / scheme / 默认端口）：

```python
@dataclass(frozen=True)
class ProxyEntry:
    host: str
    port: int
    username: str
    password: str
    scheme: str        # "http" | "https" | "socks5" | "socks5h"
    label: str = ""    # 原始输入（脱敏展示用）
```

**核心函数**：
- `parse_proxy(raw, default_scheme="http") -> ProxyEntry | None`：
  - 支持 `scheme://user:pass@host:port`、`scheme://host:port`、`user:pass@host:port`、`host:port:user:pass`（裸 4 段）、`host:port`（无认证）、IPv6（`[::1]:port` 与裸 `[::1]:port:user:pass`）。
  - 缺失端口默认值：`http/https` → 80/443？不，代理端口不能凭空猜测——**缺失端口时返回 `None` 并给出原因**（区别于外部项目直接取 1080）。对 socks5 才默认 1080（对齐 `UpstreamProxy.from_url`）。
  - 自动归一化 scheme 别名：`socks`→`socks5`。
- `proxy_to_url(entry) -> str`：转标准 URL（供 requests / curl_cffi / httpx）。
- `load_proxy_pool(config=None, env_prefix="PROXY") -> list[ProxyEntry]`：环境变量 + 配置文件（逗号/换行分隔）合并去重。
- `choose_proxy_entry(pool, index=None) -> ProxyEntry | None`：随机或按 index。
- `to_dict` / `masked`（脱敏展示，与 `phone_proxy.redact_proxy_url` 保持一致）。

**集成点**：
- `phone_proxy.normalize_proxy_url` 内部可委托 `parse_proxy` + `proxy_to_url`（行为向后兼容）。
- `proxy_pool.UpstreamProxy.from_url` 可委托 `parse_proxy`（补齐 IPv6 / 无认证）。
- `paypal_proxy.select_proxy_from_pool` 可用 `load_proxy_pool` 作为默认池来源。

**依赖**：无新增。

### 2.2 能力 2：认证代理 → 本地浏览器代理桥（→ 新模块 `sms_tool/proxy_bridge.py`）

**目标**：给定**一条**代理（http/https/socks5，可带认证），在进程内启动一个本地 `socks5://127.0.0.1:<port>`，
浏览器（nodriver / Playwright / 手动配置）只需指向本地端口，无需关心上游格式与认证。

**方案**：复用 `proxy_pool.Socks5Server`，但**新增一个只含单上游的便捷启动器**，向上游提供 socks5 认证握手。
本地客户端以 `socks5h://127.0.0.1:<port>` 连接（无认证），桥内部对上游做认证握手与转发。

**核心接口**：

```python
@dataclass
class LocalProxyBridge:
    listen_host: str = "127.0.0.1"
    listen_port: int = 0            # 0 => 自动选空闲端口
    upstream: ProxyEntry | None = None

    def start(self) -> None:        # 启动 Socks5Server 单上游，返回实际端口
    @property
    def local_url(self) -> str:     # "socks5h://127.0.0.1:<port>"
    def stop(self) -> None
    def __enter__ / __exit__        # 上下文管理器
```

**关键点**：
- 桥的上游连接复用 `proxy_pool._connect_through_upstream` 的认证握手（socks5 user/pass）。若上游是 http(s) 代理，则需要一个额外的 http-CONNECT → socks5 的适配（本项目 `Socks5Server` 只支持 socks5 上游；**本方案将上游统一用 `ProxyEntry` 归一化，http 上游通过 HTTP CONNECT 隧道作为 socks5 目标**）。

  说明：为控制范围与零依赖，本版桥**仅支持 socks5 / socks5h 认证上游**（最常见场景），http 上游通过 `parse_proxy` 归一化后，若为 http 则在文档标注为"暂未实现"，不静默错误转发。

- 每次 `start()` 选择空闲端口，`stop()` 释放；可作为上下文管理器与 `with` 块配合使用。

**集成点**：
- `sms_tool/nodriver_paypal.py`、`sms_tool/paypal/orchestrator.py` 中浏览器会话创建前，可 `with LocalProxyBridge(upstream).local_url` 作为浏览器 `proxy` 参数。
- CLI `--ba-token` / `--proxy` 路径（`sms_tool/cli.py`）可选择开启本地桥。

**依赖**：无新增（复用 `proxy_pool` 与 `asyncio`）。

### 2.3 能力 3：授权结果上下文归一化器（→ 新模块 `sms_tool/paypal_authorization.py`）

**目标**：把外部项目 `IdentityElevationPayPalFlow` 中**"解析授权响应上下文 + 归一化结果 + 分类 fatal contingency"**的
工程模式，落地为一个**只读、纯解析**的通用辅助：输入 PayPal GraphQL / JSON 响应体，输出归一化的授权上下文与 `PaymentResult`-兼容映射。
**不包含**任何 guest→member 身份提升交互、反风控请求构造。

**核心函数**：

```python
@dataclass
class PayPalAuthorizationContext:
    checkout_session_type: str        # 如 BILLING_WITHOUT_PURCHASE
    billing_agreement_id: str         # B-...
    ba_token: str                     # BA-...
    ec_token: str                     # EC-...
    funding_context: dict             # 解析到的 BuyerFundingContext 字段（只读透传）
    approved: bool
    status: str                       # 归一化状态
    extra: dict

def parse_authorization_context(payload: dict | str, *, ba_token="") -> PayPalAuthorizationContext
def classify_authorization_outcome(ctx) -> dict   # -> {"ok","status","error_code","error_stage","retryable",...}
def to_payment_result(ctx, *, payment_method="paypal") -> dict  # 映射到 PaymentResult 兼容 dict
```

**校验/分类规则**（只读、纯规则）：
- `checkout_session_type == "BILLING_WITHOUT_PURCHASE"` → 期望类型；其他类型标注 `unexpected_checkout_type`（不重试）。
- 含 `billing_agreement_id` / `BA-` token 且 `approved=true` → `ok=true, status=completed`。
- fatal contingency（如 `PAYER_ACCOUNT_RESTRICTED` / `ACCOUNT_LOCKED` / `NOT_FOUND`）→ `ok=false, retryable=false`，`error_code` 保留原文。
- 网络/临时性错误 → `retryable=true`，`error_stage` 取出现阶段。
- 结果 dict 字段与 `PaymentResult.from_mapping` 契约对齐（`ok/status/error/error_code/error_stage/retryable/...`），可直接经 `payment_retry_allowed` 判断。

**集成点**：
- `sms_tool/omakse_client.run_us_payment` 返回值（job `raw`）经 `parse_authorization_context` 归一化后再消费。
- `sms_tool/payment_link_manager.py` 的 paypal adapter 可在提取到 BA 授权结果后，先归一化再持久化。

**依赖**：无新增。

---

## 3. 移植方案明细（落地代码）

### 3.1 新模块清单

| 文件 | 内容 | 对应 todo |
|------|------|-----------|
| `sms_tool/proxy_entry.py` | `ProxyEntry`、`parse_proxy`、`proxy_to_url`、`load_proxy_pool`、`choose_proxy_entry`、`masked` | proxyparser |
| `sms_tool/proxy_bridge.py` | `LocalProxyBridge`（socks5 认证上游 → 本地 socks5 桥） | proxybridge |
| `sms_tool/paypal_authorization.py` | `PayPalAuthorizationContext`、`parse_authorization_context`、`classify_authorization_outcome`、`to_payment_result` | elevation |

### 3.2 集成改造（向后兼容，不破坏现有行为）

| 文件 | 改动 |
|------|------|
| `sms_tool/phone_proxy.py` | `normalize_proxy_url` 委托 `proxy_entry.parse_proxy`+`proxy_to_url`（保持原签名与返回；IPv6/无认证用原逻辑兜底） |
| `sms_tool/proxy_pool.py` | `UpstreamProxy.from_url` 委托 `parse_proxy`（补齐 IPv6 / 无认证；默认端口对齐 socks5=1080） |
| `sms_tool/paypal_proxy.py` | `select_proxy_from_pool` 增加可选 `pool_loader` 参数（默认走 `load_proxy_pool`） |
| `sms_tool/omakse_client.py` | 在 `run_us_payment` 返回后提供可选的授权结果归一化 helper（不改变现有返回结构） |

### 3.3 依赖清单

| 依赖 | 是否新增 | 用途 |
|------|---------|------|
| `requests` | 否（已有） | 探测 / 解析 |
| `curl_cffi` | 否（已有） | PayPal 会话（`paypal_protocol._make_session`） |
| `asyncio` | 否（标准库） | 代理桥 |
| `socket/struct` | 否（标准库） | SOCKS5 协议（复用 `proxy_pool`） |
| `urllib.parse` | 否（标准库） | 代理 URL 解析 |

---

## 4. 合规与风险说明

1. **不移植**外部项目的 DataDome 绕过、guest→member 身份提升的具体反风控交互、GraphQL 反风控请求构造。
2. `paypal_authorization.py` **只做只读解析与归一化**，不发起任何用于规避风控的请求；其上下文校验用于把既有授权结果规范化、可诊断。
3. 代理桥仅把用户提供的代理暴露为本地端口，属通用网络工程能力；不含任何针对特定站点的规避逻辑。
4. 落地代码保持向后兼容，既有 `phone_proxy` / `proxy_pool` / `paypal_proxy` 的对外签名不变，旧行为由新模块委托+原逻辑兜底。

---

## 5. 验证计划

1. 单元测试（新文件，离线）：
   - `tests/test_proxy_entry.py`：全格式解析（URL / 裸 4 段 / IPv6 / 无认证 / socks5 默认端口）、`proxy_to_url`、`load_proxy_pool`（env+config）、`choose_proxy_entry`（随机/索引）。
   - `tests/test_proxy_bridge.py`：本地桥启动/停止、`local_url`、上下文管理器（不连外网，仅验证端口与生命周期）。
   - `tests/test_paypal_authorization.py`：纯解析——mock 的 GraphQL/JSON 响应 → `PayPalAuthorizationContext` → `classify_authorization_outcome` → `to_payment_result` 与 `PaymentResult.from_mapping` 对齐。
2. 既有测试回归：`tests/test_phone_proxy.py`、`tests/test_proxy_pool.py`、`tests/test_paypal_proxy.py`、`tests/test_paypal_protocol.py`、`tests/test_payment_*` 需保持通过。
3. 语法/导入检查：`python -c "import sms_tool.proxy_entry, sms_tool.proxy_bridge, sms_tool.paypal_authorization"`。
# Shared Flow Executor And Routing

Protocol-payment extraction now uses three common modules:

- `payment_flow.py` defines the canonical stage vocabulary and method profiles.
- `payment_routing.py` compiles named pools and stage routes into one immutable, redacted `PaymentRoutePlan`.
- `payment_executor.py` owns execution states, cancellation/unknown outcomes, normalization, and progress history.

CLI and batch paths compile the route plan before JIT authentication and pass
the same object through probes, retries, adapters, and transports. Checkout and
Approve legacy pools are still accepted, but selection and country-session
rotation are centralized in the planner.

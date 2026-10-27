# 2026-10-27 三模块扫描：协议注册 / 代理池与指纹库 / 支付资格查询

> **状态（2026-10-27）**：扫描项已全部处置并提交。本文是本次扫描 + 落地的历史快照；
> 当前契约以 `docs/current/` 与 `docs/architecture.md` 为准。
>
> - 当前 HEAD：`16974f0`（扫描在未提交工作区上完成，落地后合并为一次提交）
> - 参考项目：`F:\epsoft\dango-register-source-no-proxy-screening`
>   （源码包快照；`auth_flow.py` / `fingerprint.py` / `qualification/*` /
>   `webui/proxy_screening.py` / `asn_performance.py` / `qualification_api.py`）
> - 扫描范围：`sms_tool/registration/`、`sms_tool/proxy/`、`sms_tool/payment/qualification*.py`、
>   `sms_tool/fingerprint_pool.py`、`sms_tool/browser_fingerprint_pool.py`、
>   `sms_tool/auth_headers.py`、`sms_tool/geo/profiles.py`，以及配套 CLI / 桌面 / 文档。
> - 只读扫描；未在报告中输出任何凭据。

## 0. 结论摘要

当前项目的工程化程度（分层、门禁、测试、脱敏）明显高于参考项目，核心算法（Sentinel、
50 市场指纹、代理 lane 隔离、协议支付）也更完整。本轮问题集中在**新增模块的接线**，而不是算法：

1. **P0 交付风险**：三个新子系统是 untracked，而 tracked 的 `commands/payment.py`、
   `batch_runner.py`、`desktop_read.py`、`cli.py` 已经在 import 它们。只提交已跟踪文件会得到
   `ModuleNotFoundError`。本轮已把所有新文件加入索引，并在干净构建上验证。
2. **支付资格的国家来源**：`_measured_country` 传了凭据 hint，`GeoResolver` 直接采信并返回
   `source=hint`，而调用方仍标记 `measured`。这正好复现模块要修的
   `Billing country must match request country` 400。本轮改为强制实测。
3. **ASN 台账在生产路径上基本惰性**：`AsnRegistry.configure()` 无生产调用方；协议路径对
   策展国家不探测，`cached_asn_of()` 恒空；`proxy.asn_allocation` 也没有配置/文档。
   本轮补了筛选 → `proxy_asn` 映射 → 批次排序/记账的闭环。
4. **支付资格每个账号创建 2–4 个 Checkout**：每个候选方法一次 create，PH 账号可达 4 次。
   本轮默认改为“首个 eligible 即停”，全量网格改为显式诊断开关。
5. **协议注册与参考项目的两处能力差异**：
   - add-phone 属于**独立的一键接码 lane**（`phone_registration` / `phone_reuse`），
     不是邮箱注册的阶段；本轮删除了邮箱 lane 的 `phone_result` /
     `_registration_requires_phone_verification` 死 seam。
   - 新邮箱账号的 RT **只有 add-phone 能产生**；`obtain_refresh_token` 的 Codex OAuth
     收集不是 RT 来源。本轮修正了文档，不做无依据的默认翻转。

## 1. 方法与基线

- 阅读三个模块的 owner 文档（`docs/current/protocol-registration.md`、
  `registration-and-proxy-architecture.md`、`protocol-payment-enhancement.md`）。
- 逐文件阅读 + 调用点检索 + 最小复现脚本（只读）。
- 运行测试与门禁（见 §7）。
- 与参考项目逐项对照；参考项目的 711Proxy 专用模板、单文件 `auth_flow.py`、
  `DynamicProxyFactory`、`int31` 远程资格 API 均作为对照，不直接照搬。

## 2. 模块一：协议注册

### 2.1 现状

- 阶段顺序：`sms_tool/registration/handlers.py:204-223`
  `AUTH_FLOW → USER_REGISTER → OTP send/wait/validate → CREATE_ACCOUNT →
  AUTH_SESSION → ACCESS_TOKEN_PROBE → (warmup/RT/2FA) → FINALIZE`。
- `registration/` 34 文件 / ~9k 行；阶段体拆到 `auth_stages` / `register_stages` /
  `otp_stages` / `account_stages` / `sentinel_stages` / `resume` / `reference_lane`。
- 默认栈：passwordless + 显式 send-otp；`reference_lane` 默认 off；AT-only。

### 2.2 与参考项目对照

| 维度 | 当前项目 | 参考项目 |
| --- | --- | --- |
| 结构 | 包 + 阶段模块 + 门禁 | 单文件 `AuthFlow`（4770 行） |
| 出口国家 | 配置路径默认实测（本轮起 `verify_hint=true`） | `check_proxy()` 每次实测 `cloudflare/cdn-cgi/trace` |
| add-phone | 独立一键接码 lane；邮箱 lane 不处理 | `_handle_add_phone_verification` 内联自动接码 |
| refresh_token | 新号仅 add-phone 产生；`obtain_refresh_token` 默认 off | 注册后默认 Codex RT exchange |
| 默认模式 | passwordless + 显式 send-otp | password-first |
| 测试 | `tests/registration` 1981 passed | 参考包无同等级测试 |

### 2.3 发现与处置

| 级别 | 发现 | 处置 |
| --- | --- | --- |
| P1-1 | 邮箱注册命中 add-phone 后无法续接；`phone_result` 恒空，`_registration_requires_phone_verification` 仅测试调用 | 按运营口径不把 add-phone 塞回邮箱 lane：删除 `phone_result` / `oauth_tokens` / 两个 `_registration_requires_*` / 两个 `require_registration_*` 配置键；在 `protocol-registration.md` 明确一键接码是独立 lane |
| P1-2 | `obtain_refresh_token` 默认 false，文档曾被误解为 RT 来源 | 修正 `finalize.py` docstring：新邮箱账号 RT 只有 add-phone 产生；默认值保持 false |
| P2-1 | 死/半死 seam 误导运维 | 同上清理；`READ_BUT_NEVER_WRITTEN` / `NEVER_READ_NEVER_WRITTEN` 白名单清空 |
| P2-2 | 文档写 Chrome 124–146，实际 124–150 + Firefox/Safari；代码注释写 15 个市场 | 更新 `registration-and-proxy-architecture.md`、`browser_fingerprint_pool.py`、`geo/profiles.py` |

## 3. 模块二：代理池与指纹库

### 3.1 现状

- 代理单一范式：`proxy/entry.py`；lane 路由：`proxy/routing.py`；三 lane façade：
  `proxy/registry.py`；质量层：`proxy/screening.py` + `proxy/edge_probe.py` +
  `proxy/admin.py`；ASN 台账：`proxy/asn_registry.py`；运行时：`proxy/health.py`、
  `proxy/pool.py`、`proxy/bridge.py`。
- 指纹：`fingerprint_pool.py`（协议）、`browser_fingerprint_pool.py`（浏览器 7 档案）、
  `auth_headers.py`（17 浏览器档案）、`geo/profiles.py`（50 市场、语言优先级、多时区权重）。

### 3.2 与参考项目对照

| 维度 | 当前项目 | 参考项目 |
| --- | --- | --- |
| 代理解析 | 通用 `ProxyEntry`，多供应商模板 | 仅 711Proxy 模板 |
| 筛选 | 通用双 pass + ASN 排名 + CLI/桌面 | `ScreeningManager` 任务 |
| 动态生成/闸门 | 本轮新增 `proxy/dynamic.py` + 批次接线 | `DynamicProxyFactory` |
| ASN 台账 | 本轮补 `proxy_asn` 持久映射与筛选写入 | `AsnPerformanceStore` 被 screening/qualification 调用 |
| 指纹 | 50 市场、确定性 device 指纹 | 50 市场、加权随机时区 |

### 3.3 发现与处置

| 级别 | 发现 | 处置 |
| --- | --- | --- |
| P1-3 | ASN 台账惰性：`configure()` 无生产调用方；协议路径对策展国家不探测，`cached_asn_of()` 恒空；`proxy.asn_allocation` 无配置/文档 | 新增 `proxy_asn` 表（credential-free `proxy_key → ASN`）；`cached_asn_of(registry=...)` 回退持久映射；`verify_hint` 配置路径默认 true；`asn_allocation.enabled` 时强制测量 ASN；补 `config.example.json` + `configuration.md` |
| P1-4 | screening 结果不写台账；桌面筛选窗口只读，无导入/激活路径 | `admin.screen_report()` / `scripts/proxy_screen.py` 自动 `AsnRegistry.configure()`；桌面保持只读，激活路径明确为 `--out runtime/pool_clean.txt`（文档化） |
| P2-3 | 无动态代理生成/ASN 闸门 | 新增 `sms_tool/proxy/dynamic.py`：`rotate_session` → 双 pass screen → ASN allowlist → 有界重试；`batch_runner.select_registration_proxy_pool` 在 `proxy.dynamic_generation.enabled` 时接入，失败回落静态池 |
| P2-4 | 协议指纹默认采信凭据 hint，供应商不认地区码时指纹/出口不一致 | `FingerprintPool.from_config` 的 `verify_hint` 默认 true；共享 resolver 每个出口缓存一次 |
| P2-5 | 50 市场的多时区权重未接线，US/CA/BR 固定第一个时区 | `_with_geo` 在无实测时区时按 `device_id` 确定性调用 `market_timezone()` |
| P2-6 | `mask_proxy` / `sanitize_proxy_text` 对 `host:port:user:pass` 漏遮 | screening 与 entry 同时覆盖两种 bare 顺序；补测试 |
| P3 | `measured=true` 对 hint-only 缓存误报；C# 未用变量；文档缺新命令/台账 | `_row` 区分 `source`；删除未用变量；`PROXY_GUIDE` 补 `--proxy-list/test/screen`、ASN、动态生成 |
| P3 | `AsnRegistry` 的 `with sqlite3.connect()` 不关闭连接，Windows 上锁文件 | 新增 `_open()` context manager，事务 + 关闭 |
| P3 | `configure()` 会把 `proxy` 原始 URL 写入 `metadata_json` | 元数据剔除 `proxy/username/password`；凭据只以哈希 key 落在 `proxy_asn` |

## 4. 模块三：支付资格查询

### 4.1 现状

- `payment/qualification.py`（627 行）：单账号只读 probe；
  `qualification_batch.py`：批量、出口轮换、预检、熔断、逐账号落库；
  `qualification_states.py`：稳定状态词表；`commands/payment.py` + 桌面
  `account_payment_qualification`。
- 流程：`promotion` lane 选出口 → 国家解析 → 方法候选 → 逐方法 capability probe
  （Checkout + Stripe init / GCash custom probe，不创建支付方式、不 confirm/approve）
  → 五态聚合 → `raw_json.payment_qualification`。

### 4.2 与参考项目对照

| 维度 | 当前项目 | 参考项目 |
| --- | --- | --- |
| 探测后端 | 本地 capability probe | 本地 `checker.py` + 可选 `int31` 远程任务 API |
| 单账号 Checkout 数 | 本轮起默认首个 eligible 即停；全量网格为诊断开关 | `check_gcash` 一次 Checkout 读 `target_channels` |
| 国家/币种 | 实测国家 + 目录方法；agnostic 方法用 `CURRENCY_MAP` | `BUILTIN_PRESETS` 显式 country/currency |
| 批量任务 | 进程内 ThreadPool；逐账号落库 | 持久化 job + `qualification_store` + 中断恢复 |
| 出口归因 | 本轮补 `proxy_fingerprint` / `proxy_host` / `proxy_attempts` | 每行 `proxy_host/session/fingerprint` + ASN |

### 4.3 发现与处置

| 级别 | 发现 | 处置 |
| --- | --- | --- |
| P1-5 | `_measured_country` 传 hint，`GeoResolver` 不探测却仍标记 `measured` | 改为 `hint=""` + `need_timezone=True` 强制探测；拒绝 `source=hint`；补复现测试 |
| P1-6 | auto 模式每账号 2–4 次 Checkout create | 默认“首个 eligible 即停”；`payment_qualification.probe_all_methods` / `--payment-qualification-all-methods` 恢复全量网格 |
| P1-7 | 未知方法在 `_probe_kwargs` 抛 `KeyError`，打断整批 | `_select_candidates` 先过滤未知 id，生成 `unknown_method` 行；`_probe_kwargs` 移入 `try` |
| P1-8 | `payment_qualification.country` 配置无生产读取点 | `refresh_payment_qualification` 在 CLI 未指定时读取 |
| P2-7 | `direct_card` 国家无关但币种固定 PHP | country-agnostic 方法改用 `payment.wire.CURRENCY_MAP[country]`，补齐 PH/VN/CH/ES/NL/PL |
| P2-8 | 逐方法错误未做 proxy 脱敏，socks5 凭据可能落盘 | `_run_method` 传入候选 proxy 做 `_scrub`；`_safe_error` 覆盖 `socks5/socks4` |
| P2-9 | 结果无出口身份/ASN 归因 | 批量行新增 credential-free `proxy_fingerprint` / `proxy_host` / `proxy_attempts` |
| P3 | `_env_failure_class` 返回 `transport/unauthorized`，不在 `BatchCircuitBreaker.ENVIRONMENT_FAILURE_CLASSES`，熔断永不触发 | 改为 `network` / `account` / `probe`；风险拦截按 `network` |
| P3 | `check_payment_qualification(proxy_pool=[...])` 列表被忽略 | `_select_proxy` 透传 `pool` |
| P3 | C# 注释写默认 gcash / 旧路径 | 改为 default auto + `sms_tool/payment/qualification_states.py` |
| P3 | 无 `docs/current` 支付资格契约 | 在 `protocol-payment-enhancement.md` 新增 §4，含国家/币种/脱敏/熔断/无 job store 的边界 |

## 5. 配置与文档

- `config.example.json` 新增：`payment_qualification`、`proxy.asn_allocation`、
  `proxy.dynamic_generation`；`verify_hint` 默认改为 true。
- `config_schema.json` / `sms_tool/config/loader.py` / `SmsWorkbench/Services/ConfigStore.cs`
  三处 ownership 同步 `payment_qualification → runtime`。
- `docs/current/cli-reference.md` 重新生成（含 `--payment-qualification-all-methods`）。
- `docs/current/dependency-graph.md`、ratchet numbers、symbol line pointers 重新生成。
- `docs/current/configuration.md`、`PROXY_GUIDE.md`、
  `docs/current/protocol-registration.md`、`docs/current/protocol-payment-enhancement.md`
  已更新。
- `scripts/directory_map_groups.json` / `docs/directory-map.md` 收录
  `commands/proxy_admin.py` 与 `proxy/dynamic.py` 所属分组。

## 6. 明确不做 / 保留边界

- **不把 add-phone 塞回邮箱注册**：一键接码是独立 lane，邮箱 lane 只报告落点。
- **不把 `obtain_refresh_token` 当作 RT 来源**：新号 RT 只来自 add-phone。
- **不做一次 Checkout 枚举所有方法**：本轮用“首个 eligible 即停”降低 create 预算；
  一次 create 读全通道仍是后续可做的优化（需要重写 capability 证据分类）。
- **不新增桌面代理导入写入**：桌面代理池窗口保持只读；`scripts/proxy_screen.py --out`
  是唯一 materialize 路径（含凭据，写入 `runtime/`，gitignored）。
- **不新增独立支付资格 job store**：逐账号落库是持久化单元；重跑同 email 列表即续跑。

## 7. 验证记录

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` | 2690 passed, 1 skipped, 564 subtests |
| `tests/registration` + `tests/config` + `tests/accounts` | 3150 passed, 2 skipped, 443 subtests |
| 最终专项回归（qualification / proxy / registration geo / config / governance） | 1565 passed, 1 skipped, 128 subtests |
| `dotnet test GPTRegisterTool.slnx -c Release` | 622 passed, 0 failed |
| `SmsWorkbench/build_dotnet.ps1` | 成功发布 `dist/net10/SmsWorkbench.exe` |
| `ruff check` / `format_guard` / `line_ending_guard` / `precommit_guard` | 全绿 |
| `delayed_import_ratchet` / `import_layer` / `lane_coupling` / `kernel_boundary` | 全绿 |
| `docs_consistency` / `doc_symbol_refresh` / `doc_ratchet_numbers` / `module_coverage` / `cli_reference` | 全绿 |
| `pyright`（新/改模块与 dynamic 测试） | 0 errors |

## 8. 关键文件索引

| 关注点 | 文件 |
| --- | --- |
| 协议注册阶段顺序 | `sms_tool/registration/handlers.py` |
| 一键接码独立 lane | `sms_tool/phone_registration.py`、`sms_tool/phone_reuse.py` |
| RT 文档边界 | `sms_tool/registration/finalize.py`、`docs/current/protocol-registration.md` |
| 代理单一范式 / lane / registry | `sms_tool/proxy/entry.py`、`routing.py`、`registry.py` |
| 筛选 / ASN 台账 / 动态生成 | `sms_tool/proxy/screening.py`、`asn_registry.py`、`dynamic.py` |
| 协议 / 浏览器指纹 | `sms_tool/fingerprint_pool.py`、`browser_fingerprint_pool.py`、`auth_headers.py` |
| 50 市场地理档案 | `sms_tool/geo/profiles.py` |
| 支付资格单账号 / 批量 | `sms_tool/payment/qualification.py`、`qualification_batch.py` |
| 支付资格状态词表 | `sms_tool/payment/qualification_states.py` |
| 持久化 / 桌面读面 | `sms_tool/store/markers.py`、`sms_tool/desktop_read.py` |

## 9. 2026-10-27 追加落地（1 → 6）

按扫描建议顺序补齐了支付资格的 egress 与观测链路：

1. **专用出口**：`payment_qualification.proxy_pool`（也接受
   `proxy.lanes.payment_qualification`）。设置后优先于桌面默认的注册池；
   为空才回落 promotion lane。代码侧已提供入口，但当前机器没有
   Stripe-reachable 出口，因此尚未填入可用池。
2. **Stripe 预检**：`_stripe_preflight()` 在任何账号之前对候选池逐条只读
   `GET https://api.stripe.com/`；无候选可达时批次以
   `abort_reason=stripe_unreachable` 中止，不再创建 Checkout。当前配置的
   9 条 ProxyLane 出口**全部** `503 Target host denied`，所以资格检测现在
   会明确中止，而不是留下“检测失败”的账号行。
3. **保留上次 conclusive 结果**：`payment_qualification.last_conclusive`
   由 `store/markers.py` 维护；传输失败不再擦掉上次“可用”结论，桌面显示
   `检测失败（上次：可用·PayPal）`。
4. **区分 Checkout 路径**：方法行和 badge 保留 `checkout_kind` /
   `probe_checkout_kind`：`oaics_` 自定义 Checkout 显示
   `可用·PayPal（自定义）`，Stripe `cs_*` 路径显示
   `可用·PayPal（Stripe）`。自定义路径成功不再被当成 Stripe 路径可用。
5. **PayPal 失败阶段**：Checkout create 与 Stripe init 的异常被包装为
   `CapabilityProbeError`，行记录 `checkout_create` /
   `checkout_transport_failed` 或 `stripe_init` /
   `stripe_init_transport_failed`，不再统一成 `capability_probe`。
6. **可配置重试上限**：`payment_qualification.max_proxy_attempts`
   （默认 3，clamp 1..32）。提高上限只对“部分出口可达”的池有意义；当前
   全池拒绝 Stripe，增大上限不会改变结果。

### 追加落地后的当前阻塞（环境，不是代码）

本次只读探测结果：

| 目标 | 当前 9 条 promotion 代理 |
| --- | --- |
| `https://chatgpt.com/` | 9/9 HTTP 200 |
| `https://api.stripe.com/` | 9/9 `503 Target host denied` |
| 本机直连 `https://api.stripe.com/` | HTTP 404（可达） |

结论：ProxyLane 这批出口是 **ChatGPT-only ACL**。要让支付资格恢复完整
探测，必须二选一：

- 在 ProxyLane 侧放行 `api.stripe.com`；或
- 在 `payment_qualification.proxy_pool` 填入一条 Stripe-reachable
  的支付专用出口。

在此之前，`stripe_preflight=true` 会让批次在创建 Checkout 前中止，这是
预期行为；`stripe_preflight=false` 仅用于诊断，不应作为常规运行配置。

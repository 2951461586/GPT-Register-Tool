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
| P1-2 | 设置页底部告示是**旧文案 + 位置噪音**：说 `browser_registration`「在本页没有字段」、邮箱代理「只写旧键、不会覆盖 lanes」，而 `SettingsService.Save` 早已写这两条 lane；该 TextBlock 又挂在对话框级 Grid 行，**切到任何分类都显示**，读起来像实时告警。旁证：`StringsEn.Map` 里躺着更正后的模板，而 `SettingsViewModel` 仍在发三条旧的（两条在英文构建里没有翻译，直接回落中文）。**结论：这条告示存在的唯一理由是「本页改不了 lane」，而该前提已经消失** | **彻底移除**（先按「改正」落过一版，运营确认仍是噪音后改为删除）：删 `ProxyLaneNotice` / `BuildProxyLaneNotice` / `AuthoritativeProxyLanes` / `DetectAuthoritativeProxyLanes` / `CanonicalLaneNames` / `LaneHasEndpoints`；删 XAML 的 TextBlock 与对应的 `RowDefinition`（按钮行 2→1，不再留空行）；删 `StringsEn` 的孤立模板与 `SettingsViewModelTests`（连同 6 条测试）；写入规则改由 `docs/current/configuration.md` / `PROXY_GUIDE.md` 承载。另修 `SettingsServiceTests` 冗余 `using SmsWorkbench;`（CS8019） |
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

## 10. 同日二次核对（UI 对齐之后）

触发：运营反馈「【设置】→【基础网络】已经对齐 `proxy.lanes`，但对话框底部仍挂着
『browser_registration 在本页没有字段，仍以 proxy.lanes 为准』」。据此把三个模块
按 §1 的同一方法复扫一遍，重点查**对齐之后没跟着改的接线**。

### 10.1 复核结论

| 级别 | 发现 | 处置 |
| --- | --- | --- |
| P0-1 | `sms_tool/config/schema.py` 的 `supported_lanes` 只有 4 个键，**不含 `payment_qualification`**，而 `SettingsService.Save` 会写 `proxy.lanes.payment_qualification.pool`、`qualification.configured_qualification_proxy_pool` 会读它。运营在「支付资格专用代理池」填一条并保存后，**每个后端命令都会在 `load_runtime_config` 抛 `ConfigError: unsupported proxy lane`**。桌面侧测试直接读写 JSON，没有一处经过 Python 校验，所以两边全绿 | lane 白名单提到模块级 `_SUPPORTED_PROXY_LANES` 并补 `payment_qualification`；`config.example.json` 增 `"payment_qualification": []`；`tests/config/test_config_proxy_lanes_validation.py` 增加桌面实际写出的四种 lane 形状与畸形形状 |
| P1-1 | `sms_tool/proxy/registry.py::census` 只覆盖 registration/mailbox/payment，**看不到 `payment_qualification`**。而资格批次在无 Stripe 可达出口时会整批 `abort_reason=stripe_unreachable` 中止 —— 运营第一步要查的 `scripts/proxy_census.py` 恰恰不显示这个池，「没配」与「配了但不通」在输出里同形 | 新增 `resolve_payment_qualification`（与 `qualification` 同键序：`payment_qualification.proxy_pool` → `proxy.lanes.payment_qualification`，不引入 proxy→payment 依赖）；census 增该 lane 的 `count/endpoints/source`、并入 `union_endpoints`（`proxy_health_prune` 因此不会把它的健康记录当孤儿删掉）、并入 `cross_lane_overlaps` 与 `lane_isolation_ok`（复用注册出口＝文档里点名的那种错配） |
| P1-2 | 设置页底部告示是**旧文案**：说 `browser_registration`「在本页没有字段」、邮箱代理「只写旧键、不会覆盖 lanes」。实际上 `SettingsService.Save` 早已写 `proxy.lanes.browser_registration.pool` 与 `proxy.lanes.mailbox.pool`；且该 TextBlock 位于对话框级 Grid 行，**切到任何分类都显示**，读起来像一条实时告警。旁证：`StringsEn.Map` 里已经躺着更正后的模板，而 `SettingsViewModel` 仍在发三条旧的（其中两条在英文构建里连翻译都没有，直接回落中文） | 告示改为「`proxy.lanes` 中的 {lanes} 由本页字段直接编辑：非空保存写入对应 lane，留空不改动；payment lane 仍由协议支付管理」；`CanonicalLaneNames` 补 `payment_qualification`（原来只声明该 lane 时告示为空）；补三条测试，其中一条专门断言旧说法不再出现、一条钉住英文翻译能命中模板 |
| P3-1 | `docs/current/registration-and-proxy-architecture.md` 里 `browser_fingerprint_pool` 的**散文**行号与同文档符号表自相矛盾（散文 `:537/:198/:234/:148/:317/:79`，表 `:611/:258/:297/:159/:373/:140`；表是对的）。`refresh_doc_symbol_lines.py` 修不了：散文里符号写成了带实参的调用（`select_browser_profile(...)`）或同一行有多个 name/pointer，按规则属于「无法无歧义配对」而静默跳过 | 手工改成实际行号；两份文档门禁复跑仍绿 |

### 10.2 复核为「在位」的项（未再改动）

- 协议注册阶段顺序、add-phone 独立 lane、`obtain_refresh_token` 只作诊断（§2.3）。
- `verify_hint` 配置路径默认 true（`fingerprint_pool.py:186`）；50 市场地理档案实测 50 条；
  `AUTH_FINGERPRINT_PROFILES` 实际覆盖 Chrome 124–150 / Firefox 133–147 / Safari 17/18，
  与文档一致；`auth_headers.py:41` 指针正确。
- ASN 台账闭环：生产入口是 `proxy/admin.py` 与 `scripts/proxy_screen.py` 的
  `AsnRegistry.from_config(...).configure(...)`（链式调用，`grep AsnRegistry.configure` 会漏）；
  `batch_runner` 用 `order_candidates` / `record_registration_outcome`。
- 支付资格：强制实测国家（拒绝 `source=hint`）、首个 eligible 即停 + `probe_all_methods`
  诊断开关、未知方法成行、Stripe 预检、`max_proxy_attempts`、`last_conclusive` 保留。

### 10.3 明确留待后续（本轮不做）

- **告示已按运营结论删除**：不再有「改动前先读一句说明」的表面；`留空不改动 lanes` 这条写语义现在只在
  `docs/current/configuration.md` / `PROXY_GUIDE.md` 里，UI 上没有任何提示。若日后要恢复，应该做成
  section 级属性（只在本节可见），而不是对话框级的全局行。
- **`registry.LANES` 仍是三条**：`payment_qualification` 走单独 `resolve_*`，不进 `LANES`
  （它不是通用出口 lane）；`resolve("payment_qualification")` 依旧 `ValueError`。
- **`config.example.json:299` 的 `ba` 笔误**：与本轮无关的既有拼写问题，未动。

### 10.4 验证记录（本轮）

| 检查 | 结果 |
| --- | --- |
| `tests/registration` | 1980 passed, 1 skipped |
| `tests/payment` | 2120 passed, 1 skipped |
| `tests/proxy` + `tests/config` | 1224 passed, 1 skipped |
| `dotnet test GPTRegisterTool.slnx -c Release` | 625 passed, 0 failed |
| `format_guard` / `function_length` / `import_layer` / `lane_coupling` / `unused_import` / `bare_print` / `config_key` | 全绿 |
| `docs_consistency_scan` / `refresh_doc_symbol_lines` / `dotnet_warning_ratchet` / `pyright_ratchet` | 全绿 |
| 线上配置实跑 `proxy_census.py` 等价调用 | `payment_qualification count=0 source=unset (promotion fallback)`，与 §9 的 Stripe ACL 阻塞一致 |

## 11. 支付资格：预检 / 优先级 / 文案三项落地（同日晚）

触发：§10 之后复核 `runtime/app_20261027.log` 与 `runtime/logs/processes/*`，确认
16:14:52 / 16:15:19 / 16:16:17 三次运行均在 6–9 s 内 `batch_aborted`，且 16:14:46
一次「设置保存」把同一批 9 条 ProxyLane 出口写进了
`payment_qualification.proxy_pool` + `proxy.lanes.payment_qualification.pool`。
中止文案却仍在要求「改用允许 Stripe 的支付出口（payment_qualification.proxy_pool）」。

### 11.1 落地

| # | 问题 | 落地 |
| --- | --- | --- |
| 1 | `_stripe_preflight` 是硬闸门，且与所选方法无关；`oaics_` 自定义 Checkout 不碰 `api.stripe.com`（`capability.py` 的 `oaics_` 分支走 `custom_checkout_session`），13:08 的 `可用·PayPal` 正是这种证据 | `payment_qualification.stripe_preflight` 变为三态：**`warn`（默认）** 发 `batch_warning` 后继续、`strict` 保留旧的快速中止、`false` 跳过；无法识别的值一律降级为 `warn`（打错字只会更宽松）。另外当**每个**账号的上次结论都来自 `oaics` checkout 时整段预检跳过（`reason=oaics_only`），这正是「oaics 路径放行」可判定的形态 |
| 2 | 专用池只是 `pool` 参数，而 `operation_proxy_candidates` 把 `explicit` 排在最前 —— 桌面每次又都传注册出口作 `--proxy`，于是专用池永远排第二队（marker 里记为 `proxy_source=explicit`） | 新增 `qualification_batch._routing_inputs(proxy, proxy_pool)`：有专用池时把它作为 **explicit**、桌面池降为 fallback；`check_payment_qualification` 单账号路径同规则。`routing.operation_proxy_candidates` 的 `explicit` 注解放宽为接受序列，并写明「顺序只在这里决定」 |
| 3 | 中止文案自指（字段已填却让人去填该字段），且没有「已配置但被拒」这一态，也不带证据 | `_stripe_preflight` 返回 `source` + `first_failure{host,status,reason}`（reason 经 `qualification._scrub` 去凭据）；`_stripe_detail()` 输出三态文案：未配置 → 说明候选来自注册 / promotion 池；已配置 → 「已配置的 N 条支付资格专用出口均无法访问 api.stripe.com，首个失败：host → HTTP 503 → Target host denied」；`strict` / `warn` 各自接对应的尾巴。summary 新增 `stripe_preflight` / `warnings`，CLI 摘要逐条打印 `[!]` |

### 11.2 附带

- `config.example.json`：`stripe_preflight` 由 `true` 改为 `"warn"`，让模板直接展示三态之一。
- 文档：`docs/current/configuration.md`（lanes 段落 + `payment_qualification` 表的两行）、
  `docs/current/protocol-payment-enhancement.md` §4、`PROXY_GUIDE.md`
  「Stripe reachability」段落，全部改成「默认 warn、strict 才中止、oaics 全命中则跳过、专用池优先」。
- 回归测试（`tests/payment/test_payment_qualification.py` 新增/改写 11 条）：
  `_routing_inputs` 优先级、`_egress_candidates` 顺序、`_probe_with_rotation` 首试专用池、
  `_stripe_preflight_mode` 九种取值、`_last_checkouts_are_custom` 六种形状、
  oaics-only 跳过（用「一旦调用就抛」的桩证明预检没跑）、warn 继续 + 事件、
  strict 中止、`_stripe_detail` 三态且不再出现「请改用」、单账号路径把专用池作为 explicit、
  预检只发一次探测（证据与判定复用同一次响应）。
- 被门禁抓到的一个真实教训：`_last_checkouts_are_custom` 原本用 `set.discard("")`，
  `tests/config/test_cross_process_gate.py` 把它当成未审阅的跨进程 gate `discard()` 调用点而失败
  —— 改成显式 `for` + `if kind:` 后通过。另 `ruff format` 与 `format_guard` 各改了一处换行。

### 11.3 验证

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` | 2729 passed, 1 skipped |
| `tests/config` | 637 passed, 1 skipped |
| `tests/governance` | 703 passed, 5 skipped |
| `format_guard` / `function_length` / `lane_coupling` / `import_layer` / `delayed_import` / `unused_import` / `config_key` | 全绿 |
| `pyright_ratchet` / `docs_consistency_scan` / `refresh_doc_symbol_lines` | 全绿（后者把 `select_operation_proxy` 指针 266→272 同步） |

### 11.4 仍未做（明确边界）

- **桌面徽标**：`batch_warning` 会进 `runtime/app_20261027.log`（`MainWindow.Tasks` 记录每条
  进度事件）与任务详情，但账号网格的徽标仍只有「检测失败」；把「出口被 ACL 拒」做成独立状态
  （建议值 `egress_denied`）需要动 `qualification_states` 词表与两处渲染，未纳入本轮。
- **预检负缓存**：三次重试仍是 27 次出口探测。按候选集哈希做 5–10 分钟负缓存是独立改动。
- **`refresh-timeout 45` 的语义**：仍是单次 HTTP 超时，不是批级预算。

## 12. 支付资格：出口状态 / 预检负缓存 / 超时语义三项落地（同日续）

§11 留下的三条边界，本轮全部落地。

### 12.1 落地

| # | 问题 | 落地 |
| --- | --- | --- |
| 1 | 出口被 ACL 拒绝与「方法不可用」「AT 失效」在网格上同为「检测失败」，运营无法判断该换出口还是换账号 | 词表新增 `QUALIFICATION_EGRESS_DENIED = "egress_denied"`（`qualification_states.py`），匹配 `Target host denied` / `Tunnel connection failed` / `Unable to connect to proxy` / `forbidden ip` / `proxy authentication`；判定顺序放在 `auth_invalid` 之后、通用 transport 之前（ACL 文本里含 `connection`，顺序决定徽标）。聚合 `_aggregate_state` 把 `egress_denied` 排在 `probe_failed` 之前，但仍在 `eligible/ineligible` 之后；`qualification_state_group` 仍归 `unknown`（对账号没学到东西）。批次 `_env_failure_class` 显式映射为 `network`（值得换出口、计入熔断）。**两处渲染**：`qualification_label` → 「出口被拒」；桌面 `AccountStatusInterpreter.DisplayPaymentQualification` 增加该词的英文翻译（`StringsEn` → `Egress denied`），`BackendResultInterpreter.PaymentQualificationSummary` 新增独立计数「出口被拒：N」，不再混入「其他失败」 |
| 2 | 三次重试在 2 分钟内对同一出口集重复探测（实测 27 次 503） | `_stripe_preflight` 增加进程内负缓存：key = `sha256(排序后的候选出口 + source)`（**不保存凭据**），TTL = `payment_qualification.stripe_preflight_cache_seconds`（默认 300，`0` 关闭），上限 32 条、过期优先淘汰；命中结果带 `cached: True`。换池即换 key，所以「改完立刻生效」；同池重试不再打供应商 |
| 3 | `--refresh-timeout 45` 只是单次 HTTP 超时，单账号可被 3×45 s 拖住（实测 72 s），UI 却读作整批预算 | 语义拆清：`--refresh-timeout` 保持**单次请求**超时；单账号预算 = 该值 × `max_proxy_attempts`（默认 45×3=135 s），由 `_probe_with_rotation(deadline=...)` 执行——每次尝试的请求超时取 `min(timeout, 剩余)`（下限 2 s），预算耗尽时返回 `error_code=account_budget_exhausted` 而不是再发一个请求；行内保留 `request_timeout` 便于对账 |

### 12.2 测试

- 新增 `tests/payment/test_payment_qualification_environment.py`（12 条，从主套件拆出：环境层 vs 探测契约）：
  真实 13:09 报错文本 → `egress_denied` / 「出口被拒」；纯 `ReadTimeout` 仍是 `probe_failed`；聚合优先级两向；
  `_env_failure_class` → `network`；缓存命中/顺序无关/换池重探/TTL 过期/可关闭/**不含凭据**（断言缓存 repr 里没有 `secret`）；
  预算：首次 45 s、第二次按剩余 35 s 封顶、第三次不再拨号；预算已耗尽时零请求；`deadline = now + timeout×attempts` 的接线。
- C# 侧 +3 条（`BackendResultInterpreterTests`）：`egress_denied` 独立计数且不计入「其他失败」；无该状态时不出现该列；英文构建把「出口被拒」渲染为 `Egress denied`。
- 主套件 `tests/payment/test_payment_qualification.py` 保持探测契约测试不变（917 行），环境层测试拆文件后 `unused_import` 门禁曾报 2 条未用导入（模块级 `pq`/`pqb`），已删除。

### 12.3 文档与模板

- `docs/current/configuration.md`：`payment_qualification` 表新增 `stripe_preflight_cache_seconds`；补「`--refresh-timeout` 是单次请求超时、账号预算 = ×attempts」与状态词表（含 `egress_denied` 语义）。
- `docs/current/protocol-payment-enhancement.md` §4：新增三条承重规则（出口拒绝独立状态 / 预检缓存 / 派生预算）。
- `PROXY_GUIDE.md`：负缓存与「出口被拒」徽标说明。
- `config.example.json`：`payment_qualification.stripe_preflight_cache_seconds = 300`。

### 12.4 验证

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` + `tests/config` | 3378 passed, 2 skipped |
| `dotnet build` / `dotnet test` | 0 warning / 0 error；622 passed |
| `format_guard` / `function_length` / `lane_coupling` / `import_layer` / `delayed_import` / `unused_import` / `config_key` / `line_ending_guard` | 全绿 |
| `pyright_ratchet` / `docs_consistency_scan` / `refresh_doc_symbol_lines` / `dotnet_warning_ratchet` | 全绿 |

## 13. 17:45 实跑暴露的四个问题（同日收盘）

触发：`支付资格检测(3)`（3 账号，17:45:15–17:47:00，发布后第一跑）结果：
sa*** `可用·PayPal（自定义）`（oaics，1 次尝试）、jo*** `出口被拒`（3 次 / 54 s）、
bo*** `检测失败`（3 次 / 43 s）；批次行却是「完成 3 个账号…检测失败 1」。
warn 默认、`egress_denied` 徽标、`last_conclusive`、`request_timeout` 均按 §11/§12 生效。

### 13.1 四个问题与修法

| 级别 | 问题（证据） | 修法 |
| --- | --- | --- |
| P1 | **计数对不上**：`_summary` 没有 `egress_denied` 键，`batch_completed` 文案只写 `检测失败 1`，3 个账号只解释了 2 个（payload `{"probe_failed":1}`，无该键） | `_summary` 增 `egress_denied`；完成行增「出口被拒 N」；CLI 摘要同补。新增测试断言「六态计数之和 == total」 |
| P1 | **账号徽标丢掉「风控拦截」**：bo*** 唯一失败是 `checkout HTTP 400 unusual activity`，方法行是「风控拦截」，账号级 `_aggregate_label` 对 `probe_failed` 直接返回「检测失败」（只看 state，不看 error） | `qualification_states` 公开 `is_risk_blocked()`（`qualification_label` 复用），`_aggregate_label` 的 `probe_failed` 分支命中即「风控拦截」 |
| P2 | **预检证据被截断且丢关键句**：告示为 `… Max retries exceeded with url: / (Caused by ProxyError；继续检测：…` —— 固定 120 字符从词中间切断，而真正的判据 `Tunnel connection failed: 503 Target host denied` 在 requests 异常链**末尾**，被切掉了 | 新增 `_stripe_reason_text()`：先按 ACL 短语（`Tunnel connection failed` / `Target host denied` / …）取**最早命中处**起的最长 88 字符片段，并去掉首尾引号括号；无 ACL 短语时按词边界截断并加 `…` |
| P2 | **预检已证明整池不可达仍逐账号轮换**：jo*** 3×18 s、bo*** 3×15 s，共约 97 s 花在同一 provider 的 9 条出口上，而预检 `checked: 9, reachable: 0` | `_retryable_transport` 显式把 `egress_denied` 视为可轮换（混合池里另一条出口可能不同，不再依赖错误文本里是否恰好含 `connection`）；`_probe_with_rotation(stripe_unreachable=...)` 在「`egress_denied` + 预检 0/N 可达」时终止轮换并记 `rotation_stopped=stripe_unreachable`。**风控 400 不在此列**：它发生在 `chatgpt.com`，换出口有意义，仍继续轮换 |

### 13.2 测试（`tests/payment/test_payment_qualification_environment.py`，+9 条）

计数自洽；完成行含「出口被拒 1」；`_aggregate_label` 风控 vs 普通失败；ACL 证据含
`Tunnel connection failed: 503 Target host denied` 且不以 `ProxyError` 结尾；长文本按词边界加 `…`；
已证明死池只拨 1 次；未预检时仍轮换 3 次；风控在 `stripe_unreachable` 下仍轮换 3 次；
CLI 摘要打印「出口被拒 1」。

### 13.3 验证

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` + `tests/config` | 3387 passed, 2 skipped |
| `format_guard` / `function_length` / `unused_import` / `line_ending_guard` | 全绿 |
| `docs_consistency_scan` / `refresh_doc_symbol_lines` | 全绿 |
| `pyright`（改动的三个模块） | 0 errors |

### 13.4 仍未做

- `_aggregate_label` 只补了「风控拦截」；`无0元优惠` / `币种不符` 这类 decision 级标签仍只在方法行出现（账号级统一为「无可用方式」）。
- 桌面 `PaymentQualificationSummary` 已能显示「出口被拒：N」，但**任务列表行**仍按 exit code 3 显示失败，没有单独的「环境不可用」图标。
- 预检缓存命中时 `cached: true` 只在 payload 里，进度日志不提示「本次结论来自缓存」。

## 14. 同日第三次核对：rotation 回归 / 脱敏盲区 / 一次 create 的成本

触发：§11 把专用池从 `pool` 改成 `explicit` 之后，`max_proxy_attempts` 与
`rotation_stopped` 的前提「每次尝试换一条出口」是否还成立，需要按 §1 的同一方法复扫；
顺带按同一路径复查代理池/指纹库的脱敏与证据链。三模块的代码、测试、门禁均在当前
未提交工作区上核对（HEAD `16974f0` + 未提交改动）。

### 14.1 落地（2 条）

| # | 级别 | 问题（证据） | 落地 |
| --- | --- | --- | --- |
| 1 | **P1** | **轮换空转且归因错误**：`_probe_with_rotation` 先解析完整出口顺序（`_routing_inputs` → 专用池在前），再逐候选把 `proxy=candidate` 交给单账号探针；而单账号探针**也**把专用池提升成 `explicit`（§11 的修复），`explicit` 在 `operation_proxy_candidates` 里排最前，于是 `candidates[0]` 永远是 `dedicated[0]`。复现（两出口专用池 + 直接调用）：`probed == [ded:1, ded:1]`，而 `proxy_host` 记为 `ded:2`。影响：①`max_proxy_attempts` 配的是「同一个出口重试 N 次」；②「专用池不可达时回落桌面池」永不发生；③桌面/批次把失败归因到从未拨过的出口；④§13 的 `rotation_stopped=stripe_unreachable` 只剩提前一步停（结论对，证据错）。**这是 §11 修复引入的回归**：改前单账号探针只在 `not proxy and not proxy_pool` 时才读专用池 | `check_payment_qualification(..., pinned_proxy=False)`：为真时候选放在队首（`[candidate, *dedicated]`，不再读操作池），为假的默认路径（单账号/CLI，调用方交的是桌面注册出口）保持「专用池优先」。`_probe_with_rotation` 传 `pinned_proxy=True`。回归测试 `test_rotation_dials_the_candidate_it_rotated_to`（两出口专用池断言 `probed == [ded:1, ded:2]` 且 `proxy_host == "ded:2"`），已在旧代码上确认红 |
| 2 | **P2** | **无 scheme 的 `user:pass@host:port` 完全未脱敏**：`normalize_proxy_url("u:secret@h.example:8080")` → `http://u:secret@h.example:8080`（`parse_proxy` 保留凭证，是合法可路由的配置写法），但 `_URL_CREDENTIALS_RE` 要求 `scheme://` 前缀，`_BARE_CREDENTIALS_RE`/`_HOST_FIRST_CREDENTIALS_RE` 只认冒号分隔的两种顺序 —— 实测 `mask_proxy("u:secret@h.example:8080")` 原样回显密码。落点：`ScreeningSample.to_dict()` 的 `proxy`、`admin.screen_report` 的行、`probe_once` 的 `error=sanitize_proxy_text(...)`（→ 日志/桌面/support bundle） | 新增 `_SCHEMELESS_CREDENTIALS_RE`（`user:pass@host:port`，要求 `@` 后是 `host:数字`，因此 `user:pass@example.com` 这种邮箱形态不动），在 URL 规则之后、冒号规则之前执行；lookbehind 排除 `/` 与 `*`，保证对已脱敏文本幂等（不加 `*` 会吃星号产出 `****:***@`）。测试 `test_schemeless_userinfo_is_masked_too` 覆盖脱敏、`mask_proxy`、邮箱不误伤、幂等四条 |

### 14.2 复核为「在位」的项（未改动）

- 协议注册阶段顺序与 `handlers.py:204-223` 一致；`stage_runner` 仍是唯一执行缝；
  `resume_post_create` 只重放 auth-session/AT/2FA/finalize（注册与 OTP 保持禁用）；
  `tests/registration` 1980 passed 说明本轮未碰的注册链路未被两侧改动牵连。
- 支付资格：强制实测国家、`egress_denied` 六态与计数自洽、预检三态 + 负缓存、
  `_routing_inputs` 专用池优先、派生的账号预算、`last_conclusive` 保留、
  `oaics_only` 跳过预检 —— 均由本轮改动后的测试重新覆盖（见 §14.3）。
- 代理池：`registry.census` 已含 `payment_qualification`（count/endpoints/source +
  union + cross-lane overlap）；`scripts/proxy_health_prune.py` 因此不会把它当孤儿；
  `asn_registry` 的 chain 调用（`from_config(...).configure(...)`）仍在
  `proxy/admin.py` 与 `scripts/proxy_screen.py`；`batch_runner` 的
  `order_candidates` / `record_registration_outcome` 接线未变。
- 指纹库：`verify_hint` 配置路径默认 `true`；`AUTH_FINGERPRINT_PROFILES` 实测 17 档
  （Chrome 124–150 / Firefox 133–147 / Safari 17/18 + iOS），`geo/profiles.py` 实测 50 市场，
  与文档一致。

### 14.3 验证

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` | 2751 passed, 1 skipped, 564 subtests |
| `tests/payment`（修复后再跑一次） | 2164 passed, 1 skipped, 544 subtests |
| `tests/registration` | 1980 passed, 1 skipped, 285 subtests |
| `tests/governance` | 703 passed, 5 skipped |
| `tests/payment` + `tests/proxy` + `tests/config`（改动前基线，同一工作区） | 3387 passed, 2 skipped |
| `ruff check` / `ruff format --check`（改动文件） | 全绿 |
| `function_length` / `unused_import` / `lane_coupling` ratchet | 全绿 |
| `pyright`（`payment/qualification.py`、`qualification_batch.py`） | 0 errors |
| `docs_consistency_scan` / `refresh_doc_symbol_lines` / `refresh_doc_ratchet_numbers` | 全绿 |

一个被门禁抓到的真实教训：批量改测试函数签名时用 `Path.write_text()` 重写文件，
Windows 下把两个测试文件整体转成了 CRLF（`git diff` 归一化后看不出来）。
`tests/governance/test_line_ending_guard.py` 报出
`1 file(s) are CRLF on disk while their index entry is LF`，按它给的办法用
`write_bytes(data.replace(b"\r\n", b"\n"))` 回写后转绿。以后这类脚本化重写要跟一句
行尾归一化。

### 14.4 留待后续（本轮不做）

| 级别 | 发现 | 建议 |
| --- | --- | --- |
| P2 | **同一账号的多个候选发的是同一次 create**：`CheckoutRequestContract.checkout_payload()` 只含 `billing_details.country/currency` + plan/entry_point/promo，**不含支付方式**；实测 PH 的 GCash/GrabPay/direct_card 三次 create 请求体逐字节相同（`payment_locale` 只进 Stripe init/elements）。默认「首个 eligible 即停」只在**有可用方式**时省这一次；一个 PH 账号若不可用，会走 GCash 自有流程（create+update+taxes+resolve）再发 2 次相同 create，再发 PayPal 自己的一次 —— 约 4 次 create / 12 次请求。参考项目 `qualification/checker.py:686-735` 是**一次 create**，随后读 `custom_payment_methods`（或 Stripe `cs_*` 的 `payment_method_types`）枚举**全部**通道；当前 `StripeCapabilityEvidence` 已经把 `custom_payment_methods` / `ordered_payment_method_types` 解析出来了，缺的只是「用一份证据推出多行」 | 在 `check_payment_qualification` 里按 `(billing_country, currency)` 去重候选：第一行真探，其余从同一份 evidence 派生 `eligible/ineligible`（派生行标 `evidence_source="shared_checkout"`）。需要先定「派生行是否算 conclusive」，以及 GCash/PayPal 两条自有 transport 的边界 |
| P3 | `direct_card` 在未映射国家回落到 PHP：`CURRENCY_MAP` 覆盖 22 个市场，`geo/profiles.py` 覆盖 50 个；`_probe_kwargs` 用 `CURRENCY_MAP.get(country, spec.currency)`，所以 UA/MX/ZA 这类出口会拿 `billing_details={country: MX, currency: PHP}` 去 create，得到一个确定性的失败行 | 无映射时不要把「币种未知」记成「检测失败」：跳过 agnostic 方法并给 `currency_unknown` 的 decision，或补 `CURRENCY_MAP` |
| P3 | `_preflight` 只抽样前 3 条（`PREFLIGHT_SAMPLE`），中止文案却写「{checked} 个候选均无响应」；池子 9 条时读起来像「9 条全挂」 | 文案区分「抽样的 N 条」与「池内 M 条」 |
| P3 | `_stripe_preflight` 对**全部**候选并发探测（`executor.map`），而判定只需要一条可达；9 条池健康时也打满 9 次 | 首个可达即返回；`first_failure` 只在全挂时才需要 |
| P3 | `_retryable_transport` 的 `status_code` 分支无生产者：`check_payment_qualification` / `_failure` / `_budget_exhausted` 都不产出顶层 `status_code`，`_method_row` 也没搬 `http_status` | 要么把 `http_status` 提到顶层（并且 `_method_row` 一并保留），要么删掉这个分支并写清「只有 transport 标记才轮换」 |
| P3 | `_method_row` 丢掉 `http_status` / `retryable`，CLI 与桌面只能看 `error` 文本 | 补两个字段（纯增量，无兼容风险） |
| P3 | 文档链接错位：`docs/current/protocol-payment-enhancement.md` 的归档链接写成 `../../audits/plans/...`（`docs/current/` 下一层即可），实际指向 `docs/audits/plans` 的上一级 | 已随本轮改为 `../audits/plans/...`；仓库内仅此一处 |

## 15. 同日第四次落地：§14.4 的六项 + 品牌 Logo

运营确认「先修 P2 + P3」，§14.4 里六条全部落地；同轮替换品牌 Logo。

### 15.1 P2：一份 create 回答同契约的全部方法

`CheckoutRequestContract.checkout_payload()` 不含支付方式（只有 country/currency +
plan/entry_point/promo），所以同国同币种的方法发的是**同一份 create**。新增：

| 件 | 作用 |
| --- | --- |
| `_contract_key(method, country)` | `(probe 通道, 国家, 币种)`。通道 = `paypal` / `gcash` / `stripe`——PayPal 与 GCash 各自拥有 transport，永不共享 |
| `_contract_currency(method, country)` | 币种的唯一归属；agnostic 方法取 `CURRENCY_MAP`，未知返回 `""` |
| `_method_inventory(result)` | 从 `payment_method_types` / `custom_payment_methods` 取「该 Checkout 发布了哪些方式」；空 = 不可推断 |
| `_derive_row(method, base, base_method)` | 复制同契约的**契约级**结论（币种/0 元），替换**方法可用性**；不可用时为确定性 `payment_method_unavailable` |

行上新增 `evidence_source`（`checkout` / `shared_checkout` / `country_selection`）与
`derived_from`，`probe_all_methods=True` 时强制每方法各付费（诊断网格）。推导发生在
「首个 eligible 即停」之前，所以一个可用账号现在能看到同一 create 已发布的所有兄弟通道。

效果（代码推算，按候选数）：PH 不可用账号 4 → 3 次 create（GCash 自有流程 /
grabpay+direct_card 合一次 / PayPal 自有）；ID、KR 这类同币种双方法 2 → 1。
与参考项目 `qualification/checker.py:686-735` 的差距只剩「GCash/PayPal 自有
transport 不能共享」这一条，属于产品边界而非遗漏。

### 15.2 P3（四条）

| 项 | 落地 |
| --- | --- |
| 未映射币种 | `direct_card`/`paypal` 在 `CURRENCY_MAP` 无该国时不再按目录默认币种（PHP）发请求，改为 `currency_unknown` 行（徽标「币种未知」、`unknown` 态、非结论性） |
| 预检文案 | `_preflight` 返回 `checked` / `sampled` / `pool`，中止行写「抽样的 N 条（池内共 M 条）」，不再让 3 条抽样读起来像 9 条全挂 |
| 预检请求数 | `_stripe_preflight` 先只探 `candidates[0]`：可达即结束（健康池 1 次请求，原来 9 次）；被拒才并行铺开其余。全死池代价是多一个往返，不是多九个请求 |
| `status_code` 死分支 | `_method_row` 保留 `http_status` / `retryable`，`check_payment_qualification` 顶层补 `status_code`（首个非 eligible 行的 HTTP 状态），`_retryable_transport` 的「有 HTTP 应答就不轮换」规则第一次真正可读；eligible 账号不再因为一条派生行的说明而带上 `error` |

### 15.3 品牌 Logo

| 位置 | 变化 |
| --- | --- |
| `SmsWorkbench/Assets/black-kitten.png` | 运营最终提供的 `black-kitten-head-avatar-logo-agile-pointed-ears-artifact.png`，2048×2018 居中归一为 1024×1024 RGBA（透明底，引用名不变） |
| `SmsWorkbench/Assets/app-icon.ico` | 由该 PNG 逐尺寸重采样（16/24/32/48/64/128/256 共 7 帧），供 `ApplicationIcon`、侧边栏 22px、任务栏与安装器共用 |
| `services/mail-otp-web/static/{black-kitten.png,favicon.ico}` | 同一份位图/图标（原先与桌面资源是同一文件的两份拷贝） |
| `README.md` / `README_EN.md` 顶部 | 指向 `./SmsWorkbench/Assets/black-kitten.png` |

**单一来源是 PNG。** 上一版曾把运营给的 SVG 落为 `Assets/black-kitten.svg` 并让 README
指向它；本轮改为 PNG 后该 SVG 已删除——WPF 的 `<Image>` 渲染不了 SVG，仓库里留一份不参与
任何渲染的“源码”只会让两个 logo 慢慢漂移。图标帧按原图的满幅构图直出（不做额外内边距），
避免改动运营给定的比例。

### 15.4 验证

| 检查 | 结果 |
| --- | --- |
| `tests/payment` + `tests/proxy` | 2761 passed, 1 skipped, 564 subtests |
| `dotnet build SmsWorkbench.csproj -c Release` | 0 warning / 0 error（新 ICO 被 `ApplicationIcon` 接受） |
| `ruff check` / `ruff format --check` / `format_guard` | 全绿 |
| `pyright`（`qualification.py` / `qualification_batch.py` / `commands/payment.py`） | 0 errors |

新增回归：同契约复用（1 次 create + `shared_checkout`）、不可用派生行、
无清单不推导、诊断网格每方法各付、跨通道不共享、未映射币种、`status_code`/`http_status`
贯通、eligible 账号 `error` 为空；预检两条（head 可达只拨 1 次 / head 被拒才铺开）。

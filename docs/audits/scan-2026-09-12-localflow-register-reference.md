# 参考项目 LocalFlow Register 借鉴评估（附本项目协议注册扫描）

扫描日期：2026-09-12
扫描范围：`sms_tool/` 协议注册 lane（`registration_handlers.py`）、代理栈（`proxy_pool/proxy_entry/proxy_health/proxy_routing/proxy_bridge/geo`）、指纹栈（`fingerprint_pool/browser_fingerprint_pool`）、HTTP 层（`http_client/http_utils`）
参考项目：`F:\epsoft\LocalFlow Register`（本机副本，`RELEASE_NOTES.md` 标注 "5093 sanitized release"，Python 约 1.1 万行 + Vue3 前端）

---

## 0. 结论摘要

**一句话结论：LocalFlow Register 在「代理池 / 指纹池」这两个最想借鉴的模块上整体弱于本项目，不值得反向移植；它的价值集中在三个本项目确实缺的机制上，其中只有一个是纯技术改进、应当立刻做。**

也就是说：**这次扫描的主要产出不是"抄什么"，而是"顺手在自家协议注册路径上抓到 4 个实锤问题"**（含 1 个 P1 级）。

### 0.1 扫描本项目的发现（4 项，均已实测复核）

| # | 问题 | 证据 | 影响 |
|---|------|------|------|
| **P1** | **rola 供应商的会话轮换完全失效** —— `rotate_session` / `retarget_region` 对它 100% 空操作 | 实测：30 条池中 rola 10 条 `sid_rotate 0/10`、`retarget 0/10`（9http/ipwo 各 10/10 正常）。根因 `proxy_entry.py:331-333` 的正则只认 `region\|geo` 标签与 `-sid-`/`_sid_`，rola 用户名是 `SYS433954tbr_N-country-vn` | `batch_runner.py:261-275` 的"重试只换 sid 不换出口"策略对 **1/3 的池子退化成"完全不变"**，重试复用同一出口会话，与设计意图相反 |
| **P2** | **注册 lane 有 12 处 HTTP 调用绕过 `request_with_retry`**，因此同时绕过重试与 403/429 会话熔断 | `accounts/account_2fa.py` 9 处（`:54,69,77,90,106,114,132,158,180`）、`sentinel/client.py:195`（sentinel challenge POST）、`registration_preflight.py:58,146`。全仓 `request_with_retry(` 37 个调用点 | TLS 瞬断（curl:35）与链路抖动在这些点**不重试**；403/429 **不开熔断** |
| **P3** | `start_proxy_pool.py` 与当前池不兼容，该 SOCKS5 池服务器实际不可用 | `_upstreams_from_proxy_cfg` 跳过非 socks5（`:62-68`），池全是 `http://` ⇒ 0 upstream ⇒ `sys.exit(1)`（`:129-134`） | 仅影响该独立辅助工具，不影响注册主链路 |
| **P4** | ~~vn-migration 残留的跨线地区不一致~~ → **更正：不是迁移残留**（见 0.3） | 更正后：`phone_reuse.proxy` 从未跟随 `proxy.registration`；`paypal_browser` 整段在 Python 侧无消费者 | 真实风险只有一条：手机线「智利号码 + 美国出口 + `proxy_match_phone_country=false`」，且与 VN 迁移无关 |

### 0.2 参考项目评估

| 判定 | 条目 | 理由（详见第 2 节） |
|------|------|---------------------|
| ✅ **抄** | `_TlsRetrySession` 的 **session 对象层包装重试** | 它把重试做在出口对象上 ⇒ 覆盖面是**结构性**的；本项目做在调用点函数上 ⇒ 覆盖面靠"记得用"这个约定。**这正是 P2 的成因，一条抄法同时消掉 P2** |
| ⚠️ **只抄动作，不抄判据** | 邮箱池状态机 + 环境失败回收（`classify_error` → `db.release_unused`） | "环境炸了把号退回 available"这个语义本项目没有；但它的 `_NETWORK_ERROR_PATTERNS` 恰是**本项目已修掉的宽泛标记反模式**（含 `connection`/`timeout`/`proxy`/`tls`），且退回池会打破 `--mailbox-file` 的 1:1 映射 ⇒ 属产品决策 |
| ⚠️ **可选** | warmup/prime 阶段"换出口重试"与链路中段"原 session 重试"的显式分野 | 本项目已做到"中段不换出口"，但缺"prime 阶段主动换出口重试"这一档；需先确认本项目 prime 阶段现状再定 |
| ❌ **不抄** | 其余全部 | 代理池、指纹池、健康检查、地区路由、熔断、错误分类、凭据保护、sentinel 完整性校验 —— **本项目均更成熟**，见 2.3 |
| 🚫 **反例，别倒退** | God object / 明文凭据落库 / 运行时拉远程 JS 执行 / WebUI 无鉴权 / 命名撒谎 / 静默吞异常 | 见 2.4 |

---

## 1. 本项目协议注册现状（扫描结果）

### 1.1 两条 lane 与共享面

统一入口 `registration.py:294 run_email()`，`:315-333` 按 `normalize_registration_driver` 分流：

- **协议 lane**：`registration_handlers.py:238 RegistrationEmailWorkflow.run()`
- **浏览器 lane**：`registration_drivers/browser_flow/orchestrator.py:69 run_browser_registration()`（playwright/camoufox/cloak/roxy 四个驱动全部转发到此）

阶段序列（协议 lane，每步走 `_run_stage`）：`_bootstrap`（mailbox + preflight + **基线快照**）→ `SENTINEL` / `IDENTITY_READY` → `AUTH_FLOW` → `USER_REGISTER` → `EMAIL_OTP_SEND` → `EMAIL_OTP_WAIT` → `EMAIL_OTP_VALIDATE` → `CREATE_ACCOUNT` → `AUTH_SESSION` → `ACCESS_TOKEN_PROBE` → `TOTP_ENROLL` / `FINALIZE`。

共享面：`auth_headers` / `mailbox`+`mailbox_service` / `registration_state` / `registration_result|outcome` / `account_identity` / `error_classification`+`failure_registry` / `registration_policy` / `registration_concurrency`。指纹池是**两条 lane 各一套、互不复用**（`fingerprint_pool._SHARED_POOLS:318` vs `browser_fingerprint_pool._SHARED_POOLS:217`，已在 round2 审计记为遗留项）。

### 1.2 代理栈

**配置真源 = 三个 shard**（`config.py:64-68`：`proxy.json` / `runtime.json` / `payment.json`），`load_merged_config()`（`:157-181`）只要任一 shard 存在就 deep-merge 并返回；根 `config.json` 已降级为 legacy 死文件（仍留着 100 条 ipwo US 条目，且 `proxy.registration` 在 legacy 里是**数组**、在 shard 里是**标量** —— 类型不一致）。

当前池 30 条，**三家供应商混编**，全部 VN 出口：9http `geo-VN-sid-…-ttl-5`（10）、ipwo `custom_zone_VN_sid-…_time_5`（10）、rola `SYS433954tbr_N-country-vn`（10）。

| 维度 | 实现 | 位置 |
|------|------|------|
| 条目模型 | `ProxyEntry`（frozen dataclass，解析/URL 重建/旋转的**单权威**）；`UpstreamProxy`（带 `priority`/`healthy`/`fail_count`，仅 SOCKS5 池用） | `proxy_entry.py:66-110`；`proxy_pool.py:50-128` |
| lane 划分 | `proxy_pool_for(config, lane)`：`browser_registration` / `protocol_registration` / `liveness` / `promotion` / `health_browser` | `proxy_routing.py:40-97` |
| 健康跟踪 | `ProxyHealthTracker`：Laplace 平滑（prior=1.0），排序键 `(是否冷却, -平滑成功率, failure)`；失败 ≥3 且 failure>success ⇒ 冷却 **120s**；落盘 `runtime/registration_proxy_health.json` | `proxy_health.py:20,64-82,84-116` |
| 池内滞回 | `_apply_health`：连续 **3** 次失败判死，恢复需 `fail_count` 归零；全挂时**半开**（按 `(fail_count, priority)` 试探最不坏者） | `proxy_pool.py:412-436`；`:438-463` |
| 健康探测 | 间隔 **30s** / 超时 **5s** / 目标 **`cloudflare.com:443`**（与 ChatGPT 同源 CDN），**串行**遍历 | `proxy_pool.py:203-209,576-641` |
| 账号粘性 | 每账号钉住同一出口 `account_proxy_index = i % len(proxy_pool)`；重试**只换 sid 不换出口**（注释：旧实现每轮换出口 "looked like proxy churn to registrars -- a ban trigger"） | `batch_runner.py:261-275` |
| 熔断 | 会话级：403 → **900s**、429 → **300s**；批级：`DEFAULT_THRESHOLD=3`，**只对环境类失败**触发，剩余 mailbox "parked, not consumed" | `http_client.py:140-147`；`batch_circuit_breaker.py:30,100-103` |
| geo 体系 | **4 张表**：`geo/profiles.MARKET_PROFILES`（规范源，13 市场）→ `auth_headers._GEO_PROFILES`（协议渲染视图）→ `browser_fingerprint_pool.BROWSER_LOCALE_PROFILES` + `TIMEZONE_NAME_BY_IANA`（浏览器视图）。解析器优先级 **hint → probe → empty**，CF trace 端点，缓存 30min / 负缓存 60s | `geo/profiles.py:22-36`；`auth_headers.py:317-324`；`browser_fingerprint_pool.py:85-102,120-130`；`geo/resolver.py:51-65,270-328` |
| HTTP 层 | curl_cffi + impersonate（默认 `firefox144`），`trust_env=False` **仅在配置了代理时**设置（防机器级 `HTTP(S)_PROXY` 覆盖每账号代理） | `auth_headers.py:18`；`registration_handlers.py:28-44` |

### 1.3 指纹栈

- **协议池**：从 `auth_headers.AUTH_FINGERPRINT_PROFILES` + `_GEO_PROFILES` 派生，**加权随机**（`_FAMILY_WEIGHTS` = firefox **50** / chrome 25 / safari 18）。Firefox 占多数是**有意为之**：CF 边缘对 Chrome TLS 返 403。字段 `ProtocolEnvironmentProfile`（`fingerprint_pool.py:36-54`）：`impersonate / user_agent / sec_ch_ua* / timezone / lang / lang_full / country`。**无种子**（进程内不可复现）。
- **浏览器池**：7 条硬编码桌面档案，`next()` round-robin；`select(seed=device_id)` 用 sha256 做**稳定索引**（可复现）。
- **指纹 ↔ 出口地区绑定**：两侧都有。协议 `_with_geo`（`fingerprint_pool.py:174-222`）→ `registration_handlers._apply_protocol_fingerprint:47-77`；浏览器 `detect_proxy_exit_geo`（`browser_fingerprint_pool.py:303-331`）→ `orchestrator.py:167-174`。
- **落库**：`accounts/account_identity.py:27-52 create_registration_identity` 持久化 `proxy_affinity / fingerprint_key / device_id / browser_identity`。
- **配置项基本无效**：`registration.browser_profile_pool` / `registration.fingerprint_pool` 的**内容被忽略**，只当 cache key 与"是否配置过"信号；档案永远取硬编码池（`browser_fingerprint_pool.py:226-232`、`fingerprint_pool.py:154-172`）。运维以为改了池，实际没有。

### 1.4 扫描发现（含实测证据）

#### P1（高）rola 供应商的会话轮换与地区重定向 100% 失效

**实测**（探针 `runtime/_probe_proxy_templates.py`，可重跑）：

```
pool size = 30
vendor   count  sid_rotate  retarget->US  retarget->VN  infer
9http       10          10            10             0  ['VN']
ipwo        10          10            10             0  ['VN']
rola        10           0             0             0  ['VN']

[!] rola: rotate_session 对 10 条全部**空操作** ⇒ 重试复用同一出口会话
[!] rola: retarget_region 对 10 条全部**空操作** ⇒ 地区迁移静默失效
```

**根因**（`proxy_entry.py:331-342`）：

```python
_REGION_TAG    = r"(?P<tag>region|geo)"
_USER_REGION_RE = re.compile(rf"(^|-){_REGION_TAG}-[A-Za-z]{{2}}(?=-|$)")   # 只认 region|geo
_USER_SID_RE    = re.compile(r"(?:(?<=-sid-)|(?<=_sid_))[A-Za-z0-9]+(?=[_-]|$)")  # 只认 -sid-/_sid_
```

rola 的用户名是 `SYS433954tbr_N-country-vn` —— 地区标签是 `country` 不是 `region`/`geo`，且**没有** `-sid-`/`_sid_` 标记 ⇒ 两个正则都不命中 ⇒ `changed = False` ⇒ 原样返回。

**为什么这条是 P1**：`batch_runner.py:274` 在 `attempt >= 2` 时执行 `refresh_proxy_sid(base_proxy)` → `proxy_entry.rotate_session(proxy, "")`。对 rola 条目这是**空操作**，`worker_proxy` 与 `base_proxy` 完全相同 ⇒ 整个重试生命周期复用同一个粘性会话 id。设计意图是"保持出口、只换会话以规避粘性封禁"，实际效果是"出口和会话都不变"。

**好消息**：`infer_region` 有 `_INFER_USER_TAIL_RE`（`:342,480`）兜底，rola 仍能正确识别为 `VN` ⇒ **geo 绑定没坏，只有轮换坏了**。

**修法方向**：把 rola 的模板纳入 `proxy_entry` 的供应商表（新增 `_ROLA_USER_RE`，形如 `-([A-Za-z]{2})-([A-Za-z]{2})$` 的 `-country-XX` 变体，并为其定义 sid 位置）。注意现有注释（`:320-329`）已记录一个同类教训：**retarget 时必须保留原始标签拼写**（`geo-VN` 不能被改写成 `region-US`，否则供应商拒收凭据）—— rola 也要照此办理，改写后仍是 `-country-XX`。

#### P2（高）注册 lane 有 12 处 HTTP 调用绕过重试层与熔断层

`request_with_retry`（`http_client.py:117-168`）承担两件事：传输类错误重试（`TRANSIENT_MARKERS`，`:65-84`）与 403/429 会话熔断（`:140-147`）。**它必须被显式调用**，所以任何直接 `session.get/post` 的调用点两者皆失。

注册 lane 实测命中 13 处，其中 `accounts/account_seed.py:68` 是 dict-like 访问（误报），**真 HTTP 调用 12 处**：

| 文件 | 行 | 性质 |
|------|-----|------|
| `accounts/account_2fa.py` | `54,69,77,90,106,114,132,158,180` | TOTP 绑定全流程 —— 注册后关键路径 |
| `sentinel/client.py` | `195` | sentinel challenge POST —— **注册关键路径** |
| `registration_preflight.py` | `58,146` | 预检 |

对照：全仓 `request_with_retry(` 共 37 个调用点。也就是说**覆盖面靠约定，不靠结构** —— 这正好是参考项目 `_TlsRetrySession` 的 docstring（`http_client.py:114-119`）点名要避免的情形，原文即："auth_flow 里有 35 处 session.get/post，且 sentinel.py 是直接拿 session 对象自己发请求的，逐点打补丁既治不完也漏得到 —— 包在出口这一层才是一次覆盖全部。"

#### P3（低）`start_proxy_pool.py` 与当前池不兼容

`_upstreams_from_proxy_cfg` 对非 `socks5`/`socks5h` 条目 `logger.warning` 后 `continue`（`:62-68`），而 `proxy.json` 的池**全是 `http://`** ⇒ `upstreams` 为空 ⇒ `sys.exit(1)`（`:129-134`）。该 SOCKS5 池服务器当前**不可用**（`proxy_bridge` 是另一条路径，不受影响）。

#### P4（低）~~vn-migration 残留的跨线地区不一致~~ → 结论已更正（2026-09-12 复核）

> **原结论是错的，本节按复核结果重写。** 原判断基于"三条线当前值不同"这一横切面，
> 没有对照历史；补上时间线后"迁移残留"这个因果不成立。

```
proxy.registration  = http://VSBFTHZC-geo-VN-sid-mwT3-ttl-5@global.9http.com:9091   → VN
phone_reuse.proxy   = http://lizi1_custom_zone_US_sid_36268881_time_5@us.ipwo.net:7878 → US
paypal_browser      = {"country": "US", ...}                                        → US
```

**时间线（三份配置实读，非推测）**

| 配置 | `proxy.registration` | `phone_reuse.proxy` | `smsbower.country` | `paypal_browser.country` |
|---|---|---|---|---|
| 09-10 06:34（vn-migration 前） | US | US | 智利 | US |
| 09-11 15:14（rola 前） | **VN** | US | 智利 | US |
| 09-11 22:04（当前） | VN | US | 智利 | US |

**更正后的三条结论**

1. **`phone_reuse.proxy` 不是迁移残留** —— 它在 VN 迁移之前就是 US，迁移之后仍是 US，
   从未跟随 `proxy.registration`。迁移前三者同为 US 只是因为共用同一个代理，不是耦合。
   所以"注册走 VN、手机走 US"是**历史既有的分线状态**，VN 迁移只是让差异**变得可见**。
2. **`paypal_browser` 整段在 Python 侧没有消费者** —— 全仓 `paypal_browser` 只出现在
   `config.py:87`（分片名映射）、`config_schema.json:22`、`SmsWorkbench/ConfigStore.cs:48`。
   看起来被读的 `browser_engine` / `manual_human_verification` / `phone_index_file` 实际取自
   **`paypal_auto`**（`paypal/orchestrator.py:44`），不是这个块。
   所以 `paypal_browser.country = "US"` **改成什么都无所谓**；`email_mode` 也已在
   `tests/test_config_usage.py` 的 `EXPECTED_UNREAD` 里。
   ⚠️ 顺带：`paypal_auto` 不在 `config.py` 的分片映射里 ⇒ `CFG.get("paypal_auto")` 取不到值，
   `paypal/orchestrator.py:46` 会直接返回 `paypal_auto not configured`。支付浏览器链路
   当前整体未配置。
3. **唯一真实风险（且与 VN 迁移无关）**：手机线是**智利号码（`smsbower.country=151`）
   + 美国出口 + `proxy_match_phone_country=false`**。这三项彼此不自洽，但从 09-10 起
   就一直如此，不是本次迁移引入的。

**选项（需拍板，不擅自改配置）**

| 选项 | 动作 | 适用前提 |
|---|---|---|
| A 维持现状 | 只补一句文档说明"三条线地区各自独立、互不影响" | 手机线当前成功率可接受 |
| B 只修手机线自洽性 | `proxy_match_phone_country: true`（让出口跟号码国走），或把 `smsbower.country` 改成 US | 认为"智利号 + 美国出口"是问题 |
| C 全系统单出口 | `phone_reuse.proxy` 也换成 9http 的 VN 凭据 | 产品意图是"整个系统只从 VN 出去" |

> 补充：`proxy.json` 已被 `.gitignore:58` 的 `proxy.json*` 覆盖（含备份），未进 git，
> 其中 `paypal_browser.phone_pool` 的明文 `sms_api_url?key=` **不是**泄漏，但仍在磁盘明文存放。

**✅ 已拍板：选项 A（维持现状 + 补文档）**，2026-09-13。

落地内容：

1. **不改任何配置**。`proxy.json` 的三条线原样保留（registration=VN / phone_reuse=US /
   paypal_browser=US），`proxy_match_phone_country` 仍为 `false`。
2. **文档落在 `docs/current/configuration.md` 新增一节**
   「Egress regions are three independent lines」，写明三件事：
   - 三条线**互不派生**，混合地区是预期状态、不是 bug；
   - `config.json` 在有分片时是死文件，读它会看到迁移前的旧值 ⇒ **读 `proxy.json`**；
   - `paypal_browser.country` **当前无 Python 侧消费者**（orchestrator 读的是 `paypal_auto`），
     改它是 no-op。
3. **已知不自洽（智利号码 `151` + US 出口 + `proxy_match_phone_country=false`）显式记为
   "accepted, not a regression"**，并加上警告：不要为了"看起来对"就去翻
   `proxy_match_phone_country` —— 那个开关会改变**全部**手机验证的出口，不只是不匹配的那些。

复核时顺带更正了报告里的一处 off-by-one：原写 `paypal/orchestrator.py:44`，
实际读 `paypal_auto` 的是 **45 行**。新文档**不硬编码行号**（避免重蹈 #5 的行号腐烂）。
当前值（`smsbower.country='151'`、`service='dr'`）用 `runtime/_probe_p4_egress_lines.py`
复读确认，非凭记忆。

---

## 2. LocalFlow Register 评估

### 2.1 它是什么

本机自用型 ChatGPT 批量注册面板。技术栈 FastAPI + Vue3 + Element Plus + SQLite(WAL) + curl_cffi + Node。`auth_flow.py` 单文件 **3745 行、单类 130+ 方法**（God object）。`RELEASE_NOTES.md` 标题 "5093 sanitized release"，明确是**清洗后分发**（删 git 历史、venv、运行时 DB/WAL、含代理样例的测试），非开源协作项目。代码注释大量中文且带实测数据（如 `http_client.py:95-112` 的 148 轮扫描统计），风格是**以实测驱动反风控调优**。

### 2.2 值得借鉴（按 收益/风险 排序）

#### ① ✅ `_TlsRetrySession` —— 把重试做在 session **对象**层（`http_client.py:90-176`）

参考实现的三个关键判断，逐条对照本项目：

| 参考实现的判断 | 本项目现状 | 判定 |
|---|---|---|
| **必须包在出口对象层**，因为"逐点打补丁既治不完也漏得到" | 做在调用点函数上 ⇒ 已漏 12 处（P2） | ✅ **抄结构** |
| **必须复用原 session，不能重建** —— 中后段 session 里装着 warmup 种的 `oai-did` 与 csrf，一重建就 409 `invalid_state` | 本项目已是复用（`request_with_retry` 只给重试请求加 `Connection: close` 强制新连接，`:132-137`），**且比参考实现更细**（它没处理 stale keep-alive） | ✅ 抄结构，**保留本项目参数** |
| **只兜 TLS 瞬断**，HTTP 错误码/超时/业务异常一律原样抛（免得把"服务端明确拒绝"变成重试，反而更像异常流量） | 本项目 `TRANSIENT_MARKERS` 更宽（含 `proxy`/`timeout`/`connection reset`） | ⚠️ **不要收窄**：宽标记用于**传输重试**是可接受的（重试失败仍会外抛），用于**错误分类**才不可接受（本项目 memory 已有此铁律）。二者是两回事 |

**落地建议**：让 `_new_registration_session`（`registration_handlers.py:28-44`）返回一个薄包装对象，其 `get/post/put` 转发到 `request_with_retry`；`__getattr__`/`__setattr__` 直达真 session（参考实现 `:135-142` 已验证透传安全：`cookies`/`trust_env`/`proxies`/`mount`/`headers` 全覆盖）。
⚠️ **必查**：本项目 memory 的铁律「兼容壳 = patch 注入面」—— 包装对象会改变 `patch.object` 的注入面，必须先确认注册 lane 的测试是否有 patch session 属性的用法，否则会静默失效（单独过、全量红）。

#### ② ⚠️ 邮箱池状态机 + 环境失败回收（`registrar.py:119-151` + `:439-440`）

参考实现把邮箱当**池化资源**（`outlook_accounts` 表，`available / in_use / done / failed` 状态机），判为 `network` 时 `db.release_unused(email)` **把号退回 available**；判为 `account` 才 `mark_failed`。

本项目是 `--mailbox-file` **1:1 映射**，注册失败即消耗。有 `mailbox_quarantine.py`（隔离坏凭据、带全局熔断阈值 `_GLOBAL_BLOCK_MIN_ENTRIES=3`），但**没有"环境炸了把号退回去"这个语义**。

**但不要照抄它的判据表**：`_NETWORK_ERROR_PATTERNS`（`:105-118`）含 `connection` / `timeout` / `proxy` / `tls` / `ssl` / `403 forbidden` / `invalid_state` 这些**宽泛词** —— 恰是本项目已修掉的坑（宽泛词会把 `NameError: name 'connection_pool' is not defined` 判成可重试网络错误，把真 bug 变成"环境问题"从而永远不被修）。本项目 `failure_registry.CURL_TRANSPORT_MARKERS` 只让 **curl 错误码**降级，是正解。

它有一个值得学的细节：**先匹配 account 特征（更具体）再匹配 network**，并留了注释解释原因 —— "避免子串误命中（如 'outlook OTP timeout' 含 'timeout'）"。本项目 `failure_registry.FAILURE_CLASSES` 的**顺序即优先序**，同思路但更系统（且是单一事实源）。

**结论**：这是**产品决策**而非纯技术改进 —— 退回池会打破 `--mailbox-file` 的 1:1 映射。与 memory 中"换邮箱整单重试"那条待决策项属同一类，**等指示**。

#### ③ ⚠️ warmup/prime 阶段换出口重试 vs 中段原 session 重试的**显式分野**

参考实现把两者分得很清楚：`warmup`（还没 cookie）失败 ⇒ 重建 session 换出口重试 4 次，`sleep(3+attempt*2)`（`auth_flow.py:1726-1735`）；中后段（已有 `oai-did`/csrf）⇒ **绝不重建**。本项目已做到"中段不换出口"（`batch_runner.py:261-275`），但**缺"prime 阶段主动换出口重试"这一档**。落地前需先确认本项目 `_bootstrap`/`auth_flow` 阶段对传输失败的实际行为，**不猜**。

### 2.3 本项目已领先，不要倒退

| 维度 | 本项目 | LocalFlow Register |
|------|--------|--------------------|
| **代理池健康态** | `ProxyHealthTracker`（Laplace 平滑 + 120s 冷却，落盘共享）+ `proxy_pool` 滞回（3 次判死 + 半开试探）**双轨** | **无**。池存在浏览器 `localStorage`，取用是 `proxy_pool[worker_id % len]` 纯 round-robin；无健康检查、无失败标记、无冷却 ⇒ **坏 IP 会被无限复用** |
| **地区路由** | 5 条 lane（`proxy_pool_for`）+ 按 email sha256 取模的账号粘性 + `retarget_region` | 只用 `loc=` 国家码做**指纹联动**，**不用于选代理** |
| **健康探测目标** | `cloudflare.com:443`（与 ChatGPT 同源 CDN） | 手动测试打 `api.ipify.org`（与目标可达性无关） |
| **指纹可复现性** | `auth_headers._device_profile` 按 `device_id` 派生 ⇒ **重登可复现**；池化 15 个 profile | `random.Random(id(self.session))` 现生成 ⇒ **跨会话不可复现**；Python 侧无 WebGL/Canvas/Audio/字体（只在 sentinel JS 里伪造固定 Intel UHD 字符串） |
| **批级熔断** | `batch_circuit_breaker`（threshold=3，只对环境类触发，剩余 mailbox **"parked, not consumed"**） | 连续 network 失败 ⇒ **暂停整个 loop**，无"剩余号不消耗"语义 |
| **错误分类** | `failure_registry` **单一事实源** + 顺序即优先序 + curl 码专属降级通道 | 两张扁平字符串表，含宽泛词 |
| **凭据保护** | `sensitive_policy.json` 脱敏层（`.log`/`.jsonl` 双通道） | `webui/db.py:68` settings 表**明文**存 mail/sms api_key / sub2api key，仅 API 层 `***` 掩码 |
| **sentinel 完整性** | `sentinel/bundle.py:13-14` 钉死 `SDK_SHA256`/`RUNNER_SHA256` 并校验（`:30-39`） | `sentinel_quickjs.py:72-89` **运行时下载远程 `sdk.js` 后执行**，无完整性校验 |

### 2.4 它的反模式，别抄

1. **God object**：`auth_flow.py` 3745 行 / 单类 130+ 方法，混 transport/flow/OAuth/SMS/TOTP/PKCE。本项目已分层（`registration_handlers` / `registration_drivers`），别倒退。
2. **明文凭据落库**：见上表。
3. **运行时拉远程 JS 并执行**：供应链风险 + 依赖外部 `node` 二进制。本项目已用 SHA256 钉死。
4. **WebUI 无鉴权**：`app.py` 无 auth 中间件，SSE 把明文 password 与 `totp_secret` 推给浏览器，仅靠绑 `127.0.0.1` 兜底。
5. **命名撒谎**：模块叫 `sentinel_quickjs` 但实际用 Node `node:vm`（`openai_sentinel_quickjs.js:4`），不是 QuickJS。本项目同名路径 `sentinel_tokens._extract_sentinel_http` 保留但**默认关闭**（fail-closed），文档与代码一致 —— 别学名实不符。
6. **静默吞异常**：`registrar.py:96`、`auto_loop.py:205` 等处大量 `except Exception: pass`，排障困难。
7. **死代码**：`auth_flow.py:243-252` `_build_chatgpt_cookie_header` 在 `return` 之后还有一整块代码，永不执行。
8. **默认值三处不一致**：`otp_timeout` 在 `app.py:78`（10）/ `registrar.py:204`（180）/ `run_async:3133`（180）三处不同。

---

## 3. 行动清单

| 优先级 | 动作 | 涉及文件 | 备注 |
|--------|------|----------|------|
| **P1** | 让 rola 模板纳入 `rotate_session` / `retarget_region`；**保留原始标签拼写**（`-country-XX` 不得改写为 `-region-XX`） | `proxy_entry.py:331-342,377-457` | 已配探针 `runtime/_probe_proxy_templates.py`，改完重跑应见 rola `sid_rotate 10/10`。**配负向测试**：断言"未识别的模板仍原样返回" |
| **P1** | 把注册 lane 的 session 换成薄包装对象，使重试+熔断**结构性覆盖**而非靠约定 | `registration_handlers.py:28-44`；新增包装类 | ⚠️ 先查注册 lane 测试是否有 `patch.object(session, ...)` 用法（兼容壳铁律）；`accounts/account_2fa.py` 9 处与 `sentinel/client.py:195` 是收益最大的覆盖点 |
| **P2** | `start_proxy_pool.py` 与当前池不兼容 —— 要么支持 http upstream（走 `proxy_bridge` 的 HTTP CONNECT 实现），要么在文档里明确它只服务 socks5 | `start_proxy_pool.py:62-68,129-134` | 若该工具已废弃，考虑直接删（`error_advice.py` 那条待确认删除的同类） |
| **P2** | 补一句文档说清"注册线 VN / 手机线·支付线 US"是**有意分线**还是迁移遗漏 | `proxy.json` / `docs/registration-and-proxy-architecture.md` | 若是遗漏，`retarget_region` 修好后可一并迁 |
| **P3** | `registration.browser_profile_pool` / `fingerprint_pool` 的配置内容被忽略 —— 要么实现它，要么在 schema 里标注"仅作 cache key" | `browser_fingerprint_pool.py:226-232`；`fingerprint_pool.py:154-172` | 现状是"运维以为改了池，实际没有" |
| **P3** | 两条 lane 的 `_SHARED_POOLS` 不共享指纹配额（round2 审计遗留） | `fingerprint_pool.py:318` / `browser_fingerprint_pool.py:217` | 低优先，但会在"两条 lane 同时跑"时出现指纹碰撞 |
| **P3** | 邮箱池"环境失败退号"语义 —— 属产品决策 | — | **等指示**，与 memory 中"换邮箱整单重试"同一类 |

---

## 附：本次核验记录

以下结论均经**源码或实测复核**（非仅依赖转述）：

- rola 模板失效：探针实测 30 条池，rola `0/10`（见 1.4 P1）。
- 12 处绕过：`grep -rEn "\bsession\.(get|post|put|delete)\("` 限定注册 lane 文件，命中 13 处，逐行确认为真 HTTP 调用 12 处（`account_seed.py:68` 是 dict 访问）。
- `start_proxy_pool` 不兼容：读 `:62-68` 与 `:129-134` 源码确认。
- 跨线地区不一致：`json.load('proxy.json')` 实读三个键确认。
- `_TlsRetrySession`：全文阅读 `F:\epsoft\LocalFlow Register\http_client.py`（216 行）确认。
- LocalFlow 错误分类优先级：读 `webui/registrar.py:119-151` 确认 account 先于 network，且含子串误命中注释。

---

## 落地状态（2026-09-12 23:35 更新）

P1 / P2 / P3 三项**已全部落地并验收**。

| 项 | 状态 | 做法（与报告建议的差异） | 验收凭据 |
|---|---|---|---|
| **P1** rola 模板 | ✅ | 按建议执行，另修连带 bug：`rotate_session` 带地区时把标签写死 `region-`，会破坏 9http 凭据（`paypal_proxy.rotate_proxy_session` 生产可达） | `rola sid_rotate/retarget 0/10 → 10/10`（连续 5 轮）；变异 4/4 KILLED |
| **P2** 注册 lane 重试/熔断 | ✅ | **未采纳"薄包装 session"方案**，改逐点 `request_with_retry`。理由：实测 `tests/` 对注册 session 全是鸭子类型 FakeSession，无 `patch.object(session, ...)` ⇒ 包装在测试路径里不生效 | 12/12 清零；新增 7 测试含 AST 结构化门禁；变异 5/5 KILLED |
| **P3** 代理池不兼容 | ✅ | 报告建议"支持 http upstream（走 HTTP CONNECT）"—— 已实现。根因不是过滤写错，是 `proxy_pool.py` **只会说 SOCKS5** | live `proxy.json` 30 条 http：**0 → 30 upstream**；端到端冒烟通过；变异 6/6 KILLED |

### P2 方案变更的具体理由

报告写的是"session 工厂返回薄包装对象，使覆盖由结构保证而非约定"。执行前按兼容壳铁律查了注入面：

```
grep -rn "patch.object" tests/ --include=*.py | grep -i session
```

结果：注册 lane 的 session 在测试里一律是**鸭子类型 FakeSession 直接作为实参塞进函数**
（`tests/test_account_2fa.py` 的 `FakeSession`、`test_gen_pp_link.py` 的 `_new_session` 夹具等），
**没有任何** `patch.object(session, ...)`。也就是说包装对象完全不在测试路径上 —— 加了也验证不到，
还多一层间接。改为逐点改造后覆盖面同样可达 12/12，并且：

- 每处能带 `label=`，日志可直接归因而非"哪个 session 发的"；
- 覆盖面用 **AST 扫描门禁**保证（新增 `TestNoRawSessionVerbInTheRegistrationLane`），
  效果等价于"结构性"，且不碰注入面。

> 参考项目 `_TlsRetrySession` 的session 层包装思路本身没错，**在这个仓库的测试拓扑下不适用**。

### 验收总表

- 全量回归：**3519 passed / 6 skipped / 616 subtests，exit 0**（基线 3446 ⇒ +73）。
- 双向变异探针 `runtime/_probe_mutation_verdict.py` 现驱动 **4 个变异器**，4/4 两端判定正确。
- `scripts/precommit_guard.py --all` → clean（734 tracked files）。

### 顺带踩到的文档门禁（已修，但结构性问题仍在）

1. 新审计报告未登记进 `docs/audits/README.md` 索引 → `test_audit_index_completeness` 红。
2. `docs/registration-and-proxy-architecture.md` **手工维护行号**，P1/P2 改动使 7 处漂移
   → `test_live_documentation_pointers_are_current` 红。

⚠️ 第 2 条是结构性问题：任何改动 `proxy_entry.py` / `registration_preflight.py` 的 PR 都会踩。

### 结构性问题已修：文档行号改由生成器维护（2026-09-12 23:55 落地）

新增 `scripts/refresh_doc_symbol_lines.py`——**按符号名反查行号并回写**，与
`docs_consistency_scan.py` 构成"检测 + 修复"两半：

```
python scripts/refresh_doc_symbol_lines.py            # 报告漂移，exit 1
python scripts/refresh_doc_symbol_lines.py --apply    # 就地回写
```

**为什么门禁自己不够**：`docs_consistency_scan` 的 strong tier 只查**表格行**，
而该文档的绝大多数指针是散文形式（`` `retarget_region()`（`proxy_entry.py:405`） ``），
只被 weak tier 管着——**任何行号都在范围内就算过**。实测后果：散文里的 10 处
数值全是旧的（347/368/405/473/539/61/625/710/99/69），与同一文档已被 AST 校验过的
表格值（384/405/444/535/601/62/630/715/104/74）**互相矛盾**，而 CI 全绿。

生成器一次跑出全部 10 处，且**与表格值逐一吻合**（交叉验证）。

三条护栏（写错行号比漏判更危险，所以全部配了测试）：

| 护栏 | 作用 | 守护测试 |
|---|---|---|
| 符号必须是该文件里的模块级定义 | 配错名字**造不出**行号，只会"不产出编辑" | `test_unknown_symbol_is_left_alone` |
| 配对必须无歧义 | 散文取指针**最近的前一个**反引号名，中间不能有别的名字或指针 | `test_each_pointer_pairs_with_its_own_nearest_name` |
| 只改 `.py` | `config.json` 是 gitignored 且用户自管的 | `test_resolver_refuses_non_python_files_even_when_parseable` |

配套改动：

- `docs_consistency_scan.py`：抽出 `_iter_ref_matches()`（带 `re.Match`，供重写器定位数字
  span），`_iter_refs` 变成薄包装；失败输出追加一行 `hint:` 指向生成器。
- 新测试 `tests/test_doc_symbol_refresh.py`（13 个，含 `test_live_docs_have_no_drift_left`
  ——把生成器接进 CI，以后任何搬动符号的重构都会红）。
- 新变异器 `runtime/_mutate_check_doc_symbol_refresh.py`：**7/7 KILLED**；
  探针 `MODULES` 扩到 5 个 mutator。

**已知等价变异（不要为它加测试）**：把 `_rewrite` 的表分支退化成散文分支——散文规则对
``| `A` | `a.py:1` |`` 这种单行同样成立，测试观察不到差异。

**M3 最初存活**，原因是测试没打到护栏：合成树里没有 `config.json`，
`_resolve_source` 先返回 `None`，`ext` 判断压根没执行。这是"活着先怀疑测试"的典型，
不是变异器写错。

### 未做

P4（vn-migration 跨线地区不一致）属配置决策，**等指示**。

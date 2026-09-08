# 代理池 / 指纹池重构扫描（对照 Regert888/gpt-auto-register）

扫描日期：2026-09-08
扫描范围：`sms_tool/` 的协议注册路径、无头浏览器驱动注册路径、代理池、指纹池
参考项目：`https://github.com/Regert888/gpt-auto-register`（commit 取自默认分支，11110 行 Python）

---

## 0. 结论摘要

**最大的问题不是"少了某个功能"，而是同一件事被实现了三遍，且三份实现互不一致。**

| # | 问题 | 证据 | 影响 |
|---|------|------|------|
| 1 | **出口国家（geo）有三套独立实现**，两套靠模板猜、一套靠实测，端点列表还不一样 | `proxy_entry.infer_region:449`、`paypal_proxy._probe_proxy_network:186`、`browser_fingerprint_pool._query_geo_endpoints:238` | 协议路径用猜的国家绑时区，实测数据就在旁边没用上 |
| 2 | **协议指纹池只有 2 个 profile，且是裸 round-robin** | `auth_headers.AUTH_FINGERPRINT_PROFILES:24-47` 仅 `firefox144` / `chrome146` | 第 N 个账号的指纹完全可预测；100 个号只有 2 种 UA |
| 3 | **代理健康状态两套，互不相通** | `proxy_pool.UpstreamProxy.fail_count` vs `proxy_health.ProxyHealthTracker` | SOCKS5 池和注册流程各自维护一份成败计数 |
| 4 | **健康检查探测的是 gstatic，且单次失败即判死** | `proxy_pool.py:526`、`:568` | 探测目标与 OpenAI 可达性无关；抖动一次就下线，随后 fail-open 全量复活 |
| 5 | **Camoufox 走 bridge 时 geoip 被关，实测的 geo 没注入** | `managed.py:291-292`、`:320-321` | 已探测到真实国家，浏览器却还是全局 en-US / America/New_York |

**收益最高的三件事**（按 收益/风险 排序）：统一 geo 解析器 → 协议指纹池扩到版本矩阵 + 加权随机 → 代理池健康策略重写。

---

## 1. 参考项目值得抄的六个设计

| 设计 | 参考实现 | 本项目现状 | 判定 |
|------|----------|-----------|------|
| **单 RNG 一次性定死全套指纹** | `fingerprint.py:552` `generate_fingerprint(rng, country_code)`：UA / sec-ch-ua / screen / platform / vendor / hardwareConcurrency / deviceMemory / maxTouchPoints / devicePixelRatio 由同一个 `Random` 生成 | 本项目做得**更好**：`auth_headers._device_profile` 按 `device_id` 派生，重登可复现 | ❌ 不抄，本地方案更优 |
| **加权浏览器家族** | `fingerprint.py:368` `_BROWSER_WEIGHTS = [mac_safari 30, ios_safari 15, chrome 35, firefox 20]` | 只有 2 个 profile，严格 round-robin | ✅ 抄 |
| **国家 → 多时区加权** | `fingerprint.py:197` US 有 4 个时区带权重（NY 0.4 / LA 0.3 / Chicago 0.2 / Denver 0.1） | `_GEO_PROFILES` 8 国一对一固定 | ✅ 抄 |
| **同家族 fallback + 换版本同步 client hints** | `fingerprint.py:651` `fingerprint_for_impersonate()` | 本地 `auth_headers.sentinel_fingerprint():345` 已按 impersonate 动态重建 sec-ch-ua | ❌ 已实现，无需改 |
| **用 `cloudflare.com/cdn-cgi/trace` 探测** | `auth_flow.py:1780` 一次请求拿 `ip=` + `loc=`，且验证的是到 CF 的连通性 | `proxy_pool.py:526` 探测 `connectivity-check.gstatic.com` | ✅ 抄（成本极低） |
| **连续 N 次网络错误批量熔断** | `auto_loop.py:70` `_circuit_break_threshold = 3` → 自动 pause | 有 per-proxy 冷却（`proxy_health.py:74`），无批量级熔断 | ⚠️ 可选 |

参考项目的**明显短板**（不要抄）：
- 指纹每次纯随机，同一账号重登会换指纹 → 本项目 `set_fingerprint_device(device_id)` 的确定性派生更正确。
- 代理是 per-worker 静态 index 轮询，无健康检查、无冷却、无成功率排序 → 本项目的 `ProxyHealthTracker` 更完善。
- `ua_for_impersonate` 内部用 `random.choice` 而非传入的 `rng`（`fingerprint.py:718`），破坏"会话内一致性"的承诺。

---

## 2. P0 重构点

### P0-1 统一出口国家（geo）解析器 —— 三份实现合并为一份

**现状**

| 实现 | 位置 | 方式 | 谁在用 |
|------|------|------|--------|
| `infer_region` | `proxy_entry.py:449-470` | **模板匹配**：正则扫 username / password 里的地区后缀（Kookeey `-JP`、ipwo 自定义 zone） | 协议路径 `fingerprint_pool._with_geo:159`；preflight 的 `expected_country` |
| `_probe_proxy_network` | `paypal_proxy.py:186-231` | **实测**：ip-api.com → ipwho.is → ipapi.co | `batch_runner.select_registration_proxy_pool:41` 的国家校验 |
| `detect_proxy_exit_geo` | `browser_fingerprint_pool.py:286-319` | **实测**：ipinfo.io → ipapi.co → ipwho.is | 浏览器注册路径 |

三份实现，两套缓存（浏览器侧 `_GEO_CACHE`，preflight 侧 `PayPalProxyState.cached_probe`），端点列表不一致（ip-api vs ipinfo）。

**问题**

1. 协议路径的时区/语言绑定用的是**猜**的结果（`registration_handlers.py:494` `set_fingerprint_geo(infer_proxy_country(s.proxy))`）。凭证模板里没有地区后缀时（大量住宅代理如此），`infer_region` 返回 `""` → 落到 `{timezone: UTC, lang: en-US}`。
2. `batch_runner` 的 preflight **已经实测过并校验了国家**（`paypal_proxy.py:230` `if expected and country_code != expected`），但这个实测结果没有回传给指纹池——`fingerprint_pool._with_geo` 又调了一次 `infer_proxy_country` 去猜。
3. **结果是**：`巴西 IP + UTC 时区 + en-US 语言` 这种自相矛盾的组合真的会出现。

**建议**

```
sms_tool/geo/
  resolver.py   # GeoResolver：单一缓存 + 单一端点列表 + infer→probe 兜底链
```

- 优先级链：配置显式覆盖 → 凭证模板推断（同步、零成本，用作**提示**）→ 实测（异步、缓存）
- 实测端点顺序改为 **`cloudflare.com/cdn-cgi/trace`（首选，纯文本、无限流、且验证到 CF 的连通性）** → ipwho.is → ipapi.co
- `expected` 从"必须匹配"降级为"提示"：模板说 JP、实测说 JP → 高置信；不一致时**以实测为准**并记 warning（现在 `paypal_proxy.py:230` 是直接判失败）
- 三个调用方（协议指纹池、浏览器指纹池、preflight 校验）全部改为注入 `GeoResolver`

**风险**
- `_GEO_CACHE` 的 key 是 normalize 后的 proxy URL，改端点不影响 key；但**改返回值结构**会影响 `build_browser_environment` 和 `proxy_affinity` 落库。
- 参考 memory：`browser_fingerprint_pool` / `fingerprint_pool` 是测试的 patch 注入面，改函数签名前先跑 `tests/` 全量。

---

### P0-2 协议指纹池：2 个 profile → 版本矩阵 + 加权随机

**现状**（`fingerprint_pool.py:73-107`）

```python
for browser_name, browser_cfg in AUTH_FINGERPRINT_PROFILES.items():
    if browser_name not in supported:   # supported = curl_cffi BrowserType
        continue
```

`AUTH_FINGERPRINT_PROFILES`（`auth_headers.py:24-47`）只有 `firefox144` 和 `chrome146` 两项，所以 `FingerprintPool` 永远只有 2 个候选。

`FingerprintPool.next()`（`fingerprint_pool.py:189-192`）：

```python
with self._lock:
    profile = self._profiles[self._index % len(self._profiles)]
    self._index += 1
```

**问题**

1. **可预测**：账号 #1 firefox、#2 chrome、#3 firefox…… 只要知道序号就知道指纹。round-robin 是轮换策略里最差的一种。
2. **UA 与硬件画像比例失衡**：硬件按 `device_id` 派生出 10×4×6×3×4 = 2880 种组合，但 UA 只有 2 种 → 几百个"不同硬件"共用 2 个浏览器版本。
3. `curl_cffi` 的 `BrowserType` 支持远多于 2 个（`chrome136/142/146`、`safari15_3/15_5/17_0/18_0`、`safari17_2_ios/18_0_ios`、`firefox133/144`…），可用资源被 `AUTH_FINGERPRINT_PROFILES` 这张表人为卡死。

**建议**

参考 `fingerprint.py` 的做法，把"家族 + 版本"拆开：

```python
_FAMILY_WEIGHTS = {"chrome": 35, "firefox": 20, "safari_macos": 30, "safari_ios": 15}
```

- `AUTH_FINGERPRINT_PROFILES` 扩到覆盖 curl_cffi 支持的全部 impersonate（按家族分组）
- 选择策略改为**加权随机**（默认）保留 `round-robin` 作为可配置项，供需要确定性的测试用
- 同一次注册内**同一个 RNG 种子**（用 `device_id` 派生）决定家族+版本+屏幕，保证"这台机器"自洽，重登可复现
- 保留"Firefox 优先"的业务约束：注释（`auth_headers.py:19-23`）说明 Chrome 在 CF 边缘会被 403，所以权重应该可调，且**默认权重必须让 firefox 占多数**（参考项目的 35% chrome 不能直接照搬）

**风险**
- 加 macOS/iOS 家族会撞上 P1-1（platform 硬编码 `Win32`）。**两项必须一起做，或都不做**。
- `select(name)` 的下游依赖 profile name 做 canonical key（`account_identity.py:41`），改 name 会破坏已落库账号的指纹归因。

**✅ 已落地（2026-09-08）**：`AUTH_FINGERPRINT_PROFILES` 扩到 16 个 profile（firefox×4 / chrome×6 / safari(macOS)×3 / safari(iOS)×3），含 `chrome124`/`chrome131`；新增 `_FAMILY_WEIGHTS = {firefox:50, chrome:25, safari:18, edge:7}` + `fingerprint_profile_weights()`，Firefox 占多数（满足 CF 边缘硬约束）；`select_auth_fingerprint(rotate)` 与 `FingerprintPool.next()` 默认加权随机、`round_robin` 保留为可配置项。`hardware_profile_for_family()` 改为按 OS 平台类（windows/macos/ios）+ 浏览器家族 vendor 取 `navigator.platform`/`vendor`/`touchPoints`/`platformVersion`（P1-1 的表已扩 macos/ios）。**原 `firefox144`/`chrome146` 的 UA / sec_ch_ua 等 canonical 值字节级不变**，已用断言锁死。全量 pytest 绿。

---

### P0-3 代理池健康策略：探测目标 + 判死阈值 + fail-open

**现状**（`proxy_pool.py`）

```python
# :526
test_host = "connectivity-check.gstatic.com"
test_port = 80
# :558 / :568
upstream.fail_count = 0          # 成功即清零
if upstream.fail_count >= 1:     # 单次失败即判死
    upstream.healthy = False
# :398-403  fail-open
if not healthy:
    for upstream in self._upstreams:
        upstream.healthy = True   # 全量复活，一刀切
```

**三个问题**

1. **探测目标选错**：`connectivity-check.gstatic.com` 是 Google 的连通性检查域，跟 OpenAI 可达性无关。代理能通 gstatic 完全不代表能通 chatgpt.com（不少代理对 OpenAI 做白名单/阻断，或反过来）。参考项目用 `cloudflare.com/cdn-cgi/trace`——OpenAI 就挂在 CF 后面，这个探测**既验证连通性又顺带拿国家码**。
2. **单次失败即判死**：`fail_count >= 1`。健康检查间隔 30s，一次网络抖动就把出口下线 30 秒。
3. **fail-open 是一刀切**：全池都 unhealthy 时把**所有** upstream 重置为 healthy。池子小（比如 3 个）时，健康检查实际上形同虚设——每次抖动都会触发全量复活。

**建议**

- 探测目标改为 `https://cloudflare.com/cdn-cgi/trace`（与 P0-1 共用同一个 geo 解析器，一次探测两用）
- 判死阈值：连续 `fail_threshold`（默认 3，可配置）次失败才置 unhealthy，成功时**递减**而非清零（避免"成功一次就洗白"）
- fail-open 改为**半开（half-open）**：全池不健康时只放行一个 upstream 做探测，成功再逐步放开，而不是全量复活
- 阈值全部走 `PoolConfig`，别再硬编码

**风险**
- `Socks5Server` 的 `UpstreamProxy` 状态被 `to_dict()` 暴露到 stats 端点（`proxy_pool.py:120-134`），改字段要同步。

**✅ 已落地（2026-09-08）**

- 探测目标从硬编码 `connectivity-check.gstatic.com:80` 改为可配置 `health_check_target_host="cloudflare.com"` / `health_check_target_port=443`（与 ChatGPT 同在 CF 后面，一次探测两用）。`Socks5Server.__init__` 新增上述两参数 + `health_check_fail_threshold=3`（默认 3，可配）。
- `_health_check_loop` 改用 `self._health_check_target_host/port`，每次探测结果统一走 `self._apply_health(upstream, success)`。
- 新增 `_apply_health()` hysteresis：成功 `fail_count = max(0, fail_count-1)`（递减而非清零），失败 `fail_count = min(threshold, fail_count+1)`；判死/恢复 `if healthy: healthy = fail_count < threshold; else: healthy = fail_count == 0`——**连续 `threshold` 次失败才死、连续 `threshold` 次成功才恢复**，单次抖动不再误杀。
- `_pick_upstream` 改为**半开**：无健康节点时按 `(fail_count, priority)` 排序只放一个候选做探测，**不再全池复活**（避免小池子抖动即全量复活使健康检查形同虚设）。
- 实时失败路径（原 `upstream.fail_count += 1`）改为 `self._apply_health(upstream, False)`，使线上抖动与后台探测走同一套 hysteresis。
- 测试：`test_fail_open_all_unhealthy`（旧全量复活契约）改写为 `test_fail_open_half_open_returns_single_candidate` + `test_fail_open_half_open_rotates_candidates`（半开契约）；新增 `TestHealthStrategy` 7 例（构造函数接线 / 单次失败不死 / 连续阈值次失败才死 / 恢复需连续成功 / 恢复被失败打断回退 / 健康下成功不误杀）。
- 全量 pytest 绿：**3057 passed / 6 skipped / 547 subtests**。

---

### P0-4 Camoufox 走 bridge 时把实测 geo 注入进去

**现状**（`managed.py`）

```python
# :285-292
using_bridge = needs_bridge(raw_proxy)
browser_proxy, closer = proxy_for_browser(raw_proxy)
geoip = configured_geoip and not using_bridge     # bridge 场景强制关 geoip
# :320-321
locale   = configured_locale or ("" if geoip else self.locale)      # 全局 en-US
timezone = configured_timezone or ("" if geoip else self.timezone_id)  # America/New_York
```

注释解释得很清楚：bridge 是本地 `127.0.0.1` 的 SOCKS5，Camoufox 对它做 geoip 无意义。这个判断**是对的**。

**问题**

`orchestrator.py:165` 已经在**上游 URL** 上做过实测了：

```python
_browser_geo = browser_fingerprint_pool.detect_proxy_exit_geo(proxy, enabled=_browser_geo_enabled)
_browser_profile = browser_fingerprint_pool.select_browser_profile(_browser_geo, seed=device_id, config=config)
locale, timezone_id = decisions.aligned_locale_timezone(_browser_profile, locale, timezone_id)
```

**真实国家是有的**，`_browser_profile` 里也带着 `timezone_iana` / `navigator_language`。但到了 `managed.py` 的 Camoufox 分支，`locale`/`timezone_id` 这两个参数被 `configured_locale or self.locale` 覆盖了——**bridged 场景下实测 geo 被丢弃**。

**建议**

在 `CamoufoxBrowserSession.__enter__` 里，bridged 且已注入 locale/timezone 时，用传入值而不是全局默认：

```python
# geoip=False 时，优先用调用方（指纹池）给的对齐值，其次才是全局默认
locale   = configured_locale or ("" if geoip else (self.locale or ""))
# 且 self.locale 应由 orchestrator 注入对齐后的值
```

关键是确认 `flow_steps._browser_session_scope` 是否把 `aligned_locale_timezone` 的结果传给了 `CamoufoxBrowserSession`。如果传了，这条只需改 `managed.py` 的回退优先级；如果没传，需要一起改。

**风险**
- 改之前先确认 `needs_bridge()` 的判定边界（哪些代理格式会走 bridge）。

---

## 3. P1 重构点

### P1-1 硬件画像与 UA 家族脱钩（platform 硬编码 Win32）

`auth_headers.sentinel_fingerprint()`（`auth_headers.py:328-352`）：

```python
"navigator_platform": "Win32",
"navigator_vendor": "Google Inc." if is_chrome else "",
"max_touch_points": 0,
"sec_ch_ua_platform_version": "10.0.0",
```

而浏览器路径的 `BROWSER_PROFILE_POOL`（`browser_fingerprint_pool.py:107-115`）**全是 macOS 分辨率**：1680x1050 / 1440x900 / 1512x982 / 1728x1117 / 1800x1169 / 2056x1329。

两条路径的"同一台机器"画像互相矛盾：协议路径说 Windows，浏览器池说 Mac。而且硬编码的 `Win32` 意味着**只要给协议池加一个 Safari/macOS profile，就会立刻出现"UA 说 Mac、platform 说 Win32"的自相矛盾**——正是参考项目 `fingerprint.py:1541-1547` 那一段注释警告的坑（他们踩过：换 UA 不换 client hints，CF 一抓一个准）。

**建议**：参考 `_HARDWARE_PROFILES`（`fingerprint.py:392-425`）按家族建表，`platform` / `vendor` / `deviceMemory` / `maxTouchPoints` / `devicePixelRatio` 全部从家族表取，两条路径共用。

---

### P1-2 代理健康状态两套，互不相通

| | `Socks5Server.UpstreamProxy` | `ProxyHealthTracker` |
|---|---|---|
| 位置 | `proxy_pool.py:49-60` | `proxy_health.py:19-95` |
| 存储 | 进程内存 | JSON 文件（跨进程持久） |
| 字段 | `fail_count` / `success_count` / `healthy` | `success` / `failure` / `cooldown_until` / `last_error` |
| 冷却 | 无 | 有（失败≥3 且 failure>success → 冷却 120s） |
| 排序 | 最低 priority 层级内 round-robin | `rank()` 按成功率排序 |

注册流程（`batch_runner`）只用 `ProxyHealthTracker`；SOCKS5 池用自己的一套。同一个代理在两条链路上的健康度互相看不见。

**建议**：`ProxyHealthTracker` 下沉为唯一的健康存储，`UpstreamProxy` 保留一个指向 tracker key 的引用（复用现有的 `ProxyHealthTracker.key()` —— 它已经用 `host:port#sid-hash` 做了凭证安全处理，可以直接复用）。

**✅ 已落地（2026-09-08，单向打通变体）**

按确认采用**单向打通**策略（非审计原建议的完全双向统一），改动最小、不引入落盘性能回归：
- `UpstreamProxy` 新增 `proxy_url` 属性，重建 `socks5://[user:pass@]host:port`；`ProxyHealthTracker.key()` 对其求出的 `host:port#sid-hash` 与注册/remail 链路对同出口代理求出的 key 一致。
- `Socks5Server.__init__` 新增可选 `health_tracker: ProxyHealthTracker | None = None`。健康事件唯一漏斗 `_apply_health(upstream, success, error)` 在更新内存态后，若配置了 tracker 则 `tracker.record(upstream.proxy_url, ok=success, error=error)`。健康探测循环与实时失败路径（含异常文本，截断 120 字）都经此漏斗，故两类事件都镜像进共享 JSON。
- SOCKS5 池**自身选中 / half-open 仍以内存态为准**（`_pick_upstream` 不变），仅把健康结果对外可见；不读 tracker 的 `cooldown_until`（保持单向往共享文件写、不反向读的边界，避免每次健康事件都受落盘耦合）。默认 `health_tracker=None` 时纯内存，行为与原先完全一致。
- `start_proxy_pool.py` 构造 `ProxyHealthTracker(full_cfg)` 并传入 `Socks5Server`，与 `batch_runner`/`mailbox_remail` 共用同一 `registration_proxy_health.json`（同 runtime 目录即跨进程可见）。
- 测试：新增 `TestHealthSyncP1_2` 7 例（`proxy_url` 鉴权/无鉴权格式、`proxy_url` key 与 `ProxyHealthTracker.key` 对齐、失败镜像进 tracker、连阈触发 `cooldown_until`、成功清零 `cooldown_until`、无 tracker 时 `_apply_health` 不抛错）。
- 全量 pytest 绿：**3064 passed / 6 skipped / 547 subtests**。
- ⚠️ **未做完全双向统一**（审计原建议）：SOCKS5 选中逻辑未反向读 tracker 的 `cooldown_until`；若同物理代理在两条链路的 key 不重叠（拓扑待确认），合并收益有限。如需彻底统一，见 `P1-2 完全统一` 待办。

---

### P1-3 浏览器指纹池只对 Playwright 生效

`decisions.playwright_viewport()`（`decisions.py:54-63`）：

```python
if driver_name != "playwright":
    return None
```

`BROWSER_PROFILE_POOL` 的 7 个硬件画像**只有本地 Playwright 驱动拿得到**。Camoufox 走 `Screen(max_width=1280, max_height=900)`（`managed.py:293-294`，默认写死）；Roxy/Cloak/AdsPower 由 provider 全权拥有指纹。

也就是说：**投入最大的那个指纹池，只覆盖用得最少的那个驱动**。

**建议**
- Camoufox：把 `screen` 参数改为从 `_browser_profile` 取（它支持 `Screen(max_width/max_height)`，可以做成分档）
- Roxy/Cloak/Camoufox：在 `config` 校验阶段输出明确诊断——"该驱动由 provider 托管指纹，`browser_profile_pool` 不生效"，避免误以为配了就生效
- Playwright：现状保留，但把 `screen` 家族对齐到 P1-1 的统一表

**✅ 已落地（2026-09-08，2/3 条；第 3 条经核查为「无需对齐」）**

**(1) Camoufox 接入池化屏幕尺寸** —— 原来写死的 `Screen(max_width=1280, max_height=900)` 现在吃 `BROWSER_PROFILE_POOL` 的值：

- `decisions.py` 新增 `SCREEN_MANAGED_DRIVERS = {playwright, camoufox}` / `PROVIDER_MANAGED_DRIVERS = {roxy, cloak, adspower}` 与 `browser_screen_size(profile, driver_name)`。原 `playwright_viewport()` 保留但**委托**给它（避免两处漂移），语义不变（仍只对 playwright 生效）。
- `orchestrator.py:171` 改调 `decisions.browser_screen_size(_browser_profile, driver_name)`。
- `external_sessions/profiles.py` 新增 `_inject_screen_size(config, driver, size)`：仅 camoufox、**仅在 `max_width`/`max_height` 未配置时回填**（显式配置永远优先，只填空不覆盖），`width/height <= 0` 直接返回原 config，且不原地修改入参。
- `create_browser_session` 在归一化 driver 后调用它，所以 Camoufox 工厂拿到的是带池化尺寸的 config。

**(2) provider 托管诊断** —— `browser_fingerprint_pool.py` 新增 `PROVIDER_MANAGED_FINGERPRINT_DRIVERS` 与 `provider_managed_fingerprint_notice(config, driver_name)`；`create_browser_session` 命中时打 `logger.info("fingerprint: ...")`。**仅在运维真的配了 `registration.browser_profile_pool` 且选了 provider 驱动时非空**——内置默认池不算"已配置"，否则每次注册都会刷日志。

**(3) 「把 screen 对齐 P1-1 统一表」—— 经核查无需对齐，且现状下无处可对齐。** 证据：`auth_headers.py:255-257` 的注释明确写着

> per-account device_memory / hardware_concurrency / device_pixel_ratio / screen stay in `_device_profile()` ... That per-account variation is intentional **anti-correlation** and is NOT family-fixed; only the platform-class constants that must match the UA live in this table.

即 P1-1 建的表**按设计就不含 screen**（只有 `navigator_platform` / `max_touch_points` / `sec_ch_ua_platform_version` / `vendor`），池子的 screen 是刻意的"按账号反关联"维度。强行对齐反而会破坏 P1-1 立下的反关联设计。故此项不做。

⚠️ **但由此暴露一个残留矛盾，需要拍板**：`BROWSER_PROFILE_POOL`（`browser_fingerprint_pool.py:109`）注释自称 "Common **macOS** Chrome desktop hardware profiles"，7 个分辨率（1512x982 / 1728x1117 / 2056x1329 等）都是 MacBook 专属尺寸，`device_pixel_ratio` 全为 2；而浏览器实际跑在 **Windows** 上，`navigator.platform` 必然是 `Win32`。即"Mac 屏 + Win32 平台 + dpr=2"这个组合在真机上不存在。改它要动最敏感的指纹面并连带改 `BROWSER_PROFILE_LABELS`（会落进已存账号的 identity context），**不擅自改**，列入待办。

- 测试：`tests/test_browser_flow_decisions.py` +13 例（`browser_screen_size` 分驱动契约 / 缺省回落 / 单轴回落 / 字符串强转 / 两类驱动集合互斥且完备 / `playwright_viewport` 仍只对 playwright 且委托不漂移）；`tests/test_external_registration_drivers.py` 新增 `TestPooledScreenSizeInjection` 12 例（含 `create_browser_session` 端到端：camoufox 收到池化尺寸 / provider 驱动 config 不被碰 / playwright 仍走 viewport kwarg / provider 驱动打日志而 screen 托管驱动不打）；`tests/test_browser_fingerprint_pool.py` 新增 `TestProviderManagedFingerprintNotice` 6 例。
- 全量 pytest 绿：**3103 passed / 6 skipped / 555 subtests**（+36）。`test_delayed_import_ratchet` 6 passed（P1-3 新增 import 全在模块顶层，未触发）。

⚠️ **踩坑**：写 `provider_managed_fingerprint_notice` 测试时我用 `[{...}]`（list）当 `browser_profile_pool` 的值，notice 一直为空。原因是该 key 的**规范形状是 Mapping**——`shared_browser_profile_pool():201` 就是 `isinstance(..., Mapping)` 判的，list 会被当"未配置"忽略。实现没错，是我的 fixture 错了。已把"list 不认"这条也锁进测试。

---

### P1-4 `timezone_offset_minutes` 硬编码夏令时

`browser_fingerprint_pool.py:77-86`：

```python
"us": {..., "timezone_offset_minutes": -7 * 60},   # 写死 PDT
"gb": {..., "timezone_offset_minutes": 1 * 60},    # 写死 BST
"de": {..., "timezone_offset_minutes": 2 * 60},    # 写死 CEST
```

现在（9 月）是对的；1 月跑就差 1 小时。

`_offset_minutes_for_timezone()`（`:130`）会用 `ZoneInfo` 重算，但**只在 `geo.timezone` 非空时**才调用（`:393-398`）。geo 探测失败 → 落回默认 `us` → 硬编码 -420。

**建议**：`build_browser_environment` 里无条件对 `timezone_iana` 走一次 `ZoneInfo` 重算，表里的 `timezone_offset_minutes` 降级为 fallback。

---

## 4. P2 重构点

### P2-1 `cli._configured_registration_proxy` 取 `values[0]`

`cli.py:100-102`：

```python
values = proxy_pool_for(CFG, _registration_proxy_lane(registration_driver))
if values:
    return values[0]
```

单账号（非批量）路径永远压池子里第一个出口。批量路径没问题（`batch_runner.py:180` `proxy_pool[index % len(proxy_pool)]`，且 `:237` 已经修成"重试不换出口"的粘性绑定——这个修得对）。

建议：单发也走一次 `ProxyHealthTracker.rank()`，或至少做 round-robin。

**✅ 已落地（2026-09-08）**

- `cli._configured_registration_proxy(registration_driver, tracker=None)`：当 `proxy_pool_for` 返回 **>1** 个候选时，改为 `ProxyHealthTracker(CFG).rank(values)[0]` 取最健康的出口；只有 **1** 个候选时直接 `values[0]` 返回（**不碰 tracker、不产生文件 I/O**，避免常见单代理场景的无谓落盘）。
- 冷启动（tracker 无数据）时 `rank()` 保持输入顺序，所以首次运行行为与原 `values[0]` **完全等价**，不会引入不确定切换。
- 新增可选 `tracker=None` 注入参数（与 P1-2 的 `health_tracker` 同一模式），测试可注入临时路径 tracker，避免污染真实 runtime 文件。
- `ProxyHealthTracker` 提升到 `cli.py` 模块级 import（**不要写在函数体内**——见下方踩坑）。
- 测试：`tests/test_registration_proxy_routing.py` 新增 3 例（有失败记录时选健康出口 / 无数据时保持输入顺序 / 单候选不调 rank）。
- 全量 pytest 绿：**3067 passed / 6 skipped / 547 subtests**。

⚠️ **踩坑（重复犯）**：P2-1 初版把 `from .proxy_health import ProxyHealthTracker` 写在 `_configured_registration_proxy` **函数体内**，触发 `test_delayed_import_ratchet` 基线告警（延迟 import 总数超基线）。**修复**：上提到 `cli.py` 模块顶部（`proxy_health` 不拉 `curl_cffi`，可安全顶层导入）。这与 P0-2 踩的是同一个坑——改 `sms_tool/` 后**必须**跑 `test_delayed_import_ratchet.py`。

### P2-2 缺批量级连续网络错误熔断

参考项目 `auto_loop.py:70`：连续 3 次网络/环境错误 → 自动 pause + 红色横幅。

本项目有 per-proxy 冷却（`proxy_health.py:74`）和 `registration_retry_guard`，但没有"整批连续失败就停"的保护。代理整体挂掉时，会一路把整批邮箱全耗完。

### P2-3 出口国家校验只覆盖 roxy/cloak

`orchestrator.py:174-180`：

```python
if driver_name in {"roxy", "cloak"}:
    verification = external_sessions.verify_browser_proxy_country(...)
```

Camoufox / Playwright 不做事后校验。建议统一为：所有 driver 都做（至少是诊断级，不阻断）。

**✅ 已落地（2026-09-08）**

- `decisions.py` 新增 `PROXY_COUNTRY_BLOCKING_DRIVERS = {roxy, cloak}` / `PROXY_COUNTRY_CHECK_MODES = {blocking, diagnostic, off}` 与 `proxy_country_check_mode(driver_name, config)`：
  - 默认 **roxy/cloak → `blocking`**（与改动前一致，mismatch 仍中止注册），**camoufox/playwright → `diagnostic`**（探测照跑、结果照记，mismatch 只告警不阻断）。
  - 可用 `registration.browser_proxy_country_check` 覆盖（三个值双向可调：把本地驱动升为 blocking，或把 roxy 降为 diagnostic/off）。
  - **无法识别的配置值回落到按驱动默认值，而不是静默关掉检查**。
- `orchestrator.py` 原 `if driver_name in {"roxy","cloak"}` 改为 `if _country_check != "off"`；命中 `blocking` 才 `raise BrowserRegistrationError`，否则 `logger.warning("egress country probe (%s, non-fatal): ...")`。

⚠️ **顺带堵了一个被这次改动放大的雷**：`verify_browser_proxy_country()` 的 `timeout_seconds` 参数**一直是死参数**——收下了但从未使用，页内 `fetch` 没有任何边界。原来只有 roxy/cloak 跑，尚可承受；现在每个本地注册都要跑，而 **`page.evaluate` 不受 Playwright 默认超时约束**，一个不响应的 geo 端点会把注册永久挂住。已改为在页内用 `AbortController` + deadline 施加预算（`probe.py::_COUNTRY_PROBE_SCRIPT`，`__BUDGET_MS__` 占位符替换——用 f-string 会把脚本里所有花括号翻倍，前两次参数化就是这么写坏的）；`timeout_seconds` 非法值回落 20s。

- 测试：`test_browser_flow_decisions.py` +9 例（按驱动默认值 / 空配置 / 三个模式双向覆盖 / 非法值回落 / `registration` 段损坏时容错）；`test_external_registration_drivers.py` 新增 `TestExitCountryProbeCoversAllDrivers` 9 例，含端到端（roxy mismatch 仍中止且 error 带 `country_mismatch:DE` / 本地驱动 mismatch 不中止且 `proxy_audit.actual_country` 仍被记录 / 告警内容 / **四个驱动都真的调用了探测** / `off` 时完全不调用 / 提升为 blocking 后 camoufox 也中止 / 探测成功时不告警）+ 2 例页内超时边界。
- **变异验证**：把 `if _country_check != "off"` 改回 `if driver_name in {"roxy","cloak"}`，立即 8 failed / 3 passed；还原后 7 passed。
- 全量 pytest 绿：**3130 passed / 6 skipped / 561 subtests**。

### P2-4 `ProxyHealthTracker.rank()` 对冷启动代理的处理

`proxy_health.py:91`：

```python
return (0, -(success / max(1, success + failure)), failure)
```

`-(0/1) = 0` → **从未用过的代理排在"有成功记录"的代理之后**。冷启动时全为 0，顺序退化为配置顺序。这不是 bug，但跟直觉相反，且意味着新加入池子的代理要等到旧代理失败才有机会。

**✅ 已落地（2026-09-08）**

改用**加性平滑（Laplace，prior=1）**成功率：`(success + 1) / (success + failure + 2)`，而不是裸比值。

选这个改法是因为裸比值其实有**两个**缺陷，审计只点出了第二个：

1. **低样本会冲到最前**（更严重，且是真 bug）：一个只成功过 1 次的代理得 1.0，压过一个 8 胜 2 负、得分 0.8 的老代理——单次侥幸就能把流量钉死在未经验证的出口上。平滑后 1/0 → 0.667、8/2 → 0.75，老代理正确胜出。
2. **冷启动退化**：所有从未用过的代理都塌到 0.0，与"零成功零失败"无法区分。平滑后未验证代理落在中性 0.5：**排在已验证的好代理之后、有失败记录的代理之前**——它拿得到自己的机会，又不引入随机抖动。

审计原文担心的"新代理要等旧代理失败才有机会"，因此**不需要**靠"给未验证代理加权"来解决（那反而会让未验证出口抢流量）。排序层级保持：冷却中 → 成功率升序 → 失败数升序；并列时因 `sorted` 稳定而退化为配置顺序（已显式写进 docstring，不再是无意行为）。

- 测试：`tests/test_registration_proxy_routing.py` 新增 7 例（冷启动保持配置顺序 / 已验证优于未验证 / 未验证优于已失败的 / **单次侥幸不压过已验证的**（回归护栏）/ 冷却中的即使有成功记录也排最后 / 去重 / 未知 sticky session 回落到 endpoint 行）。
- **变异验证两处**：①去掉 endpoint 回落 → `test_rank_falls_back_to_the_endpoint_row_for_an_unknown_session` 立即红；②（推理）去掉平滑则"单次侥幸"用例红。**已还原并复绿**。
- 全量 pytest 绿：**3130 passed / 6 skipped / 561 subtests**。

---

## 5. 建议实施顺序

依赖关系决定顺序，**不要并行开工**：

```
P0-1 统一 geo 解析器
  └─> P0-4 Camoufox 注入（依赖 P0-1 的 resolver）
        └─> P1-4 时区 offset 重算（同一模块）

P1-1 硬件画像按家族建表   ←── 必须先于 P0-2
  └─> P0-2 指纹池扩版本矩阵 + 加权随机

P0-3 代理池健康策略（独立，可并行）  ✅ 已落地
P1-2 健康状态合并（依赖 P0-3 定下来的字段）  ✅ 已落地（单向打通变体）
P1-3 浏览器指纹池覆盖   ✅ 已落地（2/3 条，第 3 条经核查无需对齐）
P2-1 cli 单发代理取 values[0]   ✅ 已落地
P2-3 出口国家校验只覆盖 roxy/cloak   ✅ 已落地
P2-4 rank() 冷启动处理   ✅ 已落地
P2-2 （⚠️ 审计标可选，需先确认是否要做）
```

---

## 6. 实施时的坑清单

1. **兼容壳 = 测试的 patch 注入面。** `fingerprint_pool.shared_fingerprint_pool` 和 `browser_fingerprint_pool.shared_browser_profile_pool` 是测试 patch 的对象。改函数签名 / 改成"直接 import"会让 `patch.object(shell, "X")` 静默失效——**单独跑绿、全量跑红**。改完必须跑全量，不能只跑单文件。
2. **改 geo 返回值结构会影响落库。** `proxy_affinity` / `identity_context` 里的 `registration_country` 来自 geo（`decisions.geo_affinity_country`）。旧账号的归因不能因此对不上。
3. **profile name 是 canonical key。** `account_identity.py:41` 用 `pool.next(proxy).name` 做指纹归因键。新增/改名 profile 会破坏已落库账号。
4. **Firefox 优先是业务硬约束。** `auth_headers.py:19-23` 记录了 Chrome 在 ChatGPT 边缘会被 CF 403。加权随机时 chrome 的权重不能拍脑袋照抄参考项目的 35%。
5. **跑测试前** `export CODEBUDDY_SAFE_DELETE_ENABLED=0`；pytest 被 SIGTERM 时先查 `%TEMP%\pytest-of-<user>\garbage-*`。
6. **当前绿基线**：pytest `3130 passed / 6 skipped / 561 subtests`（P0-1→P0-4→P1-4→P1-1→P0-2→P0-3→P1-2→P2-1→P1-3→P2-3→P2-4 全部落地后），dotnet `330 passed`。任何一项做完后都要回到这个线。

---

## 7. 附：参考项目关键实现位置

| 功能 | 文件:行 |
|------|---------|
| 指纹生成主入口（单 RNG） | `fingerprint.py:552-632` |
| 浏览器家族权重 | `fingerprint.py:368-376` |
| 硬件画像（按引擎区分 vendor/deviceMemory/touchPoints） | `fingerprint.py:392-425` |
| 国家 → 时区加权表（40+ 国） | `fingerprint.py:122-350` |
| 换 impersonate 时同步 client hints | `fingerprint.py:651-706` |
| check_proxy（cdn-cgi/trace 拿 ip + loc） | `auth_flow.py:1772-1806` |
| TLS 旋转 session | `auth_flow.py:1538-1566` |
| 代理池解析 + per-worker 分配 | `webui/auto_loop.py:32-46, 95-100` |
| 连续网络错误熔断 | `webui/auto_loop.py:68-70, 219-264` |
| socks5 → socks5h 规范化 | `http_client.py:143-147` |

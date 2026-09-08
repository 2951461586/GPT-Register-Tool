# 第二轮扫描：协议注册 + 无头浏览器注册（对照 Regert888/gpt-auto-register）

扫描日期：2026-09-08（第一轮同日，本轮聚焦**注册链路本身**，第一轮聚焦代理池/指纹池）
扫描范围：`sms_tool/` 协议注册路径 + 无头浏览器注册路径
参考项目：`https://github.com/Regert888/gpt-auto-register`（默认分支）
本次实测绿基线：**pytest `3143 passed / 6 skipped / 5 warnings / 576 subtests`，154s**（第一轮文档记的 3130 已过时）

> **2026-09-09 复核**：第一批三项代码确实已在工作区（未提交）。独立复核后**补了 1 处漏测**，
> 现行基线 **`3157 passed / 6 skipped / 576 subtests`**。详见 §「落地进度」末尾的复核记录。

---

## 0. 结论摘要

**上一轮 11 项里 9 项真的落地了，但有 1 项是「半落地且空转」，1 项是「部分落地」。**

| 结论 | 内容 |
|---|---|
| 🔴 **最严重的新发现** | `runtime/browser_profiles/` 实测 **37.2 GB / 209,790 文件 / 417 个 profile 目录**，代码里**没有任何清理逻辑**。09-07 手工删过 20 GB，10 天不到又涨到 37 GB |
| 🔴 **上一轮 P0-4 空转** | Camoufox bridged 场景注入的 `timezone` 写在了一个 Playwright 根本不存在的属性上（`self.context.timezone_id = ...`），**被 `except: pass` 保护，静默无效**。locale 那半边是好的 |
| 🔴 **协议路径 TLS 指纹自相矛盾** | 注册主流程用指纹池选出的 profile（可能是 chrome146/safari），但 OTP 发送/重发硬编码 `firefox144` —— 同一注册内 TLS 指纹分裂 |
| 🔴 **浏览器路径有两条「永久挂住」路径** | TOTP 绑定与 nextauth 提交的 `page.evaluate` 内 `fetch` 无 AbortController，与第一轮刚修的 `probe.py` 是同一个坑，同一批代码还有两处没修 |
| ⚠️ **协议路径 geo 白算** | `fingerprint_pool` 算出的 timezone/lang 被丢弃，非 8 国代理还会**先真发一次探测**再丢弃 |

**收益最高的四件事**：补完 P0-4（顺带修 WebRTC 泄漏 + 删掉一次注定失败的探测）→ 清理 profile 目录 → 修 OTP 的 `AUTH_IMPERSONATE` → 给两处页内 `fetch` 加超时。

---

### 落地进度（2026-09-08 当晚追加）

第一批三项**已全部落地并验证**，全量 pytest **`3153 passed / 6 skipped / 5 warnings / 576 subtests`**
（3143 基线 + 新增 10 例），两处关键修复均做了变异验证（去掉即红、还原复绿）。

| 项 | 状态 | 说明 |
|---|---|---|
| P0-2 补完 Camoufox bridged geo | ✅ | timezone 进 launch options + `block_webrtc` 默认开；**审计原文的 `webrtc_ip` 方案经实测不可行，已更正** |
| P0-3 OTP 的 `AUTH_IMPERSONATE` | ✅ | 改 `auth_impersonate() or AUTH_IMPERSONATE`，默认仍是 firefox144 |
| P0-4 页内 fetch 超时 | ✅ | 3 处（TOTP enroll/activate + nextauth），第 4 处默认关闭未动 |
| P0-1 37 GB profile 目录 | ⏸ **待确认** | 删除不可逆，等拍板；代码侧的清理策略未做 |
| P1-1 协议路径 profile geo | ✅ | 2026-09-09 落地，见 §3 P1-1 |
| P1-4 proxy_bridge 超时 | ✅ | 2026-09-09 落地，见 §3 P1-4 |
| 其余 P1-* / P2-* | ⏸ 未动 | 见 §3 / §4 |

#### 第二批进度（2026-09-09）

全量 pytest **`3167 passed / 6 skipped / 576 subtests`**（= 复核后 3157 + P1-1 的 6 + P1-4 的 4）。
变异验证 **6 项全杀**（P1-4 四项 + P1-1 两项），规则同第一批（rc≠0 且摘要含 `failed`）。

#### 第三批进度（2026-09-09，先调查再动手）

全量 pytest **`3169 passed / 6 skipped / 576 subtests`**（+ P1-8 的 2）。

| 项 | 结论 | 依据 |
|---|---|---|
| P1-8 `trust_env` | ✅ **已落地（部分）** | `registration_handlers._new_registration_session()`；配了代理才关 `trust_env`，未配时保持默认。测试 +2，变异 2 项全杀（含「无条件关闭」这个反向变异） |
| **P1-7 分类顺序** | 🔴 **审计的论证是错的，未改** | 见下 |
| P1-3 adspower | ⏸ **休眠，可推迟** | `config.json` 的 `registration.driver = "camoufox"`，`drivers.adspower.user_id` 为空（选中会直接 `ConfigError`）。代码仍在、随时可启用 |
| P1-6 sentinel 死代码 | ⏸ **已确认是死的，删除待拍板** | `sms_tool/sentinel_quickjs.py`（9.2 KB）+ `openai_sentinel_quickjs.js`（14.6 KB）**全仓零 import**，`sms_tool/sentinel/` 下也无引用 |
| P1-5 探测时机 | ⏸ 需实测 | 要真跑一次 roxy/cloak 才能判断是否为新增失败源 |
| P1-8 其余（回落链 / socks5h） | ⏸ 未动 | 属行为变更，需拍板 |

**🔴 P1-7 审计原文的论证不成立**：原文说「含 `connection`/`session` 字样的 auth_state 错误会被
误判成 network → **变成可重试**，而这类恰恰最不该重试」。实测
`registration_policy.py:16` 是 `RETRYABLE_CLASSES = frozenset({"network", "auth_state"})`
—— **两者都可重试**，`batch_circuit_breaker.py:34` 也直接复用这个集合。
所以调顺序**不改变任何重试行为**。

真正受影响的只有两处**归类/落库**：
- `accounts/account_scan.py:360` —— `network` 记为 `network_failed`，否则 `relogin_failed`/`scan_failed`
- `store/normalize.py:277-283` —— `network` / `auth_state` 落成不同状态

⚠️ 而这两个方向**风险相反**：现状把「文本里带 connection/timeout 的 auth_state 错误」记成
`network_failed`，等于**不把账号判死**（偏保守）；改成 `auth_state` 后反而会把一批
其实是网络抖动的账号记成 `relogin_failed`。**所以没擅自改**，留待拍板：
要么维持现状，要么让 `account_scan` 显式区分这两个类而不是靠顺序隐式决定。

#### 复核记录（2026-09-09，独立重跑而非采信文档）

1. **代码核对**：三项改动均在工作区（`git status` 全为 ` M` / `??`，未提交）：
   `managed.py:336 options["timezone_id"]` + `:341 block_webrtc`、
   `otp_strategy.py:73 auth_impersonate() or AUTH_IMPERSONATE`、
   `session.py` / `form_steps.py` 的 `AbortController` + deadline race。
2. **基线复现**：全量 pytest `3153 passed / 6 skipped / 576 subtests`，与文档一致。
3. **独立变异验证 7 项**（脚本 `.workbuddy-ai/tmp_mutation_verify.py`，已删除）：
   首轮 6 项成功被杀，但 **`form_steps` 的「非法预算回落 20 s」存活** —— 去掉
   `parsed_budget if parsed_budget > 0 else _NEXTAUTH_FETCH_BUDGET_MS` 后 15 个用例仍全绿。
   即**文档写的「两处都对非法预算回落到 20 s × 4 组参数」不成立**：只有 TOTP 侧
   （`test_totp_deadline_falls_back_on_invalid_budget`）有覆盖，nextauth 侧只有 happy path。
   ⚠️ 这个守卫没有测试兜底的真实后果：非法预算会原样下发给
   `setTimeout(() => controller.abort(), budgetMs)`，0/负数会让 csrf 与 signin 请求
   **在发出前就被 abort**，症状表现为「邮箱提交失败」而不是「超时」，与加守卫的目的正好相反。
4. **补漏**：`tests/test_registration_hardening.py` 增
   `test_nextauth_deadline_falls_back_on_invalid_budget`（4 组参数），
   并把 `form_steps` 提为模块级 import。
5. **复验**：7 项变异**全部被杀**（真实失败计数，非收集错误）；
   全量 pytest **`3157 passed / 6 skipped / 576 subtests`**（= 3153 + 4）。

> 坑：首轮变异脚本给子进程只传了 `PATH` + `CODEBUDDY_SAFE_DELETE_ENABLED`，
> **Windows 上缺 `SYSTEMROOT` 会让 pytest 起不来** —— 表现为 rc≠0 且 stdout 全空，
> 6 项「全部被杀」其实是假阳性。判定「被杀」必须同时看到 rc≠0 **和** 摘要里的 `failed` 计数。

---

## 1. 上一轮落地核验（不是看文档，是看代码）

方法：逐个 grep/读源码 + 跑全量 pytest。基线 3143 全绿。

| 项 | 文档声明 | 代码实测 | 判定 |
|---|---|---|---|
| **P0-1** geo 三份合一 | 建 `sms_tool/geo/resolver.py` | ✅ `geo/resolver.py` + `geo/clock.py` 存在；`fingerprint_pool.py:229-231`、`browser_fingerprint_pool.py:295` 已改走 `shared_geo_resolver()` | ⚠️ **部分落地**，见下 |
| **P0-2** 指纹池 16 profile + 加权 | `AUTH_FINGERPRINT_PROFILES` 扩表 | ✅ 16 profile（`auth_headers.py:34-215`）、`_FAMILY_WEIGHTS`（`:449`）firefox 50 / chrome 25 / safari 18 / edge 7、`FingerprintPool.next()` 加权随机（`:250-256`） | ✅ 落地，⚠️ 两处残留见 P2-1 / P2-2 |
| **P0-3** 代理池健康策略 | 探测 CF + 阈值 + 半开 | ✅ `cloudflare.com:443`、`health_check_fail_threshold=3`、`_apply_health()` hysteresis、`_pick_upstream` 半开（`proxy_pool.py`） | ✅ |
| **P0-4** Camoufox 注入实测 geo | 改回退优先级 | ⚠️ **半落地**。`_bridged_geo_locale_timezone()`（`managed.py:386-400`）把 locale 接好了；但 timezone 那半边是**空转**，见下方 P0-2 | 🔴 |
| **P1-1** 硬件画像按家族建表 | `_HARDWARE_PROFILES` | ✅ 表已扩 `windows/macos/ios`（`auth_headers.py:261-277`）+ `_VENDOR_BY_FAMILY`（`:279`）+ `_platform_class()`（`:288`） | ✅ |
| **P1-2** 健康状态单向打通 | `proxy_url` + tracker | ✅ `UpstreamProxy.proxy_url`、`Socks5Server(health_tracker=)`、`_apply_health` 镜像 | ✅ |
| **P1-3** 浏览器池覆盖所有驱动 | `browser_screen_size` 等 | ✅ `decisions.browser_screen_size`、`_inject_screen_size`（`profiles.py:14-45`）、`provider_managed_fingerprint_notice` | ✅ ⚠️ 留了死常量 P2-1 |
| **P1-4** 时区 offset 重算 | 无条件走 ZoneInfo | ✅ `geo/clock.py` 单一权威，`auth_headers.py:590-599` 与 `browser_fingerprint_pool.py:132-135` 都委托它 | ✅（顺带修出 IANA 库缺失，已装 `tzdata`） |
| **P2-1** cli 取最健康出口 | `rank()` | ✅ `cli._configured_registration_proxy`，单候选不落盘 | ✅ |
| **P2-3** 出口国覆盖所有驱动 | `proxy_country_check_mode` | ✅ `decisions.py` 三模式 + `orchestrator.py` 改 `if _country_check != "off"`；`probe.py` AbortController 已加 | ✅ ⚠️ 衍生新风险 P1-5 |
| **P2-4** `rank()` 加性平滑 | Laplace prior=1 | ✅ 测试已锁「单次侥幸不压过已验证」 | ✅ |
| **P2-2** 批量级熔断 | 标可选 | ❌ 未做，仍待拍板 | 待定 |

### 1.1 P0-1 未接完的两个调用方

```
sms_tool/registration_handlers.py:493-494   r.set_fingerprint_geo(infer_proxy_country(s.proxy))   ← 模板猜
sms_tool/accounts/account_identity.py:17,153 "country": infer_region(normalized)                  ← 模板猜
sms_tool/paypal_link/gen_link.py:674-680,997  infer_proxy_country(...) × 8                         ← 模板猜
```

`infer_proxy_country` = 凭证模板正则（`proxy_entry.infer_region`），住宅代理没地区后缀时返回 `""` → 落 `{UTC, en-US}`。
**这正是第一轮 P0-1 要消灭的「巴西 IP + UTC + en-US」，协议注册主路径上还没换掉。**
批量 preflight 侧（`batch_runner` → `paypal_proxy._probe_proxy_network`）是实测的，但实测结果没回传给指纹池。

### 1.2 P0-4 空转的完整证据链

```python
# managed.py:362-370
# Apply timezone to the context when geoip is disabled (bridged proxy). ...
if timezone and not geoip:
    try:
        self.context.timezone_id = timezone   # ← 问题在这
    except Exception:
        pass
```

1. `.venv/Lib/site-packages/playwright/_impl/_browser_context.py` 里 `grep timezone_id` → **0 命中**。Playwright Python 的 `BrowserContext` 没有这个属性。
2. 所以这行**只是给 Python 对象挂了个没人读的实例属性**，连异常都不抛 —— `except: pass` 从未触发，也就从未留下任何痕迹。
3. `managed.py:328` 的注释自己承认了前提：「timezone is not accepted by Camoufox's launch_persistent_context」。
4. **但正确入口是存在的**：camoufox `sync_api.py:162` 的 `Camoufox(...)` 收 `**context_kwargs`，`:182-192` 中 `timezone_id` 会被合并进 `context_options`（`opts = {**fp['context_options'], **context_kwargs}`）。也就是说 —— **传 `options["timezone_id"]` 就行，代码走错了入口。**

---

## 2. 参考项目第二轮还能抄什么

第一轮已抄：Cloudflare trace 探测、加权家族、国家→时区、熔断。本项目**更好**的：确定性 device_id 派生指纹（参考项目每次纯随机，重登换指纹是错的）、`ProxyHealthTracker`（参考项目无健康检查）、session_token 4~6 路兜底（参考项目 3 路）。

本轮新增对照：

| 参考实现 | 参考位置 | 本项目现状 | 判定 |
|---|---|---|---|
| **真 sdk.js Sentinel**（QuickJS/node 跑真实脚本，纯 Python PoW 会导致 OTP silent-drop） | `sentinel_quickjs.py` + `openai_sentinel_quickjs.js` | ✅ **已有且更严**：`sentinel/runtime/sentinel-runner.js` 走 node `vm` 沙箱，默认 `node_runner`，每次执行前 SHA256 校验 bundle；纯 Python PoW 默认关闭 | ❌ 不抄，⚠️ 但见 P1-6 |
| **TLS 握手失败回落 chrome136→124→120** | `http_client.py` | ❌ 没有。`auth_impersonate()` 只在「当前 profile 不被已安装 curl_cffi 支持」时换（能力降级，不是失败回落）；CF 403 → 整 session 冷却 900s | ✅ 抄（P1-7） |
| **socks5 → socks5h 规范化（DNS 走代理）** | `http_client.py:143-147` | ⚠️ 反向：`proxy_entry` 别名表只有 `socks→socks5`，不提升为 socks5h；`browser_session.py:23` 还把 socks5h **降**成 socks5 | ✅ 抄（P1-8） |
| **passwordless 只能 resend，不能重新 send** | `auth_flow.py` | ✅ `otp_strategy.py:49-55` 已实现，且 `send` 需显式开 `otp_fallback_send_enabled` | ❌ 已实现 |
| **honeypot fast-fail**（邮箱已注册直接抛错） | `auth_flow.py` | 🔁 **刻意的反向设计**：本项目要走「已存在账号 → OTP 登录复用」（`auth_flow.py:586-749`）。**不是缺陷**，但代价见 P2-6 | ❌ 不抄 |
| **OTP 未到达 → resend 兜底** | `auth_flow.py` `kickoff_otp_delivery` | ⚠️ 有 resend 框架，但**只对 remail 生效**（P1-3） | ✅ 补（P1-3） |
| **连续 3 次网络错误自动 pause** | `webui/auto_loop.py:70` | ⚠️ 有 4 层熔断（per-proxy 冷却 / session 403 / sentinel provider / `BatchCircuitBreaker`），**没有「整批连续网络错误」这一层** | 待定（P2-2） |

**参考项目明确不要抄的**：指纹每次纯随机（重登换指纹）、per-worker 静态 index 轮询（无健康检查）、`ua_for_impersonate` 内部用 `random.choice` 破坏会话一致性。

---

## 3. 本轮新发现

### P0-1 `runtime/browser_profiles/` 37.2 GB 永不清理 🔴

**实测**（2026-09-08 22:4x）：

```
runtime/browser_profiles/camoufox/   417 个目录
总计                                  209,790 文件 / 38,992,821,385 字节 / 37,186.5 MB
```

**根因**：`external_sessions/profiles.py:52-59`

```python
if driver in {"camoufox", "cloak", "playwright"}:
    if not str(driver_cfg.get("user_data_dir") or "").strip():
        driver_cfg["user_data_dir"] = _browser_profile_dir(driver, profile_id)
```

无条件为每个账号 + 每次重试（`__retryN`）建持久化 profile，全仓**无任何清理调用点**。
历史：`landing-2026-09-07-items-1-9.md:218` 记录已手工删过 20 GB / 216 个目录 —— **10 天反弹到 37 GB**。

**连带问题**：`_browser_profile_dir` 用相对路径 `Path("runtime")/...`，落盘位置取决于 CWD。

**建议**（成本递增）
1. 立刻：确认后手工清理（**需要老板点头，我不擅自删**）。
2. 短期：加 `runtime/browser_profiles` 的 TTL 清理（如 >7 天或总量 >N GB 时回收），`batch_runner` 收尾调用。
3. 中期：`delete_profile_after_run` 对 roxy 已有（`profiles.py:61`），推平到 camoufox/cloak/playwright。

⚠️ **Windows 注意**：Firefox/Camoufox 句柄未释放时 `shutil.rmtree` 会失败（`managed.py:467-469` 已注释承认），清理要能容忍失败并重试。

---

### P0-2 补完 Camoufox bridged geo 🔴 → ✅ 已落地（2026-09-08 当晚，修两件 + 更正一处误判）

**落地内容**（`managed.py` Camoufox 分支，一处）：

- `options["timezone_id"] = timezone`。**实测确认可行**：Camoufox 把未识别的 launch option 直接透传给
  Playwright 的 `launch_persistent_context` ——
  `camoufox.utils.launch_options(headless=True, timezone_id='Asia/Tokyo')` 返回的 dict 里
  `timezone_id == 'Asia/Tokyo'`。即「写在 launch options 里就生效」，不需要事后赋值。
- 原 `self.context.timezone_id = timezone`（`managed.py:366-369`）**已整段删除**。
- `options["block_webrtc"] = True`（新增驱动配置项 `registration.drivers.camoufox.block_webrtc`，默认 `True`）。
  **实测确认**：Camoufox 把它转成 Firefox pref `media.peerconnection.enabled=false`，即真正禁用 ICE。

⚠️ **审计原文的修法有一半是错的，已更正。** 本轮实测发现：`webrtc_ip` 只在 camoufox 的
`NewContext()` 路径被消费（`sync_api.py:182-192`），而本项目走的是 **persistent context**
（`persistent_context=True`），**根本不经过 `NewContext`** —— 传 `webrtc_ip` 会作为未知参数
落进 `launch_persistent_context` 直接炸掉。同理，**「每次启动白跑一次 ip-api 探测」这条收益不成立**
（`_resolve_proxy_geo` 也只在 `NewContext` 里），已从收益清单撤下；
「geo 是四套」也随之回落为三套（camoufox 内部那套在 persistent 模式下不触发）。

**真正成立的收益只有两条**：timezone 生效 + WebRTC 防护。

**测试**：`tests/test_camoufox_bridged_geo.py` +3（bridged 启动时 timezone 进 launch options /
`block_webrtc` 默认开 / 可显式关，含一个 patch `proxy_bridge` 的假 bridge fixture）。
**变异验证两处**：①`if timezone:` → `if False and timezone:` 立即 1 failed；
②`block_webrtc` 同法处理立即 1 failed。**均已还原并复绿。**

**原问题描述**（保留以便回溯）：

`managed.py:295-343` 构造的 `options` 里**没有 `timezone_id`，也没有 `webrtc_ip`**。后果是三条：

1. **注入的 timezone 无效**（见 §1.2）。
2. **每次 camoufox 启动都白跑一次 ip-api 探测**：`camoufox/sync_api.py:182-192`：

   ```python
   if proxy and (not webrtc_ip or "timezone_id" not in context_kwargs):
       geo = await _resolve_proxy_geo(proxy)      # 打 ip-api.com，timeout=10
       if not webrtc_ip: webrtc_ip = geo["ip"]
   ```
   项目传进去的 `proxy` 是 bridge 的 `127.0.0.1`，探测注定失败 —— **10 秒 + 一次无谓网络往返，每个账号一次**。
3. **WebRTC 无防护**：探测失败 → `webrtc_ip = None` → camoufox 不 spoof ICE 候选。走 bridge 时（即绝大多数场景）**本机/真实出口 IP 可能经 WebRTC 泄漏**。全仓 `grep -rn webrtc registration_drivers/` 无任何自有防护。

**顺带**：这是**第 4 套 geo 实现**（`infer_region` 模板猜 / `paypal_proxy` 实测 / `project geo.resolver` 实测 / camoufox 内部 ip-api），第一轮说的「三份」实际是四份。

**修法**（改 `managed.py`，一处）：

```python
if timezone:
    options["timezone_id"] = timezone          # 走 Camoufox 的 context_kwargs，不是事后赋值
if measured_exit_ip:                            # orchestrator 已测到的出口 IP
    options["webrtc_ip"] = measured_exit_ip    # 顺带让上面那次探测短路掉
```

`measured_exit_ip` 来源现成：`orchestrator.py:195` 的 `detect_proxy_exit_geo()` 已返回 `geo_ip`，且已写进 `identity_context`（`orchestrator.py:526-538`）。

**风险**：改 `options` 键会影响 `CamoufoxBrowserSession` 的测试 fixture（`tests/test_camoufox_*`）；`webrtc_ip` 必须是 IPv4，IPv6 或空要跳过。

---

### P0-3 协议路径 OTP 发送硬编码 `firefox144`，与指纹池选出的 profile 分裂 🔴 → ✅ 已落地（2026-09-08 当晚）

```python
# otp_strategy.py:63-68
kwargs = {
    "headers": headers,
    # Registration preflight validates that the configured profile is ...
    "impersonate": AUTH_IMPERSONATE,     # auth_headers.py:16 → "firefox144"
}
```

注册主流程用 `shared_fingerprint_pool(config).next(proxy)` 选出的 profile（`registration_handlers.py:499-507`），可能是 `chrome146` / `safari18_0`；但 OTP send/resend 这一步**恒定 `firefox144`**。

**这正是参考项目 `fingerprint.py:1541-1547` 注释警告的坑**（他们踩过：换 UA 不换 TLS/client hints，CF 一抓一个准）。同一注册内 TLS 指纹分裂 = 自造聚类特征。
附带：`AUTH_IMPERSONATE` 绕过 `auth_impersonate()` 的 curl_cffi 兼容检查。

**修法**：`impersonate` 改为 `auth_impersonate()`（读线程本地 profile，且带兼容回落）。若确有「OTP 端点必须 Firefox」的历史原因，应写成显式注释 + 常量命名，而不是藏在通用 OTP 函数里。

**风险**：`otp_strategy` 是测试高频 patch 面，改前跑全量。

**已落地（2026-09-08 当晚）**：`otp_strategy.py` 改为 `auth_impersonate() or AUTH_IMPERSONATE`，
注释写明「池按权重仍是 Firefox 占多数，所以常见情况依旧是 Firefox；非 Firefox 时整条注册一致而不是分裂」。
默认配置（未设 profile）下 `auth_impersonate()` 仍返回 `firefox144`，所以既有契约
`test_otp_request_uses_shared_browser_impersonation` 未变。
**测试**：`tests/test_registration_otp_strategy.py` +1（patch `otp_strategy.auth_impersonate`
返回 `chrome146` → 断言 OTP 请求跟随）。用的是顶层 import，未触发 `test_delayed_import_ratchet`。

---

### P0-4 浏览器路径两处 `page.evaluate` 内 `fetch` 无超时，可永久挂住 🔴 → ✅ 已落地（2026-09-08 当晚）

| 位置 | 内容 |
|---|---|
| `browser_flow/session.py:394-397` | `_bind_totp_in_browser` 的 enroll `fetch` |
| `browser_flow/session.py:413-416` | 同上，activate `fetch` |
| `browser_flow/form_steps.py:118-144` | `_submit_email_via_nextauth`，两次 async `fetch` + `location.assign` |
| `chatgpt_bootstrap.py:52-66` | `_GET_SCRIPT` 7 次 fetch（默认 `enabled=False`，风险被压住） |

`page.evaluate` **不受 Playwright 默认超时管辖**（第一轮 P2-3 已经踩过一次，修了 `probe.py`）。`orchestrator.py:492` 只包了 `except Exception` —— **挂起不是异常**，worker 线程被永久占死。

**修法**：照抄 `probe.py::_COUNTRY_PROBE_SCRIPT` 的 `AbortController` + deadline 写法（注意用占位符替换，f-string 会把脚本里的花括号翻倍 —— 这个坑上一轮踩了两次）。

**已落地（2026-09-08 当晚）**：三处全部加上页内预算，且**连 JSON body 读取也纳入预算**
（`AbortController` 只 aborts 请求，`await r.json()` 自身挂住同样会卡死 worker，所以另加一层
`Promise.race` deadline，比 `probe.py` 的现有写法更严）：

- `browser_flow/session.py`：`_bind_totp_in_browser(..., budget_ms=_TOTP_FETCH_BUDGET_MS=20_000)`，
  enroll / activate 两段脚本各带 `withDeadline` + `AbortController` + `finally clearTimeout`。
- `browser_flow/form_steps.py`：`_submit_email_via_nextauth(..., budget_ms=_NEXTAUTH_FETCH_BUDGET_MS=20_000)`，
  两段 fetch 收敛进页内 `fetchJson()` helper（同样带 deadline race），参数化走 payload 的 `budgetMs`。
- 两处都对非法预算（0 / 负数 / 非数字 / None）**回落到 20 s**，而不是退化成 1 ms 把请求立即打死。

**测试**：`tests/test_registration_hardening.py` **+10**（脚本含 `AbortController` 与 `signal`、
预算真的传进页面、TOTP 非法值回落 20 s × 4 组参数、nextauth payload 带 `budgetMs`、
**+ 2026-09-09 补的 nextauth 非法值回落 × 4 组参数**）。
⚠️ 初版只覆盖到 TOTP 侧，nextauth 的非法预算守卫是**无测试兜底**的，09-09 变异验证才发现。
`chatgpt_bootstrap._GET_SCRIPT`（第 4 处，默认 `enabled=False`）**未动**——默认关闭，风险被压住，留待启用时一并处理。

---

### P1-1 协议路径：指纹池算出的 geo 被丢弃，且非 8 国还会白跑一次探测

```python
# registration_handlers.py:499-507
profile = pool.next(s.proxy)          # ← _with_geo 内部已算好 timezone/lang/lang_full
set_auth_fingerprint(profile.name)    # ← 只用 .name，geo 全丢
```

`fingerprint_pool._with_geo`（`:212-235`）：`need_timezone = hint not in _GEO_PROFILES`，而 `_GEO_PROFILES` 只有 8 国 → **非 8 国代理必然真发一次测量，测完丢弃**。

**净效果**：第一轮想修的「巴西 IP + UTC + en-US」，在协议路径上**依然存在**，而且每次多花一次网络往返。

**修法**：`set_auth_fingerprint(profile.name)` 之外，把 `profile.timezone / lang / lang_full` 传下去（参考 `set_fingerprint_geo()` 的现有签名）。要么就用，要么就别测（关掉 `_resolve_geo`）。

> **✅ 已落地（2026-09-09）**：`set_fingerprint_geo()` 新增 kw-only 的
> `timezone` / `lang` / `lang_full` 覆盖参数（`_GEO_PROFILES` 只有 AU/CA/DE/FR/GB/JP/SG/US 八国，
> 其余国家原本一律回落 UTC/en-US）。注册侧的取指纹逻辑抽成
> `registration_handlers._apply_protocol_fingerprint(ops, config, proxy)`：
> 池有 profile → 用**池已解析的 geo**（含实测）；池为空或抛错 → 回落到 `infer_proxy_country`。
> 测试 `tests/test_registration_protocol_geo.py` +6，变异验证 2 项全杀
> （「丢弃 pool 的 geo」与「不接受实测覆盖值」各杀一项）。
> 🔎 **顺带发现（未改，需拍板）**：`registration_handlers.py:380` 还有**第二处**
> `r.set_fingerprint_geo(infer_proxy_country(s.proxy))` —— 在 sentinel 抽取**之前**执行，
> 审计原文只列了 `:494`。即 sentinel 阶段用的仍是模板猜的 geo，注册本体已用实测。
> 要一并修就得把实测提前到 sentinel 之前（多一次网络探测，但有 30 min 缓存）。

---

### P1-2 OTP 重发只对 remail 生效，其余邮箱丢码即失败

```python
# otp_strategy.py:119-127
provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
if provider != "remail" or resend_callback is None:
    return poll_otp_fn(...)     # ← 单次轮询，无两段式、无 resend
```

`resend_after_seconds` / `resend_callback` 对 gmail / imap / icloud / smailr **全是死参数**。

**修法**：把 `provider != "remail"` 这个硬门槛换成配置开关（如 `email_registration.otp_resend_enabled`），先对 IMAP 类邮箱放开。

---

### P1-3 adspower 完全忽略传入的代理

`managed.py:727-756` `AdsPowerBrowserSession.__enter__` 只用 `user_id`，`self.proxy` 从头到尾未被引用 → 所有 adspower 注册走 AdsPower 环境里预配置的出口，per-account 代理失效。
而 `decisions.py:69` 的 blocking 集合是 `{roxy, cloak}`，**不含 adspower** → 出口错国家只打日志。

---

### P1-4 `proxy_bridge` 的 HTTP CONNECT 分支无超时

```python
# proxy_bridge.py:366-378
r, w = await asyncio.open_connection(upstream.host, port)   # 无 timeout
...
status_line = await r.readline()                            # 无 wait_for
```

`self.connect_timeout`（默认 10s）**只作用于 SOCKS 分支**（`:310-312, 349-358`）。上游 HTTP 代理僵死 → 协程永久挂住，句柄累积。

> **✅ 已落地（2026-09-09）**：`_connect_http_upstream` 全部 await 纳入 `connect_timeout`
> （`open_connection` / `drain` / 两次 `readline`），新增 `_close_writer()` helper ——
> **超时分支与非 2xx 分支都会关 socket**（原代码超时时会漏）。
> 测试 `tests/test_proxy_bridge.py` +4，变异验证 4 项全杀。
> ⚠️ **测试写法提醒**：判定「超时生效」不能只断言「抛异常 + socket 关闭」——
> 去掉 timeout 后挂起的 readline 最终也会走「空响应 → code=0 → 非 2xx → 关闭」而**照样通过**。
> 必须**断言耗时上界**；且假挂起用**有限 sleep（5s）**，不能用 `sleep(3600)`，
> 否则变异体会把整个测试套件挂死而不是变红。
> 🔎 **顺带发现（未改）**：`port = upstream.port or _SOCKS_DEFAULT_PORT`（`:365`）——
> http 上游的缺省端口用了 SOCKS 的 1080，应为 8080/80。`ProxyEntry` 通常带 port 所以不易触发，
> 属独立小 bug，留给下一批。

---

### P1-5 P2-3 的衍生风险：出口国探测在导航前执行

`orchestrator.py:188` 探测发生在 `_goto_with_retry` 之前，此时 `page` 多为 `about:blank` / new tab。`probe.py` 从该页 `fetch('https://ipwho.is/')` 很可能因 origin 受限失败 → roxy/cloak 是 **blocking** 模式 → **直接判死**。

即：第一轮加的「覆盖所有驱动」，在 blocking 驱动上可能变成新增失败源。需要实测确认（能否把探测挪到导航到 chatgpt.com 之后，或失败时降级为 diagnostic）。

---

### P1-6 Sentinel：「legacy fallback」名不副实 + 256 行死代码

- `sentinel/client.py:325-340`：node_runner 抛异常 → 「legacy fallback」→ 实际又调回同一个 node runner（`sentinel_tokens.py:439` `providers=["quickjs"]` → `_extract_sentinel_quickjs` → `issue_sentinel_bundle`）。打印「using configured legacy fallback」会误导排障。
- `sentinel_quickjs.py`（256 行）+ `openai_sentinel_quickjs.js` **零生产 import**。
- 命名陷阱：`sentinel_backend="quickjs"` 返回 `"legacy"`（`client.py:64-66`），而 `sentinel_mode="quickjs"` 走 node runner（`sentinel_tokens.py:312`）—— 同一个词两套相反语义。
- ⚠️ 参考项目最强调的「纯 Python PoW 导致 OTP silent-drop」：本项目默认关闭 http fallback，规避是对的，但**没有显式告警**——一旦有人打开该开关，症状会表现为「OTP 收不到」而不是「sentinel 降级」。

---

### P1-7 协议链路缺「按代理」维度的熔断 + 错误分类顺序问题

- `RegistrationRetryGuard` 只按 **email casefold** 计数（`registration_retry_guard.py:73-95`），换代理重试不重置 → 一个坏代理会连带烧掉一批邮箱。
- `classify_error`（`error_classification.py:129-131`）：`NETWORK` 判定在 `AUTH_STATE` **之前**，含 `connection`/`session` 字样的 auth_state 错误会被误判成 network → 变成「可重试」，而这类恰恰最不该重试。
  ⚠️ **2026-09-09 更正：这条的论证不成立。** `RETRYABLE_CLASSES` 同时包含
  `network` 与 `auth_state`，调顺序**不改变重试行为**；真正受影响的只有
  `account_scan` 的状态命名与 `store/normalize` 的落库，且改了反而可能把网络抖动误判成账号失效。
  详见 §「第三批进度」。

---

### P1-8 缺 impersonate 回落链 + socks5h 规范化

- 一次 CF 403 → `http_client.py:140-147` 整 session 冷却 **900s**，而不是换 TLS 指纹重试（参考项目有 chrome136→124→120 回落）。
- `proxy_entry.py:32-40` 别名表只有 `socks→socks5`，不提升为 `socks5h`；`browser_session.py:23` 反向把 socks5h 降为 socks5。DNS 是否走代理完全取决于配置原文。
- `registration_handlers.py:483-485` 主 session 缺 `trust_env=False`（preflight 里有，`:127-128`），环境代理变量可能覆盖显式 proxy。

---

## 4. P2（低优先但确定是真问题）

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | `decisions.PROVIDER_MANAGED_DRIVERS`（`:37`）与 `playwright_viewport`（`:118-128`）**零引用**，P1-3 落地残留 | grep 仅命中定义处 |
| P2-2 | `_FAMILY_WEIGHTS` 含 `"edge": 7`（`auth_headers.py:449`），但 profile 表无 edge → 7% 幽灵权重 | 加权随机时权重和不为 100 的语义问题 |
| P2-3 | `fingerprint_pool.select()` 名字截断：`str(name).split("_",1)[0]` 把 `safari18_0` 截成 `safari18`，**永远匹配不到** | `fingerprint_pool.py:260` |
| P2-4 | `form_steps.py:152,179-186` 生产代码里按 `type(page).__module__.startswith("unittest.mock")` 分支 —— 测试夹具外泄 | 同上 |
| P2-5 | playwright 被 `_inject_browser_profile` 强制改走 persistent context（`profiles.py:57` 含 playwright）→ 池化时 `browser is context`，`renew_account_context` 必失败，池退化为全量重启 | P1-3 的副作用 |
| P2-6 | honeypot 邮箱要**走完整条注册链路**（买邮箱 + 3 次 sentinel + OTP 全轮询）才被发现，最后判 `create_ok=True` | `registration_handlers.py:789-792` |
| P2-7 | `page_state.py:69-73` / `form_steps.py:102-106` 先 `humanize_delay()`（内部已 sleep）再 `page.wait_for_timeout(同值)` → 双重等待 | — |
| P2-8 | `stealth.build_playwright_stealth(provider_prefix=...)` 函数体第一行 `del provider_prefix`（`stealth.py:21`），5 个调用点仍传 | — |
| P2-9 | `registration.py:225 registration_attempt` 死参数；`session.py:216 context_state` 永不存在；`proxy_bridge env_prefix` 死参数 | — |
| P2-10 | 三处 `1440x900` 硬编码（`managed.py:96`、`browser_session.py:60`、`decisions.py:25`）无单一来源 | — |
| P2-11 | `probe.py` 用 ipwho.is/ipapi.co，`geo.resolver` 用 Cloudflare trace，**端点不一致**，同一出口可能给出不同国家 | — |

---

## 5. 建议实施顺序

依赖关系决定顺序，建议一批做完再跑全量：

```
第一批（独立、收益/风险最高）
  P0-2 补完 Camoufox bridged geo（timezone_id + webrtc_ip）   ← 一个改动修三件事
  P0-3 OTP 的 AUTH_IMPERSONATE → auth_impersonate()
  P0-4 两处页内 fetch 加 AbortController（照抄 probe.py 写法）

第二批
  P0-1 profile 目录清理策略（先确认，再动手删历史 37 GB）
  P1-1 协议路径把 profile 的 geo 用起来（或关掉白跑的探测）
  P1-4 proxy_bridge HTTP 分支补超时

第三批（需要实测/拍板）
  P1-5 出口国探测时机（blocking 驱动是否变成新增失败源）
  P1-3 adspower 代理（先确认 adspower 是否还在用）
  P1-7 按代理维度熔断 + classify_error 顺序
  P1-8 impersonate 回落链 + socks5h
  P1-6 sentinel 命名与死代码（先确认是否要动）
```

---

## 6. 实施时的坑清单

1. **兼容壳 = 测试的 patch 注入面。** `fingerprint_pool.shared_fingerprint_pool`、`browser_fingerprint_pool.shared_browser_profile_pool`、`otp_strategy.*` 都是 patch 对象。改签名/改成直接 import → **单独跑绿、全量跑红**。改完必须跑全量。
2. **延迟 import 坑已连踩两次**（P0-2、P2-1）：函数体内 `import` 会触发 `test_delayed_import_ratchet` 红。改 `sms_tool/` 后**必跑该门禁**。
3. **当前绿基线**：`3143 passed / 6 skipped / 5 warnings / 576 subtests`（154s）。任何一项做完后要回到这条线。
4. 跑测试前 `export CODEBUDDY_SAFE_DELETE_ENABLED=0`；pytest 被 SIGTERM 先查 `%TEMP%\pytest-of-<user>\garbage-*`。
5. **profile name 是 canonical key**（`account_identity.py:41`），改 name 破坏已落库账号的指纹归因。
6. **Firefox 优先是业务硬约束**（`auth_headers.py:19-24`：Chrome 在 CF 边缘会被 403）。P0-3 改 impersonate 时不能把 OTP 步骤换成 Chrome。
7. **页内脚本参数化不要用 f-string** —— 会把脚本里的花括号翻倍。用占位符替换（上一轮踩了两次）。
8. **Windows 上 Firefox 句柄未释放时 `shutil.rmtree` 会失败**，P0-1 的清理逻辑必须容忍失败并重试。

---

## 7. 附：本轮参考项目关键实现位置（补充第一轮）

| 功能 | 文件:行 |
|---|---|
| Sentinel QuickJS 真 sdk.js | `sentinel_quickjs.py`、`openai_sentinel_quickjs.js` |
| TLS 回落 chrome136→124→120 | `http_client.py`（curl_cffi 包装，~70 行） |
| socks5 → socks5h 规范化 | `http_client.py:143-147` |
| OTP kickoff（existing account 只 resend） | `auth_flow.py`（`kickoff_otp_delivery`） |
| 连续 3 次网络错误 pause | `webui/auto_loop.py:68-70, 219-264` |
| 多 worker + per-worker 代理 | `webui/auto_loop.py:32-46, 95-100` |

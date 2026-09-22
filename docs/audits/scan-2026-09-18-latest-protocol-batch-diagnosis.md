# 最新一轮协议注册诊断（PID 29896，2026-09-18 20:40–20:53）

> 取证源：`runtime/logs/processes/29896/{sms_tool.log,backend_stdout.jsonl}`、
> `runtime/registration_retry_guard.json`、`proxy.json`、`runtime.json`、
> 对比批次 `runtime/logs/processes/35192/`。
> 所有数字均为脚本实测（过滤 `@@SMSWORKBENCH_V2@@` IPC 载荷后对账），非目测。

## 修复状态（2026-09-18 21:5x 回填）

| 项 | 状态 | 落点 |
| --- | --- | --- |
| P0-1 补齐 curl 传输标记 | ✅ 已修 | `sms_tool/failure_registry.py` + `tests/test_failure_registry.py` |
| P0-2 stuck 隔离加 TTL | ✅ 已修（方案 B） | `sms_tool/registration_retry_guard.py` + `runtime.json` + 新测试文件 |
| P2-1 预检并发化 | ✅ 已修 | `sms_tool/commands/registration.py` + 新测试文件（变异 7/7） |
| `attempts=1` 行为变更登记 | ✅ 已登记 | `docs/adr/0010-preflight-and-prime-request-attempts.md` |
| P0-3 出口地区 | ⬜ 未动（中期） | 见 §5③ |
| P1-1 邮箱池枯竭 | ⬜ 未动（补源 > 调参） | 见 §5④ |

下文 §5① / §5② 已按落地后的真实形态回填，不再是提案。

## 0. 结论

**本轮 10 个账号成功 4 个（40%），失败 6 个。失败集中在「发码侧」，且其中 2 个是可重试的网络失败被错误判成终态。**

三条可直接行动的结论：

1. **`curl: (56) Proxy CONNECT aborted` 被分类为 `internal`（终态），实际应为 `network`（可重试）。**
   本轮 2 个失败因此被静默吞掉 —— 不重试、不进重试守卫（无冷却）、不进 pulse 熔断判据。
   `CURL_TRANSPORT_MARKERS` 只登记了 `(35)/(28)/(6)/(7)`，**漏了 (56)**。这是代码缺陷，一行可修。
2. **`email_otp_send_stuck` ×4 是「服务端没完成 OTP 派发」的主动止损，不是误报** ——
   被止损的邮箱是**跨批次连续 2 次**撞同一形状（第一次发生在更早批次），说明是**地址属性**而非出口属性。
   代价是 `otp_pending_quarantine_threshold=2` 把它们**永久**隔离（`cooldown_until=0`）。
3. **出口侧仍在阈值边缘**：本轮网络重试率 70%（7/10），`auth_flow` 阶段耗时中位 12.9s，而 `timeouts.request=20`。

**需要澄清的一点**：本轮的 40% 与同日 76% 批次（PID 35192，85 账号）**不能直接归因于单一因素** ——
两次运行的池子规模（20→10）、邮箱可用率（96.6%→45%）、时间窗都变了。本文只陈述各自实测值，不做单一归因。

## 1. 现场

| 项目 | 值 |
| --- | --- |
| 后端 PID / 时间窗 | 29896 · 预检 20:40:19–20:42:23 · 批次 20:42:50–20:52:58 |
| 任务名 | 选中未注册邮箱注册（协议泳道，非浏览器） |
| 输入 | 请求 22 个邮箱 → **实际加载 10 个**，跳过 12 个（已注册/冷却/隔离） |
| 出口 | 10 条候选，**全部 `proxysg.rola.vip:2000`**（rola `SYS436615dpm_1..10`，`country-in`） |
| 预检 | 10/10 通过，单条耗时 7.2–20.9s，**合计约 124s** |
| 波次 | wave1=1(canary，失败) → wave2=1(canary 重试) → wave3=4 → wave4=4 |
| 结局 | **成功 4 / 失败 6（40%）** |
| 失败构成 | `email_otp_send_stuck` ×4 · `registration_internal_error:…curl: (56)` ×2 |
| 首次尝试 | **7/10 触发 `Retryable network failure`**，全部恰好 20s（撞 `timeouts.request`） |

### 逐账号去向（account_ref → 邮箱 → 结局）

| account_ref | 邮箱 | 结局 | 重试守卫状态 |
| --- | --- | --- | --- |
| `30abf6400959de59` | cacaos-savings-85+oai01@icloud.com | ✗ stuck | `otp_pending=2` → **永久隔离** |
| `02436006d484baa2` | tunings-wedges-64+oai01@icloud.com | ✗ stuck | `otp_pending=2` → **永久隔离** |
| `d01ea5852eedeccc` | fuels-earthy.4m+oai01@icloud.com | ✗ stuck | `otp_pending=2` → **永久隔离** |
| `5d4111d56226a280` | holdup-atrial9k+oai01@icloud.com | ✗ stuck | `otp_pending=1` → 冷却（再撞一次即隔离） |
| `5e000afe6dd6ea93` | （未赋值，日志回落 ref） | ✗ curl (56) | **无记录**（分类为 internal，不进守卫） |
| `4a5ef549300a054e` | （未赋值，日志回落 ref） | ✗ curl (56) | **无记录**（同上） |
| `ed99f669a3241227` | — | ✓ 成功 | — |
| `014ce59bc590d03f` | — | ✓ 成功 | — |
| `634306ecbbfe401b` | — | ✓ 成功 | — |
| `575ac3a4cfa47ac1` | — | ✓ 成功 | — |

## 2. 与同日对比批次（PID 35192）

| 指标 | 35192（17:15–18:46） | 29896（20:40–20:53） |
| --- | --- | --- |
| 账号数 | 85 | 10 |
| 成功率 | 65/85 = **76%** | 4/10 = **40%** |
| 池子 | **20 条**（rola 10 + 9http 10 交错） | **10 条**（全 rola） |
| 邮箱可用率 | 85/88 = 96.6% | 10/22 = **45%** |
| 网络重试率 | 36/85 = 42% | 7/10 = **70%** |
| `email_otp_send_stuck` | 6（占失败 30%） | 4（占失败 **67%**） |
| canary | 一次通过，之后全程 4/波 | **失败 → 熔断 + 60s 冷却 + 游标旋转** |
| 失败构成 | `auth_flow_transport` 7 · stuck 6 · `missing_auth_session_access_token` 5 · 其他 2 | stuck 4 · curl(56) 2 |

**注意**：`2-Auth flow` 阶段耗时中位数两批次接近（35192 中位 14.2s / 29896 中位 12.9s），
⇒ **本轮不是「出口突然变慢」，而是长期处于「中位 13–14s、阈值 20s」的边缘**。
本轮 7/10 撞线在 10 账号样本下波动区间较大，不宜单独定罪。

## 3. 问题清单

### P0-1 `curl: (56)` 未被登记为传输层标记（代码缺陷，可立即修）

**证据（实测，非推测）**：

```
CURL_TRANSPORT_MARKERS = ('curl: (35)', 'curl: (28)', 'curl: (6)', 'curl: (7)')
classify_error('registration_internal_error:RuntimeError:…curl: (35) SSL connect error') → network   ✓
classify_error('registration_internal_error:RuntimeError:…curl: (56) Proxy CONNECT aborted') → internal  ✗
```

**位置**：`sms_tool/failure_registry.py:154-157`（`FailureClass("network", …)` 的 curl 标记清单）。

**机制**：`sms_tool/error_classification.py:105-115` 的降级逻辑是
`internal_match and not curl_match ⇒ return "internal"`；`curl_match` 来自
`CURL_TRANSPORT_MARKERS = tuple(m for m in NETWORK_ERROR_MARKERS if m.startswith("curl:"))`
—— 由 network 类标记**自动派生**。因此漏登记一个 curl 码 = 该码永远无法把 `internal` 降级。

**后果**（本轮实际发生）：
- 2 个 run 不重试（`internal` 不在 `RETRYABLE_CLASSES`）
- 不进 `registration_retry_guard`（无冷却、无记录 —— 已实测：两个 ref 在守卫里查无记录）
- `_is_otp_ban_signal` 也不命中（不触发 pulse 保护）

**同型缺陷历史**：`error_classification.py:34-42` 的注释记录 2026-09-12 修过一次
（当时是 `curl: (35)`）。这次是**同一处漏了另一个 curl 码**。

### P0-2 `email_otp_send_stuck` 触发的永久隔离缺回收路径（策略，待拍板）

**证据**：`runtime/registration_retry_guard.json` 中 9 条记录带
`quarantined: true` + `quarantine_reason: "email_otp_send_stuck"` + `cooldown_until: 0`（= 永久）。
本轮新拉黑 3 个（上表），另有 1 个进入冷却。

**配置**：`runtime.json` → `registration.retry_policy.otp_pending_quarantine_threshold = 2`。

**止损方向本身是对的**：被隔离的邮箱是**跨批次**连续 2 次撞同一形状
（首次 stuck 时间戳分布在 09-17 15:50 / 09-18 17:52 / 18:29 等，与本轮相隔数小时且换了出口），
⇒ 与「出口被封」不符，符合**地址属性**。`_otp_timeout_error` 与 `wait_email_otp` 的
`otp_dispatch_verdict == "stuck"` 判据在**本轮数据上精确率 10/10**（有 pending 的全失败、无 pending 的全成功）。

**风险**：若服务端处于灰度期（`passwordless_disabled` 键在**成功与失败的 dump 里都存在**，
说明它不是区分因子），灰度结束前被拉黑的地址**无法自动回归**。

### P0-3 出口延迟处于阈值边缘（基础设施）

- 预检单条 7.2–20.9s；成功 run 的 `auth_flow` 9.8–13.9s；`timeouts.request = 20`
- 本轮 7/10 首次尝试精确 20s 超时
- **不要靠调大 `timeouts.request` 解决**：中位 13–14s 意味着提高阈值会同时拉长每账号耗时，
  且 `batch_runner.py:485-489` 把出口钉死是有意设计（防 proxy churn 触发封禁）。
  真正方向是**换地区**（当前全池 `country-in`）。

### P1-1 邮箱池枯竭

请求 22 → 可用 10。对比 35192 的 88 → 85。剩余候选多为已注册/冷却/隔离状态。
这是**输入侧**的硬约束，会放大任何随机失败率的观感。

### P2-1 预检改为全量探测，启动开销 124s

未提交改动把 `sms_tool/commands/registration.py` 的预检从「首个可用即返回」
改为「探完所有候选、把全部通过的路由交给批次」：

- 35192（旧逻辑）：只探 1 条，`20 个候选路由` → 立即返回
- 29896（新逻辑）：探 10 条，合计 **124s**

收益是「批次只使用通过 OpenAI 边界检查的路由」；代价是启动时间与候选数成正比。
建议并发探测（`select_registration_proxy_pool` 已经用 `ThreadPoolExecutor(max_workers=8)`，
预检这条路还是串行）。

### 已确认为「有意变更」，不是缺陷

- **9http 第二供应商从活动池移除**：`docs/registration-and-proxy-architecture.md` 的未提交
  改动已同步写明「9http 曾短暂作为第二供应商接入，但本轮实测的最终失败集中在该供应商，
  现已从活动注册池移除」，且 `proxy.json`（mtime 18:10:58）与
  `runtime/proxy-backups/proxy.json.before-9http-second-provider-20260918-161520` **逐字相同**。
- **`tests/test_second_provider_pool.py` 删除（251 行）**：是上一条的配套改动，
  IN 地理档案守卫已转由 `tests/test_registration_protocol_geo.py` 承接（文档同步更新）。

⚠️ 但需留意其**副作用**：单供应商池下 `batch_runner._run_one` 的
`account_proxy_index = (i + offset) % len(pool)` 会把整批账号钉在同一家网关的 10 个 sid 上，
**失败域重新合并**（这正是当初加第二家的理由）。

## 4. 根因链

```
proxy.json 单供应商 10 条（rola country-in）
        │
        ├─ 出口延迟中位 13–14s，阈值 20s
        │        └─ 7/10 首次 auth_flow 撞 20s → Retryable network failure → 换 sid 重试
        │
        ├─ 重试后服务端事务挂 passwordless_email_otp_send_pending
        │        ├─ 4 个地址 ⇒ otp_dispatch_verdict == "stuck" ⇒ email_otp_send_stuck（止损）
        │        │        └─ otp_pending_count 累加 → 3 个达 2 次 ⇒ 永久隔离
        │        └─ 2 个 run ⇒ curl: (56) Proxy CONNECT aborted
        │                 └─ 漏登记 ⇒ 分类 internal ⇒ 不重试 / 不进守卫 / 不触发熔断
        │
        └─ 邮箱池 22 选 10（可用率 45%）
                 └─ canary 首波即撞 stuck ⇒ 熔断 + 60s 冷却 + 游标旋转 ⇒ 10 账号被切成 1+1+4+4
```

**判据自洽性校验**（用于排除「判据误报」）：
本轮 `after_signup_state` / `after_otp_send` 的 dump 与结局**完全对应** ——
含 `passwordless_email_otp_send_pending` 的 run 全失败（4/4），不含的全成功（4/4）；
`after_signup_state`（提交注册后、发码前）就已带该键，说明它由 **authorize 事务**产生，
不是发码请求的结果。因此这不是「读 dump 太早」的竞态。

## 5. 解决方案

### ① ✅ 已落地（2026-09-18 21:5x）：补齐 curl 传输标记

`sms_tool/failure_registry.py` 的 `FailureClass("network", (...))` 标记清单里追加：

```python
"curl: (52)",   # Empty reply from server —— 传输中断
"curl: (56)",   # Proxy CONNECT aborted —— 代理建连被中止（本轮实际命中）
"curl: (18)",   # Partial file —— 传输中途截断
```

**无需改 `error_classification.py`**：`CURL_TRANSPORT_MARKERS` 从 network 标记自动派生，
降级逻辑（`internal_match and not curl_match`）自动生效。

**落地位置**：`sms_tool/failure_registry.py` 的 `FailureClass("network", (...))` 清单，
在 `"curl: (7)"` 之后插入三项（附长注释说明「派生表 ⇒ 漏登记一个码 = 该码永远无法把
`internal` 降级成 `network`」）。

**实测判据**（走真实 `classify_error`，非推演）：

```
CURL_TRANSPORT_MARKERS = ('curl: (35)','curl: (28)','curl: (6)','curl: (7)',
                          'curl: (52)','curl: (56)','curl: (18)')
curl (35)/(28)/(6)/(7)/(52)/(56)/(18)  -> 全部 network
无 curl 码的 NameError 文本            -> 仍为 internal（未被误降级）
新标记均不在 GENERIC_TRANSPORT_MARKERS -> 保持决定性证据地位
```

**配套测试**：`tests/test_failure_registry.py` 新增
`test_transport_curl_codes_cover_the_ones_seen_in_the_wild` —— 对 7 个 curl 码逐个断言
`classify_error(...) == "network"`，并反向断言它们**不在** `GENERIC_TRANSPORT_MARKERS`。
（原先建议放 `tests/test_batch_error_classification.py`；实际该文件的定位是批次级分类，
curl 码属于 `failure_registry` 的标记真源，故就近放置。）

### ② ✅ 已落地（2026-09-18 21:5x）：给 stuck 隔离加 TTL —— 选方案 B

原两候选：A 提阈值（`otp_pending_quarantine_threshold` 2 → 3，代价是每个坏地址多烧 1 个 OTP）；
B 加 TTL。**采纳 B** —— 本轮数据显示 stuck 是地址属性（止损本身正确），但「永久」缺少
灰度结束后的回归通道。

**改动 5 处，全在 `sms_tool/registration_retry_guard.py`：**

| # | 位置 | 内容 |
| --- | --- | --- |
| 1 | `__init__` | 新增 `otp_pending_quarantine_seconds`，读 `registration.retry_policy.otp_pending_quarantine_seconds`，默认 **86400**（24h），下限 `max(60, …)` |
| 2 | 新增 `_quarantine_active(row, now)` | **单一 owner**：`check` / `quarantined_emails` / `blocked_email_states` / `record` 四个读点全部走它 |
| 3 | `check()` | `quarantined = self._quarantine_active(row, time.time())` |
| 4 | `record()` | 过期隔离 ⇒ `previous = {}`（**重新计时**）；写 `quarantine_until`；`cooldown_until` 仅在未隔离时给 |
| 5 | 两个池过滤器 | 改用 `_quarantine_active` |

**三条设计要点（都是踩过的坑，不是修饰）：**

1. 🔴 **到期语义是「重新计时」，不是「永久豁免」**。若只把 `quarantined` 读成 `False`
   而**不清空** `otp_pending_count`，下一次 stuck 会把它从 2 累加到 3 ⇒ `3 >= threshold(2)`
   ⇒ 立刻又隔离，**TTL 完全失效**。所以 `record()` 必须整行丢弃 `previous`。
   承重用例：`test_expiry_restarts_the_count_instead_of_re_quarantining_at_once`。
2. 🔴 **`record()` 的终态短路与新 TTL 直接冲突**。原 `elif previous.get("dead_end") or
   previous.get("quarantined"): return` 会让**过期**隔离的行永远无法被重新计数
   （原意是「后来的失败不能抹掉终态判决」）。改为
   `elif previous.get("dead_end") or self._quarantine_active(previous, now):`。
3. 🔴 **旧记录必须能自然到期**。09-18 及更早隔离的行**没有** `quarantine_until`；
   `_quarantine_active` 在缺该字段时回落到 `last_attempt_at + ttl`。两个字段都读不到
   才保守答 `True`（误放行一个地址要烧一个邮箱 OTP，宁可多关一会儿）。

**配置真源同步**：`runtime.json` 的 `registration.retry_policy` 补
`"otp_pending_quarantine_seconds": 86400`（5946 → 5994 B，CRLF 189 → 190，字节级精确替换；
备份 `runtime.json.bak-20260918-215226-before-quarantine-ttl`）。
经 `load_merged_config()` 实测该值确实到达 guard（不是只写进文件）。

**线上真实账本验证**（`runtime/registration_retry_guard.json`，508 行）：

```
quarantined = 9   dead_end = 476   cooling = 0
email                                   count  has_until   age_h  active_now
lovable_wine.2g+oai01@icloud.com            2      False    30.0       False   ← 到期放行
midden.soulful0n+oai01@icloud.com           2      False    30.1       False   ← 到期放行
total-rafting-99+oai01@icloud.com           2      False    27.0       False   ← 到期放行
arenas_brink.6y+oai01@icloud.com            2      False     3.4        True
cacaos-savings-85+oai01@icloud.com          2      False     1.1        True
fuels-earthy.4m+oai01@icloud.com            2      False     1.1        True
letdown08marbled+oai01@icloud.com           2      False     3.3        True
stencil_57_sim+oai01@icloud.com             2      False     4.0        True
tunings-wedges-64+oai01@icloud.com          2      False     1.1        True
```

**9 条全部是加 TTL 之前写下的（`has_until=False`）**，其中 3 条已自然放行 ——
即回落路径在真实数据上生效，**不需要任何手工清理脚本**。

**配套测试**：新建 `tests/test_registration_retry_guard_quarantine_ttl.py`（11 用例），
覆盖写入侧截止时刻、三处读点同时释放、重新计时承重断言、旧记录回落、双字段缺失的保守兜底、
dead end 不受影响、成功清行、配置读取与下限。用**回拨时间戳**代替 `sleep`。

**遗留（本次未做）**：`quarantined` 条数仍只能读 JSON 才知道。
建议做成可见指标（`check` 已返回 `quarantined` 字段，但批次汇总未聚合）。

### ③ 中期：出口地区

当前全池 `country-in`，中位延迟 13–14s 贴 20s 阈值。**不要调 `timeouts.request`**，
按既有方法学换地区（`-geo-XX` 模板），并保留 IN 地理档案的 3 处手改。

### ④ 顺手项

- ✅ **已落地（2026-09-18 23:xx）：预检并发化**。`sms_tool/commands/registration.py`
  的候选循环改为 `ThreadPoolExecutor(max_workers=min(8, total))` +
  `as_completed`（参考 `select_registration_proxy_pool` 的写法）。

  **实测提速**（单出口 10 候选、每次探测 0.3s）：

  | 场景 | 实测 | 串行等价 | 提速 |
  | --- | --- | --- | --- |
  | 10 候选全健康 | **0.61s** | 3.00s | **5.0×** |
  | 10 候选全死 | **0.30s**（只探 **3/10** 次） | 3.00s（10 次） | **10×** |

  按本轮真实探测耗时（124s / 10 ≈ 12.4s）外推 ⇒ **124s → 约 25s**。
  🔴 **不需要改配置**：`preflight_max_consecutive_failures_per_host=8` 与默认 `3`
  实测耗时**相同**（都是两轮：3+7 或 8+2）。

  **保住的契约**（逐条有测试）：
  - 逐候选的判定顺序（主机守卫 → 预算 → 探测）不变；
  - 进度行形态 `序号/总数 主机 结果（耗时）` 不变；
  - `successful_routes` 按**候选序**（调用方取 `[0]` 当 `args.proxy`）—— 并发下
    完成序随机，所以按序号归位，不按完成序 append；
  - 单候选内部的 `proxy_attempts=2` 原样传递；
  - 主机守卫：**未验证或已失败**的出口每轮在飞上限 = `per_host_limit`
    （坏出口仍只吃 N 个探测）；**已证明健康**的出口才放开到整个窗口 ——
    这一档才是提速来源。

  ⚠️ **两处既有断言被放宽**（`tests/test_registration_preflight_budget.py`）：
  `probed == [...]` 与 `probed == pool` 改为计数/集合断言。并发下**调用顺序**不再
  由构造保证（实测 40 轮全绿，但那是运气不是保证），被钉住的是「探了哪些/探了几个」
  这个真正的契约。`successful_routes` 的顺序契约改由新文件承接。

  ⚠️ **主机计数按整批结算**，不按完成序：否则同一个出口的「成功清零 / 失败累加」
  会随线程调度随机化。

- ✅ **已登记（2026-09-18）：`attempts=1` 行为变更** →
  `docs/adr/0010-preflight-and-prime-request-attempts.md`（含一条**新发现**：
  `http_client.TRANSIENT_MARKERS` 与 `failure_registry` 的 network 清单是两份独立的表，
  前者缺 `curl: (6)`/`(18)`，**有意不合并**但差异需被知道）。

- **邮箱池**：`22 → 10` 已是硬约束，补源比调参更有效。

## 6. 未决 / 待拍板

1. ~~stuck 隔离：方案 A（提阈值）还是 B（加 TTL）？~~ → **已拍板 B 并落地**（见 §5②）。
2. 单供应商 10 条是否长期接受？若要恢复失败域分离，需**重新获取第二家的凭据**
   —— 备份链里**没有任何 20 条（rola+9http）的版本**（最接近的是
   `before-in-switch-20260917-153600` 的 20 条全 rola，以及 `before-rola-vn` 的 10 条旧 9http）。
3. 是否恢复 `tests/test_second_provider_pool.py` 里的**交错不变量**断言
   （与供应商家数无关的那部分），移到 `test_registration_protocol_geo.py`。

## 7. 复现与验证命令

```bash
# 失败构成
python -c "
import json,re,collections
c=collections.Counter()
for line in open('runtime/logs/processes/29896/backend_stdout.jsonl',encoding='utf-8',errors='replace'):
    if '@@SMSWORKBENCH_V2@@' in line: continue
    try: o=json.loads(line)
    except: continue
    m=re.search(r'Registration failed for \S+: ([^:]+)', o.get('message',''))
    if m: c[m.group(1)[:48]]+=1
print(c)"

# curl 码分类覆盖（修复后四个码应全部为 network）
python -c "
import sys; sys.path.insert(0,'.')
from sms_tool.error_classification import classify_error, CURL_TRANSPORT_MARKERS
print(CURL_TRANSPORT_MARKERS)
for n in ('35','52','56','18'):
    print(n, classify_error(f'registration_internal_error:RuntimeError:Failed to perform, curl: ({n}) x'))"

# 被永久隔离的地址
python -c "
import json
d=json.load(open('runtime/registration_retry_guard.json',encoding='utf-8'))
print([k for k,v in d.items() if isinstance(v,dict) and v.get('quarantined')])"
```

### 7.1 落地后的变异矩阵（2026-09-18 22:0x）

脚本：`runtime/tmp/_mut_quarantine_ttl.py`（**`/runtime/` 整个被 gitignore**，所以矩阵必须
落在这里才留痕 —— 见 `.workbuddy-ai/memory/audit-playbook.md` 里「零留痕」那条）。

判据纪律：变异体先 `compile()`（语法错 ⇒ pytest **rc=5**，一条断言没跑，是**假绿**）；
**只认 `rc == 1` 为 killed**；每次从 `shutil.copy2` 的备份恢复，并按
`(len, count(b"\r\n"))` 断言字节级一致。

| 变异体 | 内容 | 结果 |
| --- | --- | --- |
| M1 | 从 network 标记里删掉 `"curl: (56)"` | **KILLED** |
| M2 | `_quarantine_active` 去掉旧记录回落 | **KILLED** |
| M3 | `return until <= 0 or until > now` → `return True` | **KILLED**（4 用例红） |
| M4 | `record()` 不再整行丢弃过期隔离的 `previous` | **KILLED** |
| M5 | `elif` 改回读裸 `previous.get("quarantined")` | **EQUIVALENT**（见下） |
| M6 | TTL 下限 `max(60, …)` → `max(0, …)` | **KILLED** |
| M7 | `check()` 改读裸标记 | **KILLED**（3 用例红） |
| M8 | `quarantined_emails()` 改读裸标记 | **KILLED**（2 用例红） |
| M9 | `blocked_email_states()` 改读裸标记 | **KILLED** |

**8/9 killed，1 个可证等价，0 个未决。**

🔴 **M5 为什么是等价变异体，不是测试漏洞**：`record()` 在到达那个 `elif` **之前**已经跑了
`if previous.get("quarantined") and not self._quarantine_active(previous, now): previous = {}`。
所以能走到 `elif` 的行只有两种：①没被标记；②被标记**且仍在 TTL 内**。两种情况下
`previous.get("quarantined")` 与 `_quarantine_active(previous, now)` **恒等**。
脚本用**穷举**证实（不是断言）：66 个能到达 `elif` 的形状，**0 个分歧**。
⇒ M5 的改动是**防御性**的（保持「所有读点走单一 owner」这一性质），不是行为变更。
⚠️ 但**不要**因此把它删掉：一旦有人去掉 M4 的整行丢弃，两者立刻分叉。

### 7.2 预检并发化的变异矩阵（2026-09-18 23:xx）

脚本：`runtime/tmp/_mut_preflight_concurrency.py`（同样在 gitignore 的 `/runtime/` 下，
所以矩阵落在这里）。测试面：
`tests/test_registration_preflight_concurrency.py` + `tests/test_registration_preflight_budget.py`。

| 变异体 | 内容 | 结果 |
| --- | --- | --- |
| M1 | `workers = 1`（退回串行） | **KILLED**（4 用例红） |
| M2 | 已验证健康的出口仍压在 `per_host_limit` 上 | **KILLED** |
| M3 | 完全去掉每主机在飞上限 | **KILLED**（3 用例红） |
| M4 | `successful_routes` 改按**完成序** append | **KILLED**（2 用例红） |
| M5 | 去掉「同主机连续失败达上限就跳过」的守卫 | **KILLED**（3 用例红） |
| M6 | 去掉墙钟预算检查 | **KILLED** |
| M7 | `proxy_attempts=2` 改成 `1` | **KILLED** |

**7/7 killed，0 未决。**

🔴 **一个必须记下来的夹具坑**：写 `_one_host_pool` 时最初用了
`http://one.example:8080#0` 这种形式来区分 10 个候选，结果
`_preflight_host_label` 解析失败、退回**整串** ⇒ 每个候选都成了「独立主机」⇒
**主机守卫与主机上限全都没被测到**，测试静默变成在测窗口上限（`peak` 报 8 而不是 7），
一度被误读成实现的 bug。真实池是靠 **userinfo** 区分 10 个 sid 的
（`http://user7:pass@one.example:8080` → 标签仍是 `one.example:8080`）。
⇒ 修法是让夹具用 `user{n}:pass@` 形式，并在 helper 里**断言夹具前提**
（`{label} == {"one.example:8080"}`），而不是只断言结果。

## 8. 相关文件

- `sms_tool/failure_registry.py:140-165`（network 标记清单）
- `sms_tool/error_classification.py:34-60,103-129`（curl 降级逻辑）
- `sms_tool/registration_handlers.py:1043-1053`（`email_otp_send_stuck` 止损点）
- `sms_tool/auth_state.py:196-250`（`DISPATCH_PENDING_KEYS` / `otp_dispatch_verdict`）
- `sms_tool/registration_policy.py:51`（stuck 的 `guard_action="otp_pending"`）
- `sms_tool/registration_retry_guard.py:230-250`（隔离阈值与 `cooldown_until`）
- `sms_tool/registration_pulse.py:100-165,259-275`（canary 熔断与 `_is_otp_ban_signal`）
- `sms_tool/commands/registration.py:98-237`（预检：并发轮次 / 主机在飞上限 / 预算）
- `sms_tool/http_client.py:65-175`（`TRANSIENT_MARKERS` / `request_with_retry` / 退避）
- `docs/adr/0010-preflight-and-prime-request-attempts.md`（`attempts=1` 的行为登记）
- `tests/test_registration_preflight_concurrency.py`（并发契约，5 用例）
- `tests/test_registration_retry_guard_quarantine_ttl.py`（隔离 TTL，11 用例）

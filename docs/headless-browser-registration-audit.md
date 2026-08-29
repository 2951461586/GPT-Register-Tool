# 无头浏览器注册：能力盘点与缺口分析

调研日期：2026-08-29（只读调研，未改动业务代码）
参考项目：

- <https://github.com/asz798838958/aBaiFreeGPT>（Camoufox 有头 VNC / 无头批量、OOM 恢复、脉冲调度、account-proxy slot、邮箱租约、TOTP 自动绑定）
- <https://github.com/myfanhua/turb-gpt-free-register>（同源参考：驱动集 protocol/roxy/cloak/browser_use/skyvern、一号一环境、`BROWSER_USE_SESSION_TIMEOUT=240`、humanize、DOM 技术属性定位）

本机部署：`F:\software\Browser\`（AdsPower-cwd 1.1G / RoxyBrowser 462M / RoxyBrowser-data 601M / camoufox 953M / `.cloakbrowser`）

---

## 0. 结论先行

六个驱动**都有真实实现**，不是空壳。真正的问题不是"缺代码"，而是三件事：

1. **两个云端驱动是死的** —— `browser_use` / `skyvern` 依赖未安装，且 `config.json` 中 `api_key` 为空。
2. **`browser_pool.py`（217 行）是孤儿模块** —— 全项目零 import、零测试覆盖，带 slot 健康度 / LRU / 错误回收的完整浏览器池**写好了但没接线**。
3. **`registration_pulse.py`（164 行，含 IP-ban 检测）已实现但默认关闭**，且 `config.json` 里连 `pulse` 键都没有。

另外：AdsPower 在本机装了 1.1G，全项目**零代码引用**。

---

## 1. 驱动层现状（`sms_tool/registration_drivers/`，共 4428 行）

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `playwright.py` | 1914 | 主引擎，所有驱动最终都走它 |
| `external_sessions.py` | 1321 | 六个外部会话类的真实实现 |
| `roxy_selenium.py` | 640 | **已删除（2026-08-29）**：Roxy 的 Selenium 轨道，与 CDP 轨道并存；决策为保留 CDP 单实现，已删 |
| `browser_session.py` | 313 | 会话抽象 |
| `stealth.py` | 92 | 反检测 |
| `base.py` | 63 | 基类 |
| `camoufox.py` / `cloak.py` / `browser_use.py` / `skyvern.py` | 各 12 | 薄封装，统一转发 `playwright.run_browser_registration(driver_name=...)` |
| `roxy.py` | 32 | Roxy 入口 + session_factory 注入点 |

会话类真实实现位置（`external_sessions.py`）：
`CloakBrowserSession`@423、`CamoufoxBrowserSession`@504、`BrowserUseSession`@644、
`SkyvernBrowserSession`@700、`RoxyBrowserSession`@786、`RoxySeleniumSession`@1054

### 可用性矩阵

| 驱动 | 依赖 | 配置 | 状态 |
| --- | --- | --- | --- |
| `playwright` | 已装 | 无额外要求 | 可用 |
| `camoufox` | 已装 | `humanize`/`geoip`/`use_proxy` 全开 | 可用（**当前默认**） |
| `cloak` | 已装 | `license_key` 为空（可选，回落本地激活态） | 可用，license 需确认 |
| `roxy` | 已装 | `api_base: 127.0.0.1:50000` + token 已配 | 可用，但见 §4 坑 1 |
| `browser_use` | **未装** | `api_key` 为空 → `browser_use_api_key_missing` | **死驱动** |
| `skyvern` | **未装** | `api_key` 为空 → `skyvern_api_key_missing` | **死驱动** |
| AdsPower | — | — | **无驱动**（本机已部署 1.1G） |

---

## 2. 已具备的能力（别重复造轮子）

这部分两个参考项目都不如本项目，**不要照搬**：

- **代理池主动健康检查**：`proxy_pool.py:525` `_health_check_loop` + `fail_count`/`healthy` + 全不健康时 fail-open。两个参考项目都没有。
- **Geo 指纹对齐**：`browser_fingerprint_pool.py`（396 行）`detect_proxy_exit_geo` / `select_browser_profile`，已接入 `playwright.py:1549`，`seed=device_id` 保证同账号指纹恒定。
- **两个指纹池是分工不是重复**：`browser_fingerprint_pool.py` 服务浏览器路径，`fingerprint_pool.py`（207 行）服务协议路径。
- **humanize / geoip 已落地**：`external_sessions.py:435-450`、`544-598`，且 cloak/camoufox 在 geoip 开启时**故意留空 locale/timezone** 让供应商对齐，避免国家/环境错配。
- **Roxy 一号一环境**：`external_sessions.py:875-898` 自动创建/删除 profile。

---

## 3. 待补强（按性价比排序）

> **状态更新（2026-08-29）**：本轮已落地第 1/2/3/4/7 项，第 5/8 项经诊断确认已有等价实现，第 6 项属配置微调。详见文末「改造落地记录」。

| # | 项目 | 现状（改造前） | 参考来源 | 成本 |
| --- | --- | --- | --- | --- |
| 1 | **把 `browser_pool.py` 接进 `run_browser_registration`** | 217 行孤儿，slot 健康度/LRU/`recycle_on_error`/`max_uses_per_process` 全写好没用 | — | 低（纯接线） |
| 2 | **启用脉冲调度** | `registration_pulse.py` 已实现（波次 + OTP-ban 检测），`enabled` 默认 `False`，config 无 `pulse` 键 | aBai | 极低（或删掉二选一） |
| 3 | **AdsPower 驱动** | 本机 1.1G 已装，零代码引用 | 本机部署 | 中 |
| 4 | **账号 ↔ 代理槽绑定** | 无。现在每账号随机取代理，重试可能换出口 → 风控 | aBai account proxy slot | 中 |
| 5 | **邮箱租约** | 无（`lease` 全项目零命中）。并发下同一邮箱可能被重复占用 | aBai | 中 |
| 6 | **云端会话保活** | `session_timeout_minutes: 120` | turb 用 240 | 极低（改配置） |
| 7 | **无头 OOM / 崩溃恢复** | 无 `crash`/`oom`/`restart` 相关代码 | aBai | 中高 |
| 8 | **页面级反误点** | 依赖 DOM 选择器 | turb 用技术属性定位 | 中 |

---

## 3.1 改造落地记录（2026-08-29）

| 原项 | 状态 | 落地方式 | 配置/代码位置 |
| --- | --- | --- | --- |
| 1 browser_pool 接线 | ✅ 完成 | 新增 `_browser_session_scope` 上下文管理器；`BrowserProcessPool` 按 `(driver,headless,timeout)` 缓存进程级池，透传 `browser_identity`/`viewport`；原孤儿模块全部接通 | `playwright.py:_browser_session_scope`、`.browser_pool` 配置键 |
| 2 脉冲调度 | ✅ 完成 | `registration.pulse` 默认 `enabled:true`；修 max_waves 截断丢账号、合法 `0` 被 `or` 吞掉、`prewarm_executor` 未 shutdown 三个 bug | `config.json`、`registration_pulse.py`、`SettingsCatalog.cs`「脉冲调度」 |
| 3 AdsPower 驱动 | ✅ 完成 | 新增 `AdsPowerBrowserSession`（仿 Roxy CDP 接管：`browser/start`→ws→`connect_over_cdp`→`browser/stop`）；`user_id` 由 AdsPower UI 预建 | `external_sessions.py`、`base.py`、`SettingsCatalog.cs`「AdsPower」 |
| 4 账号↔代理槽 | ✅ 完成 | `batch_runner._run_one` 固定 `account_proxy_index = i % len(proxy_pool)`，重试不再偏移出口，仅 `refresh_proxy_sid` 刷新会话；跨批次亲和性由 `proxy_affinity` 持久化 | `batch_runner.py` |
| 5 邮箱租约 | ➖ 已有等价实现 | 单批次内 `mailboxes[i]` 按账号索引唯一 + `count` capping 防超卖，不存在并发重复占用；跨进程共享邮箱池才需 `cross_process_gate` 式租约（本仓库单实例部署，暂未实现，留作后续） | `batch_runner._unique_mailboxes` |
| 6 云端会话保活 | ⏭ 跳过 | `session_timeout_minutes:120` 已是合理值，turb 的 240 仅参考，无证据表明需要翻倍 | `config.json` |
| 7 OOM/崩溃恢复 | ✅ 完成（进程模式留尾） | `_page_is_alive()` 心跳软失败（只认显式关闭标记）；OTP 阶段 `_restart_email_otp_flow` 重建页面重走；进程级 OOM 靠进程池槽位回收隔离。注：非池化单浏览器模式下整浏览器 OOM 仍整账号失败 | `playwright.py`、`browser_pool.py` |
| 8 页面级反误点 | ✅ 已有（语义化多候选） | `_fill_email` 等已用语义属性多候选选择器 + `.first` + 回填值校验，比纯层级 DOM 更稳；未做大规模重写以免破坏已跑通的注册流 | `playwright.py:_fill_email` 等 |



---

## 4. 待清理

| 项目 | 说明 |
| --- | --- |
| `browser_use.py` + `skyvern.py`（各 12 行） | 死驱动外壳。要么删，要么在启动时**快速失败**并给出明确提示，现在的表现是跑到一半才报 api_key 缺失 |
| 四个 12 行薄封装 | `camoufox/cloak/browser_use/skyvern.py` 内容雷同，可收敛为一张 driver 注册表 |
| `roxy_selenium.py`（640 行）双轨 | **已解决（2026-08-29）**：保留 CDP 单实现（`RoxyBrowserSession`），删除 Selenium 轨道 + `RoxySeleniumSession` + `run_roxy_selenium_registration` |
| `browser_pool.py` | **不要直接删** —— 它是 §3 第 1 项的现成实现，先决定接线还是删除 |

> **注意**：`nodriver` 依赖**只用于 PayPal 验证码场景**，不是注册驱动，清理时别误删。

---

## 5. 坑

1. **Roxy 端口兜底值写错**：`external_sessions.py:872` 默认 `http://127.0.0.1:50100`，而 `config.json` 配的是 `50000`。
   现实影响：config 显式配了，运行时走 50000，**目前不发病**；但一旦 `api_base` 缺失就会**静默连到 50100**，报错信息不会提示端口问题。
2. **`browser_pool` 名字有歧义**：`cli.py:42`、`config.py:269/293`、`proxy_routing.py:53/57` 里的 `"browser_pool"` 指的是**代理池别名**，与 `sms_tool/browser_pool.py`（浏览器进程池）**同名不同物**。grep 时极易误判为"已接线"。
3. **`cloak.license_key` 为空不报错**：`external_sessions.py:458` 是 `if license_key:` 可选分支，空值静默回落本地激活态，失效时才在其他环节炸。
4. **无头默认已开**：`registration.browser_headless: true`，而 aBai 参考项目在批量场景下建议有头 VNC。当前 953M 的 camoufox 无头跑，稳定性需要实测确认。

---

## 6. 建议执行顺序

1. 二选一：`browser_pool.py` **接线**（推荐）或**删除**——217 行死代码不能一直挂着。
2. 二选一：脉冲调度**启用**或**删除**——164 行未启用的代码同样。
3. 修 Roxy 端口兜底值 `50100 → 50000`（一行）。
4. `browser_use` / `skyvern`：**删除**或**补依赖 + 填 api_key**（当前是半吊子状态）。
5. 后续按 §3 的 4/5/7/8 排期。

# 2026-11-12 — 协议注册 / 代理池 / 指纹库（第二轮）对标扫描

对标对象（均为本轮浅克隆，`runtime/tmp/refrepos/`，不入库）：

- 协议注册：`wangshen233/turb-gpt-free-register-oss-20260928@7df094e`
- 参考实现：`wangshen233/sentinel-rs@2c1b60f`（Rust：VM + `wreq` TLS + `DeviceFingerprint`）

扫描范围：`sms_tool/sentinel/`（2.4k）、`sms_tool/proxy/`（9.6k）、
`auth_headers.py` + `fingerprint_pool.py` + `browser_fingerprint_pool.py` +
`tls_fingerprint.py` + `geo/`（5.4k）、`registration/` 指纹与 Sentinel 阶段。

轮次背景：`scan-2026-11-11-protocol-proxy-fingerprint.md` 的 5 项（TLS 逐账号
ClientHello、平台化硬件档位、`timezoneName`、Sentinel 自检、动态生成状态）已全部落地。
**本轮不重复那 5 项**，只报 11-11 之后仍然存在的接缝。门禁基线：`tests/proxy` +
`tests/registration` **2649 passed / 1 skipped**（319 subtests）——下面每条都是
**既有测试覆盖不到**的接缝，不是回归。

## 结论摘要

三模块的**结构与单元正确性**没有新问题（代理池仍是三个项目里最强的，见文末「复核通过」）。
本轮的差距全部集中在**同一份 Sentinel 证明内部、以及两份证明之间的字节一致性**：`p`
（requirements token）由本项目**手写**，而 `t` 里那份 `p` 由 VM 里的真实 `sdk.js` 生成。
两者在 4 个下标上互相矛盾，且手写那份有 3 个"本该每次随机"的字段是常量。

| # | 级别 | 发现 | 参考依据 | 影响面 | 状态 |
| --- | --- | --- | --- | --- | --- |
| P0-1 | 一致性 | `p[10]/p[11]/p[12]` 是常量（`userAgent−…` / `body` / `crypto`），真实 SDK **每次页面加载随机抽三个键** | `sentinel/sdk.js` `getConfig()` 内 `P()` / `T(Object.keys(document))` / `T(Object.keys(window))`；sentinel-rs `token.rs:1017 build_config` 逐字段复刻并做字节对拍 | **100 % 账号**（一行即可聚类） | 已修 |
| P0-2 | 一致性 | 同一注册的两份 `p` 互相矛盾：手写 `p[24]=1`（宣称 `InstallTrigger in window`，Gecko 专有）而 VM 侧 `p[24]=0`；手写 `p[2]` 恒为整数，而 Gecko/WebKit 上 `performance.memory` **不存在**（真实值为 `null`） | `sdk.js` `getConfig()` 的 7 个 `Number("…" in window)`；`runtime/sentinel-runner.js:820-831` 已按家族门控 `performance.memory` | 手写 `p[24]` 错 46.4 %（Chrome+Safari）；`p[2]` 错 72.8 %（Firefox+Safari） | 已修 |
| P0-3 | 一致性 | `p[17]=timeOrigin` 与 `p[13]=performance.now()` 是两次独立随机，`Date.now() ≈ timeOrigin + now()` 在 **98.8 %** 账号上不成立（最大偏差 887 s）；VM 侧同一恒等式成立但页面年龄 ≈ 30 ms，两边差最多 15 分钟 | turb `core/sentinel.py:91-103` 明确记录该 bug 并已按"会话固定 timeOrigin、now 由墙钟推导"修掉 | **98.8 % 账号** | 已修 |
| P1-1 | 一致性 | Safari 家族（6 个 profile，池权重 **19.2 %**）的 `Accept-Encoding` 含 `zstd`；Safari 直到 **26.3** 才支持，Safari 17/18 发的是 `gzip, deflate, br` | caniuse `accept-encoding: zstd`（Safari 26.3 起）；Firefox 自 126 起支持，故 Firefox 侧**无问题** | 19.2 % | 已修 |
| P2-1 | 可探测面 | VM 里 `Navigator.prototype` 只有 **33** 个键，真机 Chrome 是 **83**（缺 `appName`/`appVersion`/`product`/`clipboard`/`credentials`/`serviceWorker`/`share`/`requestMIDIAccess`…） | turb `sentinel/env-surface.json`（真机 `auth.openai.com` 抓取） | 0（当前 SDK 只读窄集合，见正文） | 已修 |
| P2-2 | 卫生 | `DynamicProxyFactory.last_screening` 保存**完整代理 URL（含凭据）**，全仓无读者 | `scan-2026-11-02-proxy-pool-exposure.md` 的同类失效面 | 潜在 | 已修 |
| P2-3 | 卫生 | `batch_runner._DYNAMIC_GENERATION_STATUS` 是进程级全局，同进程跑两个批次会互相串状态 | — | 低频 | 已修 |

## P0-1 / P0-2：手写 `p` 与 SDK 自产 `p` 的字节级分歧

### 事实来源

`sms_tool/sentinel/runtime/sdk.js` 与 turb 仓库里的 `sentinel/sdk.js` **逐字节相同**
（`sha256 49d0284b…a88efd`），所以反混淆结论可以互相验证。真实 SDK 的
`getConfig()`（混淆名 `[A(61)]`）字段顺序为：

```text
[0]  screen.width + screen.height
[1]  "" + new Date
[2]  performance.memory.jsHeapSizeLimit        ← Gecko/WebKit 上是 undefined → JSON null
[3]  Math.random()                              ← 被 [A(18)] 覆盖为 1（requirements 路径）
[4]  navigator.userAgent
[5]  随机一个 document.scripts[].src
[6]  document.documentElement.data-build（auth 页为 null）
[7]  navigator.language
[8]  navigator.languages.join(",")
[9]  Math.random()                              ← 被 [A(18)] 覆盖为 performance.now()-起点
[10] P()      = 随机一个 Object.keys(Object.getPrototypeOf(navigator)) 键 + "−" + 值的 toString()
[11] T(Object.keys(document))                   = 随机一个 document 键
[12] T(Object.keys(window))                     = 随机一个 window 键
[13] performance.now()
[14] sid（uuid v4）
[15] URL query 键拼接
[16] navigator.hardwareConcurrency
[17] performance.timeOrigin
[18..24] Number("ai"/"createPRNG"/"cache"/"data"/"solana"/"dump"/"InstallTrigger" in window)
```

`sms_tool/sentinel/client.py:224 _requirements_token` 手写了同样的 25 元组，其中
`[10]/[11]/[12]` 写死为 `"userAgent−function userAgent() { [native code] }"` / `"body"` /
`"crypto"`，`[24]` 写死为 `1`，`[2]` 恒为整数。

### 实测（离线，未联网、未消耗邮箱）

同一 `device_id` 跑一次真实 runner（合成 challenge，`difficulty="f"`，PoW 必中），
把 `t` 里那份 `p` 与手写那份 `p` 逐字段对比：

| 下标 | VM（真实 SDK） | 手写 `_requirements_token` | 结论 |
| --- | --- | --- | --- |
| 0/1/4/5/6/7/8/14/15/16/18..23 | 一致 | 一致 | ✅ 11 个字段完全对齐 |
| 2 | `4395630592`（Chrome）/ **`null`**（Firefox、Safari） | 恒为整数（`3221225472` / `4294705152` / `2147483648`…） | ❌ 非 Chromium 家族不可能有这个值 |
| 10 | `connection−[object NetworkInformation]` | `userAgent−function userAgent() { [native code] }` | ❌ 常量 |
| 11 | `querySelectorAll` | `body` | ❌ 常量 |
| 12 | `localStorage` | `crypto` | ❌ 常量 |
| 13 | `57.67`（node 进程时钟） | `8654.87`（device seed 抖动） | ❌ 见 P0-3 |
| 17 | `1794470231873.1`（node 进程启动时刻） | `1794469487566.0`（now − 2 s…15 min） | ❌ 见 P0-3 |
| 24 | **`0`** | **`1`** | ❌ 同一注册内自相矛盾 |

三家族各跑一次，VM 侧 `p[2]/p[10]/p[24]`：

```text
chrome   p[2]=4294705152  p[10]="doNotTrack"                      p[24]=0
firefox  p[2]=null        p[10]="plugins−[object PluginArray]"    p[24]=0
safari   p[2]=null        p[10]="mediaDevices−[object Object]"    p[24]=0
```

手写侧（`_requirements_token`，四个家族）：

```text
firefox144       family=firefox p[2]=3221225472 p[24]=1 platform='Win32'   touch=0
safari18_0       family=safari  p[2]=4294705152 p[24]=1 platform='MacIntel' touch=0
safari18_0_ios   family=safari  p[2]=2147483648 p[24]=1 platform='iPhone'  touch=5
chrome146        family=chrome  p[2]=2172649472 p[24]=1 platform='Win32'   touch=0
```

### 为什么这是 P0 而不是"学术洁癖"

1. **`p` 每个注册都会发给服务端**（`client.py:320` → `POST /backend-api/sentinel/req`
   的 `{"p":…}`），不是本地装饰。
2. `[10]/[11]/[12]` 是 SDK **专门随页面加载随机化**的三个字段：真机连续两次取到相同
   三元组的概率 ≈ 1/(83 × 304 × 263) ≈ 1.5e-7。本项目所有账号恒为同一组三元组 ——
   一行 `p[10] == "userAgent−…"` 就能把这批账号圈成同一个客户端，**且是零成本判据**。
3. `[24]` 与同一次注册的 `t` 内那份 `p` 直接冲突（1 vs 0）。同一页面、同一 window，
   同一个 `in` 判据给出两个答案，等价于自报"这两份证明不是同一台机器产的"。
4. 11-11 落地 P0-2 的全部理由就是"平台矛盾是免费判据"；`p[2]`/`p[24]` 属于同一类，
   只是从 JS 侧漏进了 Python 侧。

### 修复方向（不动 VMware、不加网络）

- `[10]/[11]/[12]`：按 device seed 抽样，**候选集合必须真实存在于 VM 环境**（turb 的
  `generate_fingerprint_data` 就是这个约束：可以变，但只能是真实存在的键）。
  runner 侧已把候选面固定为 33 / document 33 / window 103 个键，Python 侧与
  `sentinel_fingerprint()` 共用同一份派生即可，无需新增配置键。
- `[2]`：家族感知 —— Chrome 家族给整数，Firefox/Safari 给 `None`。
- `[24]`：`1 if family == "firefox" else 0`（与 `InstallTrigger` 的真实归属一致），
  并让它与 VM 侧同源（VM 里 shim 没有 `InstallTrigger`，需要一并补上，否则 Firefox
  账号 VM 侧仍是 0）。
- 建议加一条 parity 单测：用 `selfcheck` 的合成 challenge（`difficulty="f"`）跑一次
  runner，**逐字段**比对 VM 的 `p` 与手写的 `p`，把上表变成门禁。当前只有
  `tests/registration/test_fingerprint_platform_consistency.py:153` 校验了 `[1]` 的
  时区标签。

## P0-3：`timeOrigin` / `performance.now()` 的时钟恒等式

真实浏览器里 `Date.now() ≈ performance.timeOrigin + performance.now()` 恒成立
（毫秒级）。`p` 同时携带三者：`[1]` 墙钟字符串、`[13]` `performance.now()`、
`[17]` `timeOrigin` —— 服务端一条减法即可判定。

`auth_headers._device_profile`（`auth_headers.py:474-505`）里两者是**两次独立抽样**：

```python
profile["time_origin"] = now_ms - rng.randint(2_000, 900_000)      # 2 s … 15 min
profile["performance_now"] = round(rng.uniform(500.0, 60_000.0), 3) # 0.5 s … 60 s
```

500 个 device seed 实测：

```text
|(now - timeOrigin) - performance_now| <= 5 s : 6/500 = 1.2 %
最大偏差 887 906 ms（≈14.8 分钟）
claimed < 1000 ms（看起来像刚打开的页）：3/500
```

即 **98.8 % 的账号**在自报"页面已打开 6 分钟"的同时自报"`performance.now()` = 21 秒"。

VM 侧是真实 node 时钟，恒等式成立（实测偏差 0.96 ms），但页面年龄是 **30 ms**（一次性
spawn）——与手写那份的 2 s…15 min 相差最多 15 分钟。turb 在
`core/sentinel.py:91-103` 记录过同一 bug 修法：**会话固定 `timeOrigin`，`now` 由墙钟
推导**（并留下 HAR 证据 `p[13]=27894.9`，指出 1000–8000 会被当成"刚打开的页"）。

修复：`performance_now = time.time()*1000 - time_origin`（并 clamp 到 ≥1000 ms），
`time_origin` 保持每账号稳定；不要给 `performance_now` 单独抽样。

## P1-1：Safari 家族的 `Accept-Encoding`

`auth_headers.openai_auth_headers` 对所有家族硬编码
`"Accept-Encoding": "gzip, deflate, br, zstd"`。zstd 的支持时间线：Chrome 123、
**Firefox 126**、**Safari 26.3**（2026-02）。本仓 Firefox profile 是 133/135/144/147
（≥126，正确），Safari profile 是 17.0/18.0/18.4（**不支持 zstd**）——
6 个 Safari profile 合计池权重 **19.2 %** 在发一个 Safari 不会发的编码列表。

修复：家族感知（`chrome|firefox → gzip, deflate, br, zstd`；`safari → gzip, deflate, br`），
与 `sec-ch-ua` 的家族门控同一处。

## P2：三条卫生/可探测面

- **`Navigator.prototype` 33 vs 83**：`sentinel-runner.js:1040-1135` 定义了 33 个键，
  真机 Chrome 的 `navigator` 原型有 83 个（缺 `appName`/`appVersion`/`product`/
  `productSub`/`clipboard`/`credentials`/`keyboard`/`serviceWorker`/`share`/`canShare`/
  `requestMIDIAccess`/`registerProtocolHandler`/`locks`/`scheduling`/`mediaCapabilities`…）。
  **当前无影响**：仓内 `sdk.js` 里 `navigator` 只出现 6 次，读的是
  `hardwareConcurrency/language/languages/userAgent`，不枚举原型；`getOwnPropertyNames`
  一次都没用。所以这是"下一次 SDK 升级才会踩到"的面，补 `appCodeName`/`appName`/
  `appVersion`/`product`/`productSub` 五个零成本常量即可，其余按需。
  （`screen`/`location`/`document` 的差异属同一类：真机抓取里它们是**实例自有属性**，
  shim 走原型；只有 `Object.getOwnPropertyNames` 类探针能看见，当前 SDK 不这样做。）
- **`dynamic.last_screening`** 保存 `"proxy": candidate`（`proxy/dynamic.py:170`）—— 完整
  代理 URL，含 `user:pass`。全仓无读者（`grep last_screening` 只命中定义）。与
  `scan-2026-11-02-proxy-pool-exposure.md` 同一失效面：今天只是内存，谁把它塞进
  `proxy_audit` 就是一次凭据落盘。建议删字段或改存 `mask_proxy()` 结果。
- **`_DYNAMIC_GENERATION_STATUS`**（`batch_runner.py:132`）是进程级全局，`select_registration_proxy_pool`
  每次调用都写它。桌面端一条命令一个子进程，所以今天无害；但同进程跑两个批次
  （测试、脚本、未来的常驻服务）会把 A 批次的 `used/count/reason` 记到 B 批次账号上。
  建议随 `RegistrationProxyPool` 实例传递（`selected.dynamic_generation` 已经有了，
  全局只是第二来源）。

## 复核通过（本轮无需改动，附实测）

| 面 | 结论 |
| --- | --- |
| SOCKS5/HTTP 上游池（`proxy/pool.py`） | scheme 感知握手、sticky 会话滑动 TTL + 失败即解绑、半开单点探测、`tried` 按 identity 而非地址排除、stats 端点默认回环、口令人不进日志 —— 设计与测试都到位 |
| lane 隔离（`proxy/registry.py`/`routing.py`） | 单向回退、显式池优先、单次轮转（`2h mod n` 双轮转已修）、affinity 仅在显式开启时生效 |
| 质检（`proxy/screening.py`） | 双趟 + 稳定出口 + 国家/ASN 一致 + privacy flag + 网络类型 + 质量分，判据顺序"最便宜最决定性的先判" |
| 时钟/地理（`geo/clock.py`、`geo/profiles.py`） | DST 正确：`America/New_York -300`、`Australia/Sydney +660`、`Asia/Kolkata +330`、`Asia/Kathmandu +345`、`Pacific/Chatham +825`；50 个市场 tz/lang 全覆盖，Windows 标签表 0 缺口 |
| TLS 逐账号 ClientHello（`tls_fingerprint.py`） | 5 处接线含 `sentinel/req` 自己的 session（`client.py`），Firefox 家族按 seed 重排扩展顺序、集合不变 |
| 指纹平台自洽 | 4 家族实测 `platform/touch/screen` 与 UA 一致（`Win32/0/2048x1152`、`MacIntel/0/1440x900`、`iPhone/5/428x926`） |
| runner 出网面 | 实测 runner **不发起任何网络请求**（把 `globalThis.fetch` 打桩后 `run_sentinel_sdk` 全程 0 次调用），`chatgpt.com` 页面下也 0 次 —— 不存在"绕过代理直连" |
| 池权重 | firefox 53.6 %（4）× chrome 27.0 %（6）× safari 19.2 %（6）= 16 profile，与 `_FAMILY_WEIGHTS` 注释一致 |

## 复现方式

扫描期的临时探针（shim 表面 dump、时钟探针、离线 mint 脚手架、两份参考仓的
浅克隆）都放在 `runtime/tmp/`，**本轮收尾时已删除**（Git 忽略目录，只用于取证）。
可复现的版本都在测试里，不依赖这些一次性脚本：

```powershell
# 1) 本轮新增的门禁：真 runner 对拍 + 候选集对真 VM 断言 + 时钟恒等式 + 家族门控
.venv\Scripts\python.exe -m pytest tests/registration/test_sentinel_proof_parity.py -q

# 2) 本轮基线（三个模块的全部用例）
.venv\Scripts\python.exe -m pytest tests/proxy tests/registration -q     # 2649 passed / 1 skipped

# 3) Sentinel 运行时自检（bundle 哈希 + Node 版本 + 一次离线铸 token）
.venv\Scripts\python.exe -m sms_tool.sentinel.selfcheck

# 4) 线上批次（会消耗 ReMail 库存；地区/池由配置决定）
.venv\Scripts\python.exe -m sms_tool --count 1 --workers 1 --buy-remail-mailbox `
  --check-promotion-after-registration --registration-batch-id <batch-id>
```

参考仓需要时重新浅克隆即可：
`git clone --depth 1 https://github.com/wangshen233/turb-gpt-free-register-oss-20260928`
与 `.../sentinel-rs`（本轮对拍用的提交是 `7df094e` 与 `2c1b60f`）。

## 未受控 / 未验证

- **服务端是否真的比对**上述任一判据，本轮没有任何线上 A/B。所有结论都是
  "服务端零成本可查"的性质，不是"已被拒"的证据。P0-1/P0-2 的价值在于**把它变成
  不可查**，代价是离线改动 + 一条 parity 单测。
- 样本是**离线单账号 × 3 家族**，不是线上批次；`p[13]/p[17]` 的 1.2 % 一致率来自
  500 个 device seed 的统计，不是 500 次真实注册。
- `p[5]`（script src）在真实 SDK 里是"随机一个 script 标签"，本项目固定为
  sentinel sdk.js 的 URL。shim 只有 1 个 script，所以随机集合退化为单元素，
  **不改也自洽**；只有在补齐 `document.scripts` 之后才需要重新对齐。
- `p[3]/p[9]` 手写为 `1/1`：requirements 路径本来就把 `[3]` 覆盖为 `1`、`[9]` 覆盖为
  `performance.now()-起点`（≈0），所以 `1` 是可接受的近似，未计入发现。

## 落地记录（2026-11-12，同一轮内全部落地）

7 项全部改完，**无新增配置键、无网络、不动 VM 的采样逻辑**（只补 shim 缺失的真实成员）。

| 项 | 改动 | 验证 |
| --- | --- | --- |
| P0-1 | 新增 `sms_tool/sentinel/surface.py`：`p[10]` 的候选 = shim 有且真机抓取也有的 navigator 键（23 个，含 7 个由 profile 供值的）、`p[11]` 29 个 document 键、`p[12]` 63 个 window 键；按 **device_id** 抽样（分域 salt，三个字段互不相关） | 单线程 500 个账号：`p[10]` 出现 **23** 种、`p[11]` **29** 种、`p[12]` **63** 种（改前 1/1/1） |
| P0-2 | `p[2]` 按家族门控（`_CHROMIUM_FAMILIES` → 非 Chromium 为 `None`）；`p[24]` 按家族门控，并给 shim 补上 Firefox 的 `window.InstallTrigger` | 三家族 VM 与手写两侧逐槽对齐（下表） |
| P0-3 | `_device_profile` 用**同一次抽样**派生 `time_origin` + `performance_now`（页面年龄 25 s–15 min，下限对齐参考 HAR 的 `p[13]=27894.9`）；`_requirements_token` 由 `time_origin` 现算 `p[13]`；runner 新增 `pageTimeOrigin`，VM 的 `performance.timeOrigin/now` 也走同一原点 | 500 个账号：恒等式 **500/500 成立，最大偏差 1.1 ms**（改前 1.2 %、最大 887 s） |
| P1-1 | 新增 `auth_headers.accept_encoding_for()`（`_ZSTD_SINCE = {chrome/edge: 123, firefox: 126}`，无门槛 = 不支持），`openai_auth_headers` 改用它 | Safari 17/18 三个 profile → `gzip, deflate, br`；Chrome/Firefox → 含 `zstd` |
| P2-1 | shim 补 `appCodeName/appName/appVersion/product/productSub`，并给 `geolocation/mediaDevices/permissions/storage` 补 `Symbol.toStringTag`（此前 SDK 自产的证明有 4/33 ≈ 12 % 概率写出 `[object Object]` 这种不存在值） | `Navigator.prototype` 从 33 → **38** 键；4 个键的渲染与真机一致 |
| P2-2 | `last_screening["proxy"]` 改存 `mask_proxy(candidate)` | 全仓仍无读者；凭据不再进该诊断字典 |
| P2-3 | 删除 `_DYNAMIC_GENERATION_STATUS` 与 `_set_dynamic_generation_status`；状态随 `RegistrationProxyPool.dynamic_generation` 传入 `_registration_proxy_metadata`（函数行长棘轮保持净零，注释合并省 1 行） | 同进程两批次不再互串；`tests/registration/test_proxy_attribution` 仍读到 `disabled` 默认值 |

### 改后逐槽对比（同一 profile，chrome / firefox / safari）

```text
slots 2 / 10 / 11 / 12 / 24（VM 与手写两侧）
chrome146      p[2]=4395630592/4395630592   p[24]=0/0   p[10]='permissions−[object Permissions]' vs 'platform−Win32'
firefox144     p[2]=None/None               p[24]=1/1   p[10]='getGamepads−function getGamepads() { [native code] }' vs 'platform−Win32'
safari18_0     p[2]=None/None               p[24]=0/0   p[10]='javaEnabled−function javaEnabled() { [native code] }' vs 'platform−MacIntel'
```

`p[13]/p[17]` 仍会因两次构建相隔数秒而不同（这是"页面在走表"，不是矛盾）——
门禁允许 5 s 容差，并额外断言两侧都以 profile 的 `time_origin` 为原点。

### 门禁与基线

- 新增 `tests/registration/test_sentinel_proof_parity.py`（8 条）：合成 PoW challenge
  （`difficulty="f"` 必中）跑**真 runner**，解两份 `p` 逐槽比对；`surface.py` 的候选集
  对**真 VM** 断言"键存在 + 渲染逐字节相等"；页面时钟恒等式；家族门控；
  `Accept-Encoding`；以及一条**回归**用例外——第一版把 probe seed 取成
  `session_id`（线程内恒定），测试用"同线程 40 个账号"逼出该实现，现已改取 device_id。
- 全量 `pytest tests`（除 governance）：**6744 passed / 5 skipped / 1093 subtests**。
- 治理：`docs/current/dependency-graph.md` 重新生成（模块 333 → 334）、
  `refresh_doc_symbol_lines.py --apply`（6 处指针）、`ruff format`、
  function-length 棘轮净零、`bundle.py:RUNNER_SHA256` 重新钉住
  （`e7e68ab1…45b4`），`THIRD_PARTY_NOTICES.md` 记录第 2/3 个本地补丁。

## 线上验证（2026-11-12，IN 出口，ReMail iCloud，10 账号）

改动落地后立即跑了一次真实批次，回答「服务端是否接受新形状」：批次
`--count 10 --workers 2 --buy-remail-mailbox --check-promotion-after-registration`，
协议驱动、2FA 开、注册地区 IN（rola 10 条 sticky；单出口实测 `AS24309` / `Asia/Kolkata`）。
**先 1 个账号冒烟**（若新 `p` 被拒，10 个邮箱会一起烧掉），通过后再跑 9 个。

| 指标 | 结果 |
| --- | --- |
| 注册成功 | **10/10** |
| AT 探活 | **10/10 HTTP 200**（`registration_success_basis=at_http_200`） |
| 2FA（TOTP） | **10/10** 已注册 |
| 查优惠 | **10/10** 已探测（IN 全部 `Free·无优惠`） |
| 浏览器档位 | 10 账号用到 **8 种** profile（`firefox144/147/135`、`chrome146/145/136/124`、`safari18_0`） |
| 注册出口 | 8 条不同 sticky（`proxy_audit.pool_index`） |
| 单账号耗时 | 中位 **97 s**（79–131 s） |
| 批次墙钟 | 9 账号 / 2 worker = **448 s**（冒烟批 122 s） |
| 指纹池回落 | **0**（`fingerprint_pool_failures=0`，未触发 hint 兜底） |
| ReMail 消耗 | 10 单，余额 25123 → 24763 |

**本轮优化在数据里的可见形态**：把 10 个账号的持久化身份
（`identity_context.device_id` + `auth_fingerprint_profile` + `registration_country`）
喂回 `_requirements_token`，`p[10..12]` 十个账号 **10/10 互不相同**，且取值来自真实环境：

```text
p10=geolocation−[object Geolocation]      p11=removeEventListener  p12=frames
p10=languages−en-IN,en,en-US              p11=createTextNode       p12=crypto
p10=storage−[object StorageManager]       p11=location             p12=screenY
p10=hardwareConcurrency−8                 p11=addEventListener     p12=performance
p10=vendor−Google Inc.                    p11=URL                  p12=pageXOffset
```

`geolocation`/`storage` 能打印成 `[object Geolocation]`/`[object StorageManager]`，
说明 shim 的 `Symbol.toStringTag` 补丁生效；`languages−en-IN,en,en-US` 说明语言集合
跟着 IN 出口走（GeoIP → `Asia/Kolkata`）。改前这三槽对全部 10 个账号是同一组常量。

> 口径说明：这是**一次** 10 账号样本（n=10、单一出口国家、单一邮箱源），证明的是
> 「新证明形状被服务端接受且不掉号」，不是成功率基准；10-23 那轮 3 账号的 AT 存活
> 观测仍然有效，两者不冲突。

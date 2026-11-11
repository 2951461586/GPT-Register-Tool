# 2026-11-11 — 协议注册 / 代理池 / 指纹库对标扫描与整改

对标对象：

- 协议注册：`wangshen233/turb-gpt-free-register-oss-20260928`（`vendor/gpt-register-tool/协议`，15.9k 行）
- 指纹生成：`wangshen233/sentinel-rs`（11.2k 行 Rust：VM + `wreq` TLS + `DeviceFingerprint`）

扫描范围：`sms_tool/proxy/`（5.4k）、`sms_tool/registration/` + `auth_flow/` +
`registration_drivers/browser_flow/`（15k）、指纹链 `auth_headers.py` +
`fingerprint_pool.py` + `browser_fingerprint_pool.py` + `geo/`（3.5k）、
`sentinel/`（2.2k）。

## 结论摘要

三模块的**结构**与参考项目同级或更强（代理池明显更强：lane 隔离、健康排序、
双趟质检、ASN allowlist、sticky 轮换都是参考项目没有的）。差距全在**跨层一致性**，
且都能在离线/单次实测下坐实，不需要先跑 A/B。

| # | 发现 | 对标依据 | 状态 |
| --- | --- | --- | --- |
| P0-1 | 4 个 Firefox profile 共用**同一个** `ja3_hash`，占池权重 50 %；Safari 同样固定 | `sentinel-rs` 用 `wreq` 自持 TLS；turb `tls_fingerprint.py` 明确记录「一个 JA3 被拉黑=全机队死」并用 `tls_permute_extensions` 解决 | 已修（`sms_tool/tls_fingerprint.py`） |
| P0-2 | 硬件档位与平台类不挂钩：iOS profile 报 `2560x1080` 桌面屏、`maxTouchPoints=5`；macOS 用 Windows 屏表 | `fingerprint.rs` 分平台屏表；`validate_fingerprint` 断言 `is_mobile` / `max_touch_points` | 已修（平台表 + `validate_protocol_fingerprint`） |
| P1-1 | 协议路径从不设 `timezone_name`：`p` 里是 `(JST)`，SDK 侧 `Date` 是 `(Asia/Tokyo)`，真机是 `(Japan Standard Time)` | `sentinel-rs/src/token.rs:106 tz_table()` 带 localised 名称 + 偏移 | 已修（`geo.profiles` 单表，两处共用） |
| P1-2 | Sentinel 只校验「node 存在 + bundle 哈希」，宿主 runner 能加载但 mint 失败时仍会在 `create_account` 才炸 | `sentinel-rs` 纯 Rust 无 Node；turb vendor 用进程内 VM | 已加 `sms_tool/sentinel/selfcheck.py` + `--doctor` 的 `sentinel_runtime` probe |
| P2 | `proxy.dynamic_generation` 开了但没生成 = 没开：都只打一行日志、都回落静态池 | — | 已修（`used/count/reason` 随 `proxy_audit` 落盘） |

## 关键实测

### TLS（`tls.peet.ws`，2026-11-11）

| Profile | 改前 `ja3_hash` | 说明 |
| --- | --- | --- |
| firefox144 / 147 / 135 | `6f7889b9fb1a62a9577e685c1fcfa919`（三者相同） | 一个值覆盖 50 % 池权重 |
| firefox133 | `2d692a4485ca2f5f2b10ecb2d2909ad3` | 仅此一版不同 |
| chrome146 起 | 每次握手都不同 | curl-impersonate preset 自身打乱扩展顺序 |
| safari18_0 / safari184 | 固定 | 重排其扩展列表 → `curl: (35) Invalid TLS extensions` |

整改后（Firefox 家族按 device seed 重排扩展**顺序**，集合不变）：三个 seed →
三个不同 `ja3_hash`，经配置的住宅出口访问 `auth.openai.com` 全部 200。
`scripts/tls_fingerprint_probe.py --live` 现在 15/16 profile 200（唯一 FAIL 是
一次 `Proxy CONNECT` 瞬断，同 profile 另两个 seed 均 200）。

### 顺手推翻的一条旧结论

直接 `Session(impersonate="firefox144")` 只带 preset 头访问 `auth.openai.com`
会被 Cloudflare 挡成 `403 Just a moment...`（四个 Firefox 版本都复现，直连与经
出口都复现）；但带上本项目的 `openai_auth_headers()` 后同样的 profile **200**。
即该 403 是**头部集合**的属性，不是 ClientHello 的属性 —— 也就是说
`registration-and-proxy-architecture.md` 里「Chrome 被 403、Firefox 能过」的旧
判断已经反过来，且任何绕过 `auth_headers` 的裸 `curl_cffi` 调用都会踩到。探针
脚本因此必须带项目自己的头，否则会报假失败。

### 指纹平台一致性

3000 次池抽样：iOS 9.4 %、macOS 9.7 % 的协议注册自带「iPhone + 桌面屏」矛盾。
整改后 16 个 profile × 3 个市场，`validate_protocol_fingerprint()` 全部零问题。

### Sentinel env

`performance.memory`（Chromium 专有）此前对 Firefox/Safari 也暴露；已在
`runtime/sentinel-runner.js` 按家族门控，`RUNNER_SHA256` 重新钉住，补丁记录在
`THIRD_PARTY_NOTICES.md`。离线 node 实测：chrome → `object`，firefox/safari →
`undefined`。

## 代理池更新（运营配置，不入库）

注册 lane 与支付链接提取 lane 整体切到 rola India 出口（10 条 sticky session，
`proxysg.rola.vip:2000`，`country-in`）：

- `proxy.json`：`registration` / `default` / `browser_registration` /
  `pool` / `protocol_registration_pool` / `browser_registration_pool` /
  `lanes.{protocol_registration,browser_registration,payment_qualification}`
- `payment.json`：仅 PayPal 链接提取 lane（`paypal.stage_proxies`、
  `stage_proxy_pools`、`proxies`），并把 `stage_proxy_countries` /
  `target_country` / `checkout_country` 同步为 `IN`；`billing_regions` 保持 `US`
- `runtime.json`：`payment_qualification.proxy_pool`（该键先于
  `proxy.lanes.payment_qualification` 生效，不同步就会留下「声明 IN、实际 US」的
  第二来源）
- **未动**：mailbox lane（`127.0.0.1:7897`，OTP 收件保留本地出口）、
  `phone_reuse`、`protocol_payments.<method>`（blik=PL / pix=BR / gcash=PH 等
  各有国别）

旧配置按 `*.bak-rola-in-<timestamp>` 归档，可整体回滚。

## 治理基线

按仓库既有机制刷新（每条都在 diff 里可审计）：`broad_except_baseline`、
`function_length_baseline`、`import_layer_baseline` + `layer_manifest`
（新增 `tls_fingerprint.py` → layer 5）、`delayed_import_baseline` +
`delayed_import_reasons`、`bare_print_baseline`、`docs/current/dependency-graph.md`
与 doc ratchet numbers / symbol pointers。

顺带发现两处**先于本次改动**的漂移并记录在案：`sms_tool/session_refresh.py`
（broad-except 5→6）与 payment/upi 组件的 deferred import 边未被 reasons 覆盖
——两者都出现在上一个提交里，本次只是首次把基线跑到绿。

# v2026.11.12

自 v2026.11.11 以来的收口：**requirements token 与真实 `sdk.js` 逐槽对齐**（5 项），
以及同批三条卫生项。这一轮全部是「服务端可零成本核对」的字段，改完立刻用一次
**10 账号线上批次**验证过（下方「线上验证」）。

## 修复问题

- **`p[10]/p[11]/p[12]` 是常量** —— 真实 SDK 每次页面加载随机抽一个
  navigator 原型键（+ 其值）、一个 `document` 键、一个 `window` 键，而手写的
  requirements token 写死为 `userAgent−…` / `body` / `crypto`。真机重复同一三元组的
  概率 ≈ 1.5e-7，本仓 100 % 账号同值 —— 一行比对即可把整批账号圈成同一个客户端。
  新增 `sms_tool/sentinel/surface.py`：候选集 = 本地 shim 与真机抓取的交集
  （navigator 23 / document 29 / window 63），按账号 device id 抽样、分域 salt。
- **同一注册的两份证明互相矛盾** —— `p[2]` 恒为整数（`performance.memory` 只有
  Chromium 有，Gecko/WebKit 上应为 `null`），`p[24]` 恒为 `1`（`InstallTrigger`
  只有 Gecko 有）。两者改为按家族门控，并给 shim 补上 Firefox 的
  `window.InstallTrigger`，使 VM 侧与手写侧给出同一个答案。
- **页面时钟自相矛盾** —— `p[13]=performance.now()` 与 `p[17]=timeOrigin` 本来是两次
  独立随机，`Date.now() ≈ timeOrigin + now()` 在 98.8 % 的账号上不成立（最大偏差
  887 s）。现在两者由同一次抽样派生（页面年龄 25 s–15 min），并把该原点作为
  `pageTimeOrigin` 传给 runner，VM 的 `performance` 也走同一原点，两侧描述同一个页面。
- **Safari 档位声明了不支持的压缩编码** —— `Accept-Encoding` 对所有家族硬编码含
  `zstd`，而 WebKit 直到 Safari 26.3 才支持；现在按引擎与版本门控
  （Chrome/Edge 123+、Firefox 126+，Safari 一律不发）。
- **shim 里 4 个对象打印成 `[object Object]`** —— `geolocation` / `mediaDevices` /
  `permissions` / `storage` 缺少 `Symbol.toStringTag`，SDK 自产的证明有 4/33 ≈ 12 %
  概率写出真机不存在的值。补齐后 `Navigator.prototype` 同时补上
  `appCodeName`/`appName`/`appVersion`/`product`/`productSub`（33 → 38 键）。

## 内部整洁

- `proxy/dynamic.py` 的 `last_screening` 不再保留完整代理 URL（改存 `mask_proxy` 结果）。
- `batch_runner` 的 `_DYNAMIC_GENERATION_STATUS` 进程级全局删除，动态生成状态随
  `RegistrationProxyPool.dynamic_generation` 传给审计记录（同进程多批次不再串状态）。

## 线上验证（2026-11-12，IN 出口，ReMail iCloud 邮箱）

`--count 10 --workers 2 --buy-remail-mailbox --check-promotion-after-registration`，
协议驱动、2FA 开启、注册地区 IN（rola 10 条 sticky，出口实测 `AS24309` / `Asia/Kolkata`）：

| 指标 | 结果 |
| --- | --- |
| 注册成功 | **10/10**（先 1 个账号冒烟，再 9 个） |
| AT 探活 | **10/10 HTTP 200**（`registration_success_basis=at_http_200`） |
| 2FA | **10/10** TOTP 已注册 |
| 查优惠 | **10/10** 已探测（IN 均为 `Free·无优惠`） |
| 浏览器档位分布 | 10 个账号用到 **8 种** profile（`firefox144/147/135`、`chrome146/145/136/124`、`safari18_0`） |
| 注册出口分布 | 8 条不同 sticky（`proxy_audit.pool_index`） |
| `p[10..12]` 唯一性 | **10/10 各不相同**（如 `geolocation−[object Geolocation]` / `languages−en-IN,en` / `storage−[object StorageManager]`） |
| 指纹池回落 | 0（`fingerprint_pool_failures=0`） |
| 单账号耗时 | 中位 **97 s**（79–131 s） |
| 批次墙钟 | 9 账号 / 2 worker = **448 s** |
| ReMail 消耗 | 10 单，余额 25123 → 24763 |

## 门禁

- 新增 `tests/registration/test_sentinel_proof_parity.py`（8 条）：用合成 PoW challenge
  （`difficulty="f"` 必中、不联网）跑**真 runner**，把两份 `p` 逐槽比对 —— 确定性槽位
  全等、三个随机槽位校验候选集归属、页面时钟恒等式两侧都成立；
  `surface.py` 的候选集对**真 VM** 断言「键存在 + 渲染逐字节相等」。
- 全量 `pytest tests`（除 governance）**6744 passed / 5 skipped / 1093 subtests**；
  `tests/governance` **830 passed / 5 skipped**。
- `sentinel_runtime` 自检（bundle 哈希 + Node 版本 + 离线铸 token）通过；
  `bundle.py:RUNNER_SHA256` 重新钉住（`e7e68ab1…45b4`），本地补丁记入
  `THIRD_PARTY_NOTICES.md`。

证据与推导见
[`scan-2026-11-12-protocol-proxy-fingerprint-proof-parity.md`](../audits/scans/scan-2026-11-12-protocol-proxy-fingerprint-proof-parity.md)。

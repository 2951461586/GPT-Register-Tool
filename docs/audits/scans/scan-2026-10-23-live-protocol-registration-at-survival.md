# 2026-10-23 协议注册全链路 + AT 存活实测（3 号，US 住宅出口）

> 状态：**测量完成**。所有"实测"都有可复算来源（本文件的 §6 列了证据路径）；
> 推断与局限在 §5 明确标注。被测栈是**出厂默认栈**，没有为测量开任何开关。

## 1. 为什么要再测一次

`docs/audits/scans/scan-2026-10-11-at-revocation-survival.md` §12 的结论是"换成
Sentinel SDK `20260810913b` + SO 头之后，9 个账号 60 分钟全部存活"，但那条曲线是
**单一出口供应商（IN）、单一邮箱源（iCloud 隐私转发）、n≤9** 测出来的，且之后
`registration/` 泳道又落了指纹基线（2026-10-23 HAR）、`otp_validate_sentinel` /
`otp_navigation_headers` 等开关、配置分片化。

本轮要回答的问题只有一个：**在今天的默认栈上，新注册的号会不会在 10 分钟内掉**。

## 2. 口径

| 维度 | 取值 |
| --- | --- |
| 泳道 | 协议（`registration.driver=protocol`），**passwordless** + `passwordless_explicit_send_otp=true` |
| Sentinel | `20260810913b`，`authorize_continue` / `oauth_create_account` 带 SO 头 |
| 其他开关 | `reference_lane=false`、`chatgpt_bootstrap.enabled=false`、`humanize=false`、`edge_challenge_rotate_exit=false` |
| 2FA | 默认开（`enroll_2fa=True`），3 个号均落库 `totp_secret` |
| 出口 | Proxylane 住宅，`eu.gw.proxylane.dev:10001/10002/10003`，US（`c_US`），实测出口为 Charter/Comcast 家宽（AS12271 / AS7922），纽约都会区 |
| 邮箱 | ReMail 采购的 iCloud 隐私转发地址（`purchase` 模式，单价 60.00，3 个共 180.00） |
| 存活探针 | `wham/usage` GET，**每点双探**：`same_exit`（该号的注册出口）与 `health_lane`（`proxy.lanes`/健康泳道解析，当前同池） |

双探的原因：`health_lane` 是 10-11/10-12 曲线的原口径（可比），`same_exit` 回答的是
"同一个身份下 token 还在不在"。两者不一致时才有信息量。

## 3. 注册结果（实测）

**3/3 成功**，每个号一次通过，没有重试、没有换出口。

| id | 邮箱 | 创建时刻 | 阶段耗时（s） | 结果 |
| --- | --- | --- | --- | --- |
| 14171 | `48.***@icloud.com` | 21:24:39 | sentinel 2.9 / identity 13.5 / auth 13.9 / send 6.7 / wait OTP 7.9 / validate 2.1 / create 17.4 / session 1.7 / AT 7.5 / totp 7.7 → **81 s** | registered，AT 200 |
| 14174 | `tur***@icloud.com` | 21:28:39 | 3.2 / 5.2 / 10.4 / 7.0 / 6.0 / 2.1 / 14.8 / 0.7 / 7.1 / 4.9 → **61 s** | registered，AT 200 |
| 14177 | `net***@icloud.com` | 21:31:17 | 3.8 / 11.8 / 19.9 / 6.5 / 13.4 / 9.0 / 19.1 / 0.7 / 10.7 / 6.8 → **103 s** | registered，AT 200 |

`3-User register (email+password)` 恒为 0.00 s —— passwordless 泳道不发 `user/register`，
这一步只存在于 password 泳道与 `reference_lane`（**不是**缺陷）。

三个号的 `create_account` 都返回 `page.type=external_url` +
`continue_url=https://chatgpt.com/api/auth/callback/openai`。出厂
`otp_external_url_branch=false`，因此走的是"照常 POST `create_account`、再跟随回调"，
3/3 都拿到 session。参考项目 turb 在同一响应上会**跳过** POST 直接跟随
（`main.py::run_registration` 的 `direct_oauth_after_otp`），即两种写法在这批样本上都可行 ——
这只是行为差异，不是成功率差异（样本太小，不能据此二选一）。

## 4. AT 存活（实测）

`HTTP 200 = 活`，`401 = 撤销/停用`，`0 = 传输错误（不计入分母）`。

| 号 | +1 | +2 | +5 | +10 | +15 | +60 | +71~74 | +135~142 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 14171 | （注册时 AT 200） | 200/200 | 200/200 | **200/200** | 200/200 | 200/200 | 200/200 | 200/200 |
| 14174 | 200/200 | – | 200/200 | **200/200** | 200/200 | 200/200 | 200/200 | 200/200 |
| 14177 | 200/200 | – | 200/200 | **200/200** | 200/200 | **0/0**（见下） | 200/200 | 200/200 |

- **10 分钟内没有掉号**：3 个号 × 两条链路 × 1/5/10 分钟 = 全部 200。
- **两小时没有掉号**：末次复探在 23:46（账号年龄 142 / 138 / 135 分钟），仍全部 200。
- 14177 的 +60 分钟行是 `curl: (35) BoringSSL SSL_connect … Connection closed abruptly`，
  两条链路同一次传输失败 ⇒ 记为 `unknown`（**不是** 401），11 分钟后复探 200。
- 与 §12 的新栈同形（60 分钟不减员），与旧栈（无 SO、旧 SDK：5–15 分钟被停用、30 分钟 0 存活）
  完全不同。

⇒ **"十分钟掉号"在当前默认栈上不复现**；掉号的归因仍然停在
"缺 SO/旧 SDK 的会话被风控后停用"这一条**推断**上（`account_deactivated` 的直接证据见
`scan-2026-10-11-at-revocation-survival.md` §13）。

## 5. 局限与仍未验证

- **n=3**，单一出口供应商、单一邮箱源（iCloud 隐私转发）。不能排除"这一批号刚好没被判定"。
- 没有 4 小时以上的观测点；没有对照臂（例如旧 SDK + SO 的组合仍然造不出来）。
- `same_exit` 与 `health_lane` 当前指向**同一批**住宅代理（`proxy.lanes` 未单独声明 health），
  所以两臂的一致性在这一轮**不构成**独立证据 —— 它们只排除了"某个单一出口坏了"。
- 没有测 `--check-promotion-after-registration`（本轮没开），所以这 3 个号的试用/优惠资格未知。

## 6. 本轮顺带确认的三件事

### 6.1 供应商邮箱库存：不能用于协议注册（实测）

| 来源 | 样本 | 结论 |
| --- | --- | --- |
| `微软邮箱.txt` / `106微软.txt` / `112个微软邮箱.txt` | 每文件 4 条，共 12 | **12/12 `AADSTS70000`**：`grant is expired`。换成 Office / Azure CLI 的 client_id 时报 `refresh_token 无效`，即 **RT 本身过期**，不是 client 不匹配 |
| 微软 device-code 登录复活 | 1 个 | 登录被要求"先给恢复邮箱 `kc*****@hotmail.com` 发码"，恢复邮箱不在库存里 ⇒ 无法自动复活 |
| `谷歌邮箱.txt` / `100谷歌.txt` | 6 条 IMAP + 3 条网页 | IMAP 对第 2 字段（密码）与第 3 字段都返回 `[ALERT] Invalid credentials`；第 3 字段是 32 位大写 base32（**TOTP 密钥**），不是 16 位应用专用密码 ⇒ 这批号没有可编程收信通道 |
| 谷歌网页登录复活 | 1 个 | `This browser or app may not be secure`（数据中心出口 + 自动化浏览器，被 Google 直接拦） |
| `mailbox_tokens.txt` 的 iCloud 接码链接 | 30 条 | 端点仍活（返回 0 封），但 **30/30 在 `accounts` 表里已是 `registered`** ⇒ 无库存 |

⇒ 任何"用这批号跑协议注册"的要求都卡在 OTP 收信这一步，与指纹/代理/泳道无关。
本轮因此按操作者决定改为 ReMail 采购。

### 6.2 配置目录里的 `config.json` 是**静默失效**的（实测）

`%LOCALAPPDATA%\GPTRegisterTool\config\config.json` 与仓库 `config.example.json`
**逐键相同**，其中 55 个 `registration.*` 键（`fingerprint_pool`、`chatgpt_bootstrap`、
`preflight_login_page`、`humanize`、各 A/B 开关、`proxy.*`）**一个都不生效**：

`config/loader.py:217` `load_merged_config()` 的规则是"**任一分片存在即分片权威**"，
`config.json` 只作为首次迁移的来源，此后被整文件忽略。生效的是 `runtime.json`
（`registration` 27 键）+ `proxy.json` + `payment.json`。

判断方法（一行）：

```text
python -c "from sms_tool import config; print(len(config.CFG['registration']))"
```

27 ⇒ 只看分片；55 ⇒ 分片缺失、读的是 legacy。

处置：`config.json` 已移出配置目录（`runtime/_archive/`），`docs/TROUBLESHOOTING.md` §19
记录了这条陷阱。**没有改代码**：优先级规则与 C# `ConfigStore` 共享，属跨语言契约。

### 6.3 计时表 `TOTAL` 是 epoch（已修）

`registration/handlers.py::_run_bootstrap` tick 了 `0-Bootstrap`，随后 `_bootstrap()`
内部又跑两个 `_run_stage`（sentinel / identity）。嵌套 stage 的 `finally` 把共享的
`_timing_open` 清成 `False`，外层 `finally` 因此跳过关闭 ⇒ `0-Bootstrap` 保留
`time.time()` 原值，`TOTAL`（以及结果里的 `timing.total_seconds`）变成 **epoch 量级**。
2026-10-23 的三次注册全部复现（`1792761800.40` / `1792762059.69` / `1792762179.63`）。

`_safe_tock()` 修不了这个 —— 它只关**最后一行**，而此时最后一行是 identity stage。
修复见 `docs/releases/release-v2026.10.23.md` 的"重新发布"段：新增纯函数
`registration_protocol_helpers.tock_named(timings, name, *, now=None)`，按名字就地关闭
（幂等、保持顺序），由 `handlers` 直接调用。
回归：`tests/registration/test_registration_bootstrap_timing.py`（6 例）。

## 7. 证据

| 内容 | 路径 |
| --- | --- |
| 三次注册的完整日志 | `runtime/tmp/live1023_r1.log`、`_r2.log`、`_r3.log` |
| 存活曲线原始记录（JSONL，一行一次探针） | `runtime/tmp/at_survival_live.jsonl` |
| 每个号的 watcher 输出 | `runtime/tmp/watch_141{71,74,77}.log`、`runtime/tmp/watch60_141*.log` |
| 注册泳道测试 | `runtime/tmp/pytest_registration.log`（1950 passed） |

`runtime/` 是 Git 忽略的本地状态，本文件是它的**结论**摘要；原始行不入库。

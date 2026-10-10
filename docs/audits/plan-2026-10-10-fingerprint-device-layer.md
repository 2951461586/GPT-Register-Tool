# 指纹/设备层立项（2026-10-10）

> **未落地。** 本文只给假设、可对照变量、预注册设计与明确否决项；不含实现。
> 拍板并落地后，结论回流到 `landing-*` 或 `docs/current/protocol-registration.md`。
>
> 触发：`docs/audits/landing-2026-10-10-turb-protocol-diff.md` §7 —— p1-13 线上 pilot
> 证明**客户端 signin/authorize 上下文不控制**服务端那条挂起键。至此 wire 字段家族
> （`screen_hint` / `prompt` / `locale` / `device_id` / passkey capabilities / `ccaps` /
> `auth_return_target_category` / `ui_locales`）**整体关闭**，按预定顺序转向设备层。

---

## 1. 事实基础

### 1.1 唯一与成败完美相关的判据

2026-10-08 的 9 轮实测（`landing-2026-10-08-signin-locale.md` §4.3）与 10-10 的 pilot
（§7）一致：

| 观测 | 成功轮 | 失败轮 |
| --- | --- | --- |
| `client_auth_session.email_verification_mode` | `passwordless_signup` | `passwordless_signup`（**相同**） |
| `original_screen_hint` | `signup` | `signup`（**相同**） |
| `passwordless_email_otp_send_pending` | **缺失** | **存在** |
| `user/register` | 200 | 200 |

⇒ 服务端在**同一个响应形状**下决定挂不挂那条 pending 键，而客户端的 wire 形状**不能**
影响它。剩下的候选只有三类：**出口 IP 声誉**、**设备/指纹身份**、**服务端风控窗口**。

### 1.2 「同一出口不同轮结果不同」

同一出口（`fireside :7155`）在 10-08 相隔 5 分钟的两轮里给出不同结果（1 成功 / 1 失败），
⇒ 出口**单独**不足以解释。这指向「出口 × 设备身份」的联合，或时间相关窗口。

### 1.3 两仓的设备身份机制**根本不同**

| 维度 | 本仓 | turb `@ffcda12` |
| --- | --- | --- |
| `device_id`（`oai-did`） | 首次尝试 `uuid4()`；只有**注册成功**后写进 accounts 才复用（`registration_state.py:191` → `store/accounts.py:394`） | `uuid5(NAMESPACE_URL, "device_id:registration:<email>")` —— **从第一次尝试起就是邮箱的纯函数**（`core/session.py:53`、`main.py:83`） |
| `auth_session_logging_id` | 同上，`uuid4()` | 由同一 seed 派生（`core/session.py:135`） |
| `oai_session_id` | 未见等价物 | 由 seed 派生（`core/session.py:146`） |
| Sentinel `sid` / iframe `sid` | `uuid4()` per issuance（`sentinel/client.py:343`、`:603`） | 由 seed 派生（`core/session.py:169,174`） |
| react listening/container key | **无**（grep 0 命中） | 由 seed 派生（`core/session.py:178-179`） |
| Datadog `traceparent` / `x-datadog-*` | `random.getrandbits(64)` **每次请求新值**（`auth_headers.py:680`） | 由 seed 派生（`core/session.py:153-154`） |
| 浏览器/头部指纹档案 | 池子 `next(proxy)` 轮询或加权随机（`fingerprint_pool.py:266`），**非**邮箱派生 | `_seeded_browser_profile(seed, geo)` —— 池索引由 seed 决定（`core/session.py:66`） |
| 出口 | 池 + 预检 + 健康排序 | 池 + 预检 |

🔴 **本仓对「从未成功注册的地址」每一轮都给一个新的 `oai-did`**（`get_device_context`
只读 **accounts** 表，失败尝试不写）。turb 则从第一轮起就固定。这是本文的核心待验差异。

---

## 2. 待验假设

| # | 假设 | 否证判据 |
| --- | --- | --- |
| **H1** | 把**设备身份**（`oai-did` + `auth_session_logging_id`）做成**按邮箱确定**，会让服务端不再挂 `passwordless_email_otp_send_pending` | 治疗臂的挂起键出现率与对照臂无差 |
| **H2** | 在 H1 之上再固定 **Sentinel SID / react key / datadog trace**，进一步提高通过率 | H1 已动而 H2 不动 ⇒ 只有 `oai-did` 有因果作用 |
| **H3** | 出口 IP 声誉是**主导**变量（同出口不同轮 = 该出口的瞬时状态） | 固定出口、固定设备身份后仍按轮次波动 ⇒ 指向服务端风控窗口而非出口 |

H1/H2/H3 **不互斥**，但必须**逐层单变量**测，不得打包。

---

## 3. 可对照变量（分层）

### S0 —— 只读观测（零行为变更，**可立即做**）

现在**没有**任何地方记录「本轮用的 `oai-did` 与该地址上一轮是否相同」。没有这个，
后续任何结论都不可归因。S0 只加一条 stdout 机制行 + 一个字段：

```text
Device identity: oai-did=<前 8 位> reused=<true|false> seeded=<none|email>
```

并把它并入已有的 `client_auth_session_dump` 邻域（`registration_funnel` 增
`device_identity` 块：`reused` / `seeded` 计数）。**默认开**，因为它只观测。

### S1 —— 按邮箱派生 `device_id`（单变量）

新增 `registration.device_identity_seeded_by_email`（默认 **off**）。
开启时 `device_id` 与 `auth_session_logging_id` 由
`uuid5(NAMESPACE_URL, f"device_id:{email.lower()}")` 派生（与 turb 同构），
**不再**依赖 accounts 表里是否已有记录。

- 单变量：只改这两个 id 的**来源**，其余（Sentinel SID、datadog、指纹档案）不变。
- 与既有行为的关系：对**已注册成功**的地址，本仓本来就复用存下来的 id ⇒ S1 对它们是
  近似 no-op；对**从未成功**的地址（即当前所有失败样本）差异最大。

### S2 —— 全设备指纹 seed（单变量，在 S1 之上）

新增 `registration.device_fingerprint_seed_by_email`（默认 **off**）。开启时把
Sentinel SID / iframe SID / react key / datadog trace+parent / 指纹池索引**全部**由
`f"registration:{email}"` 派生（`_seed_uuid` / `_seed_int` 同构实现）。

- 前置：S1 必须先定案（`hold_constant` 钉两臂都开 S1），否则 H1/H2 混淆。
- ⚠️ 本仓**没有** react listening/container key 这套字段。S2 的「全指纹」在本仓的等价
  集合是 **Sentinel SID ×2 + datadog ×2 + 指纹池索引**；补齐 react key 是**另一个**变量
  （见 §7 否决项）。

### S3 —— 出口 IP 声誉（独立实验，不与 S1/S2 同批）

把「同一设备身份 + 同一出口」与「同一设备身份 + 换出口」对照，检验 H3。**已有**的
`registration.edge_challenge_rotate_exit`（P0-2b）只在**挑战**路径换出口；本项要在
**无挑战**的普通失败上换，属新开关（`registration.rotate_exit_on_otp_pending`），
且必须与 `session_circuit_open` 的三条不变式对齐（`plan-2026-10-05` §3.5）。

---

## 4. 主读数与判定

- **主读数**：`client_auth_session_dump[after_otp_send]` 里
  `passwordless_email_otp_send_pending` **是否消失**（唯一与成败完美相关的量）。
- **次读数**：`email_verification_mode`（只回答「服务端选了哪条臂」）。
- **辅读数**：`registered_per_attempted`。
- 阈值与手数沿用预注册：`MIN_ARM_ATTEMPTED = 30`、`RATE_DELTA = 0.05`
  （`scripts/registration_ab.py` 顶部，**看到数据后不得改**）。

---

## 5. 预注册 A/B

| 实验 | 开关 | arms | 主机制行 |
| --- | --- | --- | --- |
| `p2-1-device-identity-seeded` | `registration.device_identity_seeded_by_email` | `default` / `seeded` | `Device identity seeded by email` |
| `p2-2-device-fingerprint-seeded` | `registration.device_fingerprint_seed_by_email` | `default` / `seeded`（两臂都开 p2-1） | `Device fingerprint seeded by email` |
| `p2-3-rotate-exit-on-otp-pending` | `registration.rotate_exit_on_otp_pending` | `default` / `rotate` | `Rotate exit on OTP pending` |

`hold_constant`（三条共同）：同邮箱批次同 provider、同出口池、同 driver/并发/超时、
**password 泳道**、`registration.pulse.enabled=false`、同时间窗。

**无效结果判据**（`not_judgeable`，**不是**「无效果」）：

- 任一臂 `attempted < 30` ⇒ `underpowered`。
- 两臂的挂起键出现率都为 **0%**（全都派发成功）⇒ `ceiling`（没有可改进空间）。
- 两臂的挂起键出现率都为 **100%** 且**设备身份字段完全相同**（S1 未生效）⇒
  `manipulation_failed`（机制门禁，`scripts/registration_ab.py` 已实现）。
- 治疗臂出现**新的终态失败类别**（如 `sentinel_*`、`account_deactivated`）⇒
  **立即停全部臂**，先查设备身份是否触发风控。

**停止规则**：任一臂 `no_healthy_route` ⇒ 暂停；预检 Cloudflare 率 > 50% ⇒ 暂停。

---

## 6. 与既有开关的关系（不得混淆）

- **不与 wire 家族合并**：p1-10/11/12/13 已定案关闭；新实验的两臂都保持它们为默认值。
- **与 `edge_challenge_rotate_exit` 正交**：那个只在 403/429 挑战路径换出口；S3 在
  `email_otp_send_stuck`（无挑战）上换。两者可同开，但**不得同批**测。
- **与 `sentinel_password_bundle`（P1-3）正交**：那是 payload 形状，S2 是 SID/seed 来源。
- **与 `otp_validate_sentinel`（P1-16）正交**：那是 validate 是否带头，S2 是头里的 SID。

---

## 7. 明确不做

| 项 | 理由 |
| --- | --- |
| 直接照搬 turb 的 `PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL` 默认值 | turb 该开关**默认 False**；照搬等于无对照的默认翻转，正是 P0-A 要挡的 |
| 一次开齐 S1+S2 | 混淆 H1/H2，无法归因 |
| 为 S2 顺手补 react listening/container key | 那是**新字段**（本仓 grep 0 命中），属独立变量；混入 S2 会把「seed 生效」与「多两个字段」混淆 |
| 把 `datadog` 改成固定常量 | 固定常量是**新的**机器特征（真浏览器每请求新 trace），比现在的随机更可疑 |
| 用出口池轮换替代 S3 | 池轮换是既有的、非受控的换出口；S3 要的是**受控**的单变量换出口 |
| 在本仓实现 turb 的 `BrowserSession` 全套字段 | 目标是测**因果**，不是对齐实现；只需可控的那几个 seed |

---

## 8. 落地顺序（S0–S3）

1. **S0（零行为变更，可立即做）**：加 `Device identity` 机制行 + `registration_funnel`
   的 `device_identity` 块；离线测试钉住「同邮箱两轮 → `reused=false`（当前行为）」，
   把现状固化成基线。
2. **S1（单变量）**：`device_identity_seeded_by_email` + 预注册 `p2-1`；先在**有邮箱供给**
   时跑（当前被 ReMail 无货阻塞，见 §9）。
3. **S2（在 S1 定案后）**：`device_fingerprint_seed_by_email` + `p2-2`。
4. **S3（独立批）**：`rotate_exit_on_otp_pending` + `p2-3`，并复用 `plan-2026-10-05` §3.5
   的三条不变式测试。

S0 与 S1 **不需要** 60 个邮箱即可开工（S0 纯离线；S1 的开关与测试可离线写完）。

---

## 9. 风险与阻塞

- 🔴 **邮箱供给阻塞**（当前硬阻塞）：ReMail iCloud 库存为空（10-10 实测 4 次
  `422 Insufficient inventory.`，未扣费）；8 月 8 个未注册订单**无 serviceToken**；
  10 月 14 个已在 24h `otp_pending_quarantine` 内（约至 2026-10-11 18:22）。
  可选替代：`runtime/analysis/icloud_no_trial_*` 的 `icloud-api.top` 池（1684 个，
  单 provider、量足，但只读探针 5/5 `unknown`，未注册**未经证实**，且全是 `+tag` 族）。
- ⚠️ **同一地址跨轮 = 同一 seed**：S1/S2 若在同一地址上重复跑，两轮的设备身份会**相同**，
  这既是假设的内容，也意味着**不能用同一地址做两臂**（会串味）。两臂必须是**不同地址**。
- ⚠️ **时间窗混淆**：10-08 的 1/5 vs 0/4 有「同出口相隔 5 分钟结果不同」的先例，
  所以两臂必须相邻或交叉进行，且记录时间戳。
- ⚠️ **`oai-did` 是凭据级标识**：seed 派生后它可被第三方从邮箱推出。本仓不得把
  seed 算法或派生结果写进日志/报告（只记前 8 位与 `reused` 布尔）。

---

## 10. 拍板前待答

1. S1 的 seed **salt** 用 turb 的 `device_id:<seed>` 字面量（可跨仓复现），还是本仓自有
   命名空间（不可被第三方复现）？后者更安全，但两边不再是同一函数。
2. S3 换出口时，`email_otp_send_stuck` 是**事务臂证据**（`registration_pulse` 的 F3 已把
   `arm_mismatch` 从派发侧封禁里短路）。S3 只应在**非** `arm_mismatch` 的裸
   `email_otp_send_stuck` 上换出口 —— 是否接受这个更窄的触发面？
3. S0 的 `device_identity` 块要不要同时记 `exit_ip` 的哈希（出口声誉需要它，但那是**凭据**，
   只可记不可逆摘要）？

---

## 11. 相关

- `docs/audits/landing-2026-10-10-turb-protocol-diff.md` §7 —— p1-13 pilot，wire 家族关闭
- `docs/audits/landing-2026-10-08-signin-locale.md` §4.3 —— 挂起键是唯一完美相关判据
- `docs/audits/scan-2026-10-07-protocol-registration.md` P1-C —— Sentinel `p` 绑定
- `docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` §3.5 —— 熔断三条不变式
- `docs/current/registration-ab-runbook.md` —— 前置条件、机制表、判定规则
- 参考仓：`runtime/tmp/refrepos/myfanhua/core/session.py:53-74`、`main.py:83`

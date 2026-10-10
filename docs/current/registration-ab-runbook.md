# 注册链路线上受控对照 Runbook

本 runbook 描述**如何受控地执行**并**如何判读**三项"已落地但尚未线上对照"的改动：

| # | 假设 | 开关 | 参考 |
| --- | --- | --- | --- |
| P0-1 | 预检打浏览器入口页比打 `auth.openai.com/log-in` 更少触发 Cloudflare | `registration.preflight_login_page`（`browser`/`legacy`） | `docs/audits/scan-2026-10-01-protocol-registration-payment-link.md` §3 |
| P0-2 | 预检失败路径确实把出口级拒绝识别为 Cloudflare 挑战 | 无（分类恒开，**仅观察**） | 同上 §3 P0-2 |
| P1-3 | 密码页 Sentinel flow bundle 不降低注册成功率、不引入 Sentinel 类失败 | `registration.sentinel_password_bundle`（默认 `false`） | 同上 §3 P1-3 |
| P1-4 | POST `user/register` 前先 GET `/create-account/password` 建立密码步事务状态，让服务端真正派发 OTP（修复「发码 200 但不派发」） | `registration.prime_create_account_password`（默认 `false`） | `steps._prime_create_account_password_page_enabled` docstring（2026-10-06 实测）；turb `navigate_create_account_password`、SunnyRegister `_submit_password` 均无条件执行 |
| P1-5 | signup 泳道 `authorize/continue` body 声明 `screen_hint: "signup"` 不降低成功率，且可能修复同一不派发形状 | `registration.signup_continue_screen_hint`（默认 `false`） | SunnyRegister `_authorize_email` 每次都声明；本仓 login 泳道 09-14/09-16 实测已钉住同一字段差异（`_existing_login_continue_enabled`） |
| P1-6 | POST `create_account` 前 GET `/about-you` 让页面状态与 Referer 声明一致，不降低成功率 | `registration.prime_about_you_page`（默认 `false`） | turb `navigate_about_you`（"先真实导航到 about-you"）；本仓密码页当年的同型缺口（`_prime_create_account_password_page`） |
| P1-7 | `create_account` 对 `registration_disallowed` 按 `[8,20,45]s` 有界退避（每轮重铸 Sentinel）能挽救风险窗口期内的暂时拒绝 | `registration.create_account_disallowed_backoff`（默认 `false`） | SunnyRegister `_create_account` 实测（Remail 地址特别敏感）；本仓把该错误归入终态 `account` 类（`failure_registry`） |
| P1-8 | 密码泳道 `authorize` 落 `/email-verification` 时，补发一次带 `screen_hint: "signup"` 的 `authorize/continue` 能把事务臂从 passwordless 扳回 signup（修复 P1-5 够不到的那个形状） | `registration.signup_email_verification_continue_hint`（默认 `false`） | `docs/audits/scan-2026-10-07-protocol-registration.md` §4 P0-A；`steps._signup_email_verification_continue_hint_enabled` docstring |
| P0-2b | 预检之后命中的出口级 Cloudflare 挑战，可由「同池换出口一次 + 重试同一请求」抳回（H1）；换出口仍不过时，交棒给浏览器能让账号完成注册（H2） | `registration.edge_challenge_rotate_exit`（默认 `false`）；`browser_handoff` **尚未落地**（见下） | `docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` §4；三臂 `observe` / `rotate` / `handoff` |
| P1-9 | P1-4 的 prime 导航头不完整（缺 `sec-fetch-site` / `sec-fetch-user`），不是真实顶层导航 —— 补齐后 prime 才能建立服务端真正读取的密码页状态，让 `email-otp/send` 派发验证码 | `registration.prime_navigation_headers`（默认 `false`）；两臂都开 `prime_create_account_password` | `docs/audits/scan-2026-10-07-protocol-registration.md` §4 P1-A；turb `navigate_create_account_password`（发齐两头且落点错即 `raise`） |
| P1-10 | 密码泳道首个 signin 的 `screen_hint` 从 `signup` 改为 `login_or_signup`（turb 的形状）能把事务臂从 `passwordless_signup` 扳回 signup 臂，让 `email-otp/send` 真正派发 | `registration.signin_screen_hint_login_or_signup`（默认 `false`）；**只改首个尝试的 `screen_hint`**，`authorize/continue` 行为两臂恒定 | 2026-10-07/08 两轮线上（hint@lajiao / default@fireside，不同邮箱）形状完全相同 ⇒ 出口/邮箱/continue 声明均被排除；turb `signin_openai`（`screen_hint=login_or_signup` + `prompt=login`，不发 continue） |
| P1-11 | 在 P1-10（`login_or_signup`）之上再声明 `prompt=login`（turb 的完整 signin 形状）能把事务臂从 `passwordless_signup` 扳回 signup 臂，让 `email-otp/send` 真正派发 | `registration.signin_prompt_login`（默认 `false`）；**只改首个尝试的 `prompt`**，两臂都开 `signin_screen_hint_login_or_signup` | 2026-10-07/08 线上：P1-10 治疗臂 n=2（烧过 + 全新邮箱）均为 `passwordless_signup` 且 `original_screen_hint` 跟着声明变 ⇒ 声明的 screen 被记录但不选臂；turb `signin_openai` |
| P1-12 | 在 SunnyRegister 的 signin 形状（`prompt=login` + `screen_hint=signup`）上再声明 `locale=ja-JP` 能把事务臂从 `passwordless_*` 扳回 signup 臂，让 `email-otp/send` 真正派发 | `registration.signin_locale_ja_jp`（默认 `false`）；**只加 `locale` 查询参数**，两臂都开 `signin_prompt_login`、都关 `signin_screen_hint_login_or_signup` | SunnyRegister `_start_next_auth`（唯一声明 locale 的参考）；P1-11 实测 `prompt=login` 会把臂推到 `passwordless_login` + `invalid_auth_step` |

工具：`scripts/registration_ab.py`（`plan` / `collect` / `compare`）。

> **边界**：该工具**不发起任何网络或注册流量**，只做设计打印、日志/报告归一化与对比。
> 它给出的 `verdict` 是**受控对照的判定辅助**，不是成功率结论；离线测试同样不能建立
> 线上成功率（见 [`protocol-registration.md`](protocol-registration.md) 的 Validation limits）。
> 真实运行必须由操作者在受控批次上执行。

---

## 1. 前置条件（必须全部满足）

- 同一邮箱批次：同一 provider、同一数量档、同一有效期形态。
- 同一出口池：同一 `proxy_seeds` / lane 配置；对照期间不轮换池子。
- 同一注册 driver、并发数、阶段超时与预检预算。
- 同一时间窗：Cloudflare 负载有时段性；两个 arm 尽量相邻或交叉进行。
- 已捕获每个 arm 的：
  - 运行日志 `runtime/logs/processes/<pid>/sms_tool.log`（预检行在这里）
  - 批次报告 `runtime/registration_target_<batch_id>.json`（含 `funnel`）
  - 该次运行使用的 `config.json` 快照（**操纵检查**，缺它则 `compare` 拒绝判定）

> ⚠️ **批次报告的来源（2026-10-07 扫描 §1.2 的实际阻塞点）。** 目前只有
> `--target-at200` 会写 `runtime/registration_target_<batch_id>.json`；**普通批次与桌面端
> 只把 report 经 IPC 发出，不落盘**，所以上一轮两个 arm 的 `funnel` 都是 `null`、
> `attempted` 不可算。跑 P1-8 / P1-9 时必须二选一：
> 1. 用 `--target-at200`（需 `--buy-remail-mailbox` 或 `--remail-service-mode`）跑批次；
> 2. 或从 `runtime/logs/processes/<pid>/backend_stdout.jsonl` 里取最后一条 `result` 帧的
>    `payload` 存成 JSON 文件（`{"funnel": {...}}`）后传给 `--funnel`。

### P1-8 / P1-9 / P1-10 / P1-11 / P1-12 的额外前置

- **密码泳道**：`--registration-mode password`（生产 `runtime.json` 未声明 `registration_mode`，
  默认是 `passwordless`，而这几个开关在 passwordless 泳道上是 no-op —— 机制门禁会直接判
  `manipulation_failed`）。
- **关掉 pulse**：`registration.pulse.enabled = false`。生产默认为 `true`。
  2026-10-08 起，事务臂形状（密码泳道 + dump 里 `email_verification_mode=passwordless_*`）
  已带 `arm_mismatch` 后缀，`registration_pulse._is_otp_ban_signal` 对它短路，不再换池；
  但**裸** `email_otp_send_stuck`（dump 不可读 / 臂读不出来）仍算派发侧候选信号 ⇒ 对照期
  仍应关 pulse，否则单账号 wave 会换池 + 60s 冷却，破坏「同一出口池」前提
  （2026-10-07 扫描 §5.3）。
- **主读数**：`client_auth_session_dump[after_otp_send]` 里 **`passwordless_email_otp_send_pending`
  是否消失**。不是落点 URL，也**不是** `email_verification_mode` —— 10-06/07 的 run 已经落点正确、
  事务臂也记录正确，而 10-08 的 signin 系列实测**唯一与成败完全相关**的读数就是这条挂起键
  （见 `docs/releases/release-v2026.10.08.md` 与 `docs/audits/landing-2026-10-08-signin-*.md`）。
  `email_verification_mode` 降为**次要**读数（只回答「服务端选了哪条臂」）。
- **样本**：每臂 **≥ 30** 个 attempted（`scripts/registration_ab.py` 的 `MIN_ARM_ATTEMPTED`；
  `compare` 在不足时判 `underpowered`，不得拿小样本下结论）。
- **P1-11 两臂都开 P1-10**：turb 把 `prompt=login` 与 `screen_hint=login_or_signup` 配对，
  所以 `p1-11` 的对照是 `login_or_signup` vs `login_or_signup + prompt=login`；只开
  `signin_prompt_login` 而 `signin_screen_hint_login_or_signup=false` 测的是另一个组合，不算本实验。

---

## 2. 读设计

```powershell
python scripts/registration_ab.py plan
```

输出为 JSON：每个实验的假设、开关、arms、held-constant 变量、主指标与判定规则。
阈值与手数在脚本顶部预注册（`MIN_ARM_ATTEMPTED`、`RATE_DELTA`），**看到数据后不得改**。

---

## 3. 跑与采集

以 P0-1 为例（P1-3/P1-4/P1-5 同法，仅换 `--experiment` 与 arms；P1-4 的 arm 名为 `default`/`prime`，P1-5 为 `default`/`hint`）：

```powershell
# arm A：browser（默认）
#   1) 确认 config.json 的 registration.preflight_login_page == "browser"
#   2) 跑常规注册批次，记下 batch_id 与进程日志路径
python scripts/registration_ab.py collect `
  --experiment p0-1-preflight-endpoint --arm browser `
  --log runtime/logs/processes/<pid>/sms_tool.log `
  --funnel runtime/registration_target_<batch_id>.json `
  --config config.json

# arm B：legacy
#   1) 把 config.json 改为 registration.preflight_login_page == "legacy"
#   2) 跑同规模批次
python scripts/registration_ab.py collect `
  --experiment p0-1-preflight-endpoint --arm legacy `
  --log runtime/logs/processes/<pid2>/sms_tool.log `
  --funnel runtime/registration_target_<batch_id2>.json `
  --config config.json
```

记录落在 `runtime/registration_ab/<experiment>__<arm>.json`。

> `collect` 会校验 `config.json` 与该 arm 期望的开关值一致；不一致时打印 WARNING 且记录
> `toggle_verified=false`，`compare` 将直接拒绝判定 —— 这是防止"以为改了、其实没改"的操纵检查。
>
> 🔴 **第二道操纵检查（机制行）**：`collect` 还会在 arm 的运行日志里找该实验的**独占机制行**
> （见下表）。开了开关但日志里没有那行 ⇒ 记 `mechanism_ok=false`，`compare` 一律返回
> `manipulation_failed`（**优先于** `underpowered`），**不进速率判定**。这正是 P1-5 那一轮的
> 教训：`signup_continue_screen_hint` 开着但早返回让它根本没执行，5/5 的失败被误读成
> "开关无效"。机制行必须是 `print`/`emit` 的 stdout 行，才能同时活在 `sms_tool.log` 与
> `backend_stdout.jsonl` 两种日志里。
>
> | 实验 | 机制行（日志里必须出现/必须不出现） |
> | --- | --- |
> | P1-4 | `Create account password page` |
> | P1-5 | `Signup continue declares screen_hint=signup` |
> | P1-6 | `About you page prime` |
> | P1-7 | `Create account temporarily disallowed` |
> | P1-8 | `Email verification continue hint` |
> | P1-9 | `Password page navigation headers` |
> | P1-10 | `Signin screen_hint=login_or_signup` |
> | P1-11 | `Signin prompt=login` |
> | P1-12 | `Signin locale=ja-JP` |
>
> 🔴 **机制行必须落在开关自己的分支内。** P1-5 原先登记的是 `Signup username continue`，
> 那是 `_continue_signup_username` 的**基线** print：开关开或关都会打，P1-8 的强制路径也会打。
> 结果这道门禁两头都不准 —— 对照臂只要走到该函数（落点不在密码页/邮箱验证页）就会被误判
> `manipulation_failed`，而 P1-8 开着时 P1-5 的 hint 臂又会在 P1-8 的路径上「机制通过」
> （2026-10-07 扫描 §5.2）。现改为 `if force or ...` 分支内、且 `not force` 才发的独占行；
> P1-5 的 `hold_constant` 同时钉 `registration.signup_email_verification_continue_hint=false`。
>
> P0-1 / P0-2 / P1-3 **未注册机制行**：P0-1 的两个 arm 值是端点名（字符串），无法推出机制行应在哪一侧；
> P1-3 的 bundle 行走 logger（`Sentinel password flow bundle primed`），不保证落在 stdout 日志里。
> 它们的 `mechanism_ok` 是 `null`（"未检查"），不会被判 `manipulation_failed`。
>
> 🔴 **P0-2b 故意不注册机制行**：它的机制检查是**有条件的** —— 换出口那行
> （`[Edge] Cloudflare challenge on this exit; rotated`）只有在**确实发生了挑战**时才会出现。
> 用常开的机制门禁会把「这个时间窗没发生挑战」误判成 `manipulation_failed`。
> 所以它的机制检查写在判定规则里，返回 `not_judgeable`（见下表），而不是交给机制门禁。

---

## 4. 对比

```powershell
python scripts/registration_ab.py compare `
  --arm browser=runtime/registration_ab/p0-1-preflight-endpoint__browser.json `
  --arm legacy=runtime/registration_ab/p0-1-preflight-endpoint__legacy.json
```

输出含各 arm 的 `attempted` / `registered_per_attempted` / 预检探测数 / `cloudflare_rate` /
`no_healthy_route` / 失败分类，以及 `verdict` 与 `reason`。

---

## 5. 判定规则（预注册）

- 每个 arm 的 `attempted` 必须 ≥ `MIN_ARM_ATTEMPTED`（30），否则 `underpowered`。
- 速率差阈值 `RATE_DELTA = 0.05`。

| 实验 | 判定 |
| --- | --- |
| P0-1 | `cloudflare_rate` 更低且差距 > 0.05、且 `no_healthy_route` 不更高的一方胜；否则 `inconclusive` |
| P0-2 | `observation_only`：只报每出口挑战计数；流程内换出口需**单独开关 + 单独 A/B**才能落地 |
| P1-3 | bundle 成功率不低 0.05 以上且 Sentinel 类失败不增长 → `favor_bundle`；Sentinel 类失败增长或成功率低 0.05 以上 → `keep_default_off` |
| P1-4 | prime 成功率高出 0.05 以上且 mailbox 类失败不增长 → `favor_prime`；成功率高出但 mailbox 类反增 → `favor_prime_with_red_flag`（机制与速率矛盾，须人工排查）；低 0.05 以上 → `keep_default_off`；否则 `inconclusive` |
| P1-5 | hint 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_hint`；`auth_state` 类增长（声明的 screen 与服务端事务解读冲突）→ 无论速率一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive` |
| P1-6 | prime 成功率高出 0.05 以上且 `account` 类失败不增长 → `favor_prime`；`account` 类增长（导航扰动事务）→ 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive` |
| P1-7 | backoff 成功率高出 0.05 以上 → `favor_backoff`；低 0.05 以上（拒绝是永久性的，重试只添延迟）→ `keep_default_off`；否则 `inconclusive` |
| P1-8 | hint 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_hint`；`auth_state` 类增长（补发的 POST 与服务端事务解读冲突）→ 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive`。主机制读数看 `passwordless_email_otp_send_pending` 是否从 `after_otp_send` 消失（`email_verification_mode` 只作次要读数） |
| P1-9 | `headers` 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_headers`；`auth_state` 类增长 → 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive`。主机制读数同上 |
| P1-10 | `login_or_signup` 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_login_or_signup`；`auth_state` 类增长（声明的 signin screen 与服务端事务解读冲突）→ 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive`。主机制读数同上 |
| P1-11 | `prompt_login` 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_prompt_login`；`auth_state` 类增长 → 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive`。主机制读数同上 |
| P1-12 | `locale` 成功率高出 0.05 以上且 `auth_state` 类失败不增长 → `favor_locale`；`auth_state` 类增长 → 一律 `keep_default_off`；低 0.05 以上 → `keep_default_off`；否则 `inconclusive`。主机制读数同上；两者都不动即关闭整个客户端 signin 形状家族 |
| P0-2b | 先跑 §4.1 的**无效结果判据**：任一 arm 的 `edge_challenge` 计数缺失 ⇒ `inconclusive`；三臂 `hits` 全为 0 ⇒ `not_judgeable`（窗口没测到东西，**不是**“无效果”）；`rotate` 臂命中了挑战但 `rotations == 0`（单槽池/开关未生效）⇒ `not_judgeable`（该臂是 `observe` 的副本）。有效时：`rotate` 臂出现控制臂从未有过的失败类别 ⇒ `stop_all_arms`（立即回到 `observe`）；`registered_per_attempted` 高出 0.05 以上且无新类别 ⇒ `favor_rotate`；低 0.05 以上 ⇒ `keep_default_off`；否则 `inconclusive`。**H2（handoff）单独判定，永不与 rotate 合并拍板** —— 且本轮 `handoff` 臂不可判定（`browser_handoff` 未落地，见 §6） |

---

## 6. 停止规则

- 任一 arm 出现 `no_healthy_route`：先排查出口池，**暂停对照**，不以该轮结论。
- 预检 Cloudflare 挑战率 > 50%：暂停，先修出口质量。
- 不得"看中间结果调阈值/加样本重新跑"——阈值与手数预注册，改动即为新的实验。
- 一次只改一个变量；P0-1、P1-3、P1-4、P1-5、P1-6 与 P1-7 不得同批复跑。
- 🔴 P1-4 与 P1-5 **机制上耦合**（两者都针对「发码 200 但不派发」），因此顺序必须是：先跑 P1-4（密码页 prime，作用点在 `user_register` 之前）；若 P1-4 定案且开关落地，再跑 P1-5（作用点更早，在 `authorize/continue`）。两开关同时打开跑一轮**不是**对照，是混淆变量。
- 🔴 P1-5 与 P1-8 是**同一个假设的两个作用点**：P1-5 改的是泳道本来就发的 continue，P1-8 补的是落 `/email-verification` 时的那一次（P1-5 在那个形状上不可达）。同一批只跑一个；P1-8 的两个 arm 都必须保持 `signup_continue_screen_hint=false`，因为 P1-8 的机制行自带 `screen_hint` 声明。
- 🔴 P1-10 与 P1-11 都作用在 **signin 形状**上（`screen_hint` / `prompt`），是同一假设的连续两个字段：顺序必须是先 P1-10 定案，再在 P1-10 开着的前提下跑 P1-11（`hold_constant` 已钉两臂都开 P1-10）。同一批只跑一个；两臂都不得同时打开 continue 的两个开关。
- 🔴 P1-12 接在 P1-11 之后，但按 **SunnyRegister 的形状**（`prompt=login` + `screen_hint=signup` + `locale=ja-JP`）：两臂都开 `signin_prompt_login`、都关 `signin_screen_hint_login_or_signup`，只变 `signin_locale_ja_jp`。P1-12 若也不动（速率与挂起键都不动），则客户端 signin 形状家族（screen_hint + prompt + locale）整体关闭，下一步应转向指纹/设备层或服务端路由。
- 🔴 P0-2b 的 `handoff` 臂**现在不要跑**：`registration.edge_challenge_browser_handoff` 未落地（`browser_flow` 缺“接入 cookie jar → 闯关 → 导出 → 停”的模式，见 `plan-2026-10-05` §3.4.2 的 S4b）。只跑 `observe` / `rotate` 两臂即可定 H1；`handoff` 臂会被判 `not_judgeable`。
- 🔴 P0-2b 的停止规则比速率优先：任一臂出现**新的终态失败类别**、或 `handoff` 臂把可注册地址写进死路账本（`registration_retry_guard`）⇒ **立即停全部三臂**并回到 `observe`。
- 🔴 P1-6 与 P1-7 都作用在 `create_account` 阶段：P1-6 改**请求前**（多发一次 GET），P1-7 改**失败后**（重试同一 POST）。同一批只跑其中一个；若都要定案，顺序不限，但两批的 arm 必须都保持另一开关为默认值。
- P1-4 只在**密码泳道**生效（`email_registration.registration_mode != passwordless`）；两 arm 都必须用同一 mode，且批次里不得混入 passwordless 配置。P1-7 若要复现 SunnyRegister 测到的人群，批次应以 Remail 邮箱为主。

---

## 7. 证据与复现

- 记录文件：`runtime/registration_ab/<experiment>__<arm>.json`（含 `collected_at`、
  `log_sha256`、配置快照值、原始计数）。
- 结论回流：对照完成后，把结论写回 `docs/audits/` 的 `landing-*` 或更新
  [`protocol-registration.md`](protocol-registration.md)，并说明 arm、手数、判定。

---

## 8. 明确不做

- 不改任何默认值、端点或代码。
- 不把 `verdict` 当作成功率证据。
- 不用离线单测替代线上对照。
- 不把 P0-2 的观察结果当成"流程内换出口已生效"。

---

## 9. 相关

- [`protocol-registration.md`](protocol-registration.md) — 端点/Sentinel 契约与 Validation limits
- [`registration-architecture.md`](registration-architecture.md) — 注册依赖与生命周期
- [`telemetry-and-runtime.md`](telemetry-and-runtime.md) — 日志位置与关联字段
- `docs/audits/scan-2026-10-01-protocol-registration-payment-link.md` — P0-1/P0-2/P1-3 取证

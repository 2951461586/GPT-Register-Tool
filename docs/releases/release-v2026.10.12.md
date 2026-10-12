# v2026.10.12

自 v2026.10.08 以来的收口（38 个提交）：协议注册的**默认栈**换成实测可存活的组合（新 Sentinel SDK + SO），
401 重登的两个失败原因查清并修复，以及一批「实验有效性」工具与审计记录。

> ⚠️ **默认行为变化**见「新功能 / 默认栈」与「修复问题 / 重登」；其余开关全部默认关。

## 新增功能

### 协议注册

- **默认栈**：passwordless `login_or_signup` + Sentinel SDK `20260810913b` + `user/register`/`create_account` 带 SO；
  `reference_lane` 默认关、warm-up 默认关（`tests/test_default_registration_stack.py` 钉住）。
  实测：旧栈 AT 15 分钟 2/17、30 分钟 0/18；新栈 9/9 账号 60 分钟仍活，其中 14055 在 12 小时后仍活。
  **去掉 SO 则 `create_account` 5/5 `registration_disallowed`**，SO 是必需的。样本小、单一出口、单一邮箱源，属强线索而非定论。
- **`registration.reference_lane`**（默认关）：按参考项目的 password 阶段顺序跑一条泳道（匿名预热 + providers、密码页 Sentinel bundle）。
  lane 下 OTP「pending」标记不再触发 `email_otp_send_stuck`（邮箱里码实际已到），照常轮询。
- **`registration.chatgpt_bootstrap.enabled` 的协议泳道 warm-up**（默认关）与 `scripts/at_survival_watch.py` 存活曲线工具。
- **显式 `send-otp`（浏览器式）、`device_identity_seeded_by_email`、`password_first_relogin`** 等实验开关（见 `docs/audits/`）。
- **`--check-promotion-after-registration`**：每个账号保存后立即查优惠，批末只补漏。
- **失败后重试按 `user/register` 前后区分**：`user/register` 已 200 的失败带 `|password_claimed_unfinished`，
  不再用新代理会话重试（必然 `invalid_auth_step`），重试守卫记永久死胡同。

## 修复问题

- **重登：密码探针误拒 passwordless 账号** —— 探针对无密码账号恒报 `password_form_present`，并在发验证码**之前**返回
  `existing_login_password_required`。现在仅当调用方传 `allow_passwordless=False`（注册泳道两处）才拒绝，恢复路径继续走邮箱 OTP；
  存活账号实测 OTP → MFA → session 全部 200。
- **重登：`account_deactivated` 被吞** —— `mfa/verify` 的 403 响应体此前被丢弃，只剩 `existing_login_totp_verify_failed:403`。
  现在错误串带 `:account_deactivated`，恢复链据此落终态。43 个 AT-401 的近期 iCloud 账号重登 0/43 恢复（28 确认停用），
  所以旧栈「AT 几分钟失效」应读作**账号被停用**。
- **测试污染生产库** —— `tests/conftest.py::isolated_runtime` 把 `runtime/accounts.sqlite3` 重定向到沙箱，
  此前每次测试向 `environment_ledger` 灌假行。
- **Sentinel 真实 session-observer token**、支付提取器对已失效的 `payment_pages` 路径的拒绝（Checkout 创建前）。
- 审计索引一行被写成 `???` 乱码，已按内容重写。

## 文档与工具

- `docs/audits/scan-2026-10-11-at-revocation-survival.md`：AT 存活调查（§1–13），含参考项目 401 重登策略对照与 43 账号重登实测。
- `docs/current/protocol-registration.md`：默认栈、重试规则、重登行为。
- 本轮清理：`runtime/tmp` 临时脚本与实验记录、已被替代的配置/代理备份、一次性诊断脚本与缓存均已删除（均为未跟踪、可再生数据）。

## 验证

- `pytest -q`：**6403 passed / 8 skipped**。
- WPF 由 `SmsWorkbench/build_dotnet.ps1` 编译到 `dist/net10/`；安装包由 `scripts/build_installer.ps1` 产出。

## 已知事项

- ⚠️ **请在供应商侧轮换**：本轮会话中曾把 Remail API key 与 Gmail 应用密码打印到终端输出；同时轮换 nexsms key、
  `proxy.json` 的 `phone_reuse` keys 与 rola 凭据（沿用此前版本的提醒）。
- 新栈的存活结论 n=9、单一出口（rola IN）、单一邮箱源，需要同批复测。
- 12 个近期 401 账号因库中缺 `totp_secret`（临时 launcher 写入）无法判断是否停用。
- ReMail iCloud 库存紧张；已用地址在服务端被标记，`user/register` 之后失败的地址不可再用。

# v2026.11.25

自 v2026.11.24 以来的功能迭代、稳定性升级与原生 Rust 加速扩展：新增 GPT Team 5 席位试用优惠检测与原生进程内托管支付链接提取（`team_trial`），围绕批量 AT 401 救活实战痛点系统升级重登恢复链路，并新增 Wave-3 / Wave-4 共 8 个 `rust-anything` PyO3 热点扩展（全仓达到 22 个 crate · 42 个函数），同时清理退役浏览器驱动壳模块与未用导入债务。

## 新增功能与能力升级

- **GPT Team 5 席位试用优惠检测 & 原生托管提链（`team_trial`）** ——
  - 优惠检测（`--check-promotion`）新增只读 **GPT Team 5 席位试用（`team-1-month-free`）** 资格探针，严格按 `accounts/check` 个人空间 `ChatGPT-Account-ID` 校验响应，桌面端状态列同步渲染 `可试用Team(5席)` 徽章，并在 SQLite `raw_json` 中持久化机读字段 `plus_trial_eligible`、`team_trial_eligible`、`team_trial_state`。
  - 支付链接提取模块新增第 16 种目录方式 `team_trial`（`sms_tool/pay_link/team_trial.py`，适配器 `team_trial_direct`，别名 `team` / `team_5` / `team-trial` / `team5` / `chatgpt_team`），在进程内直连 `POST /backend-api/payments/checkout` 签发 `https://pay.openai.com/c/pay/cs_live_...` 托管支付链接，**无需外部提链服务器与 CDK**；自动探测出口账单国家/币种并对 `Billing country must match request country` 自动纠偏重试一次，CLI 新增 `--team-workspace-name`、`--team-seat-quantity` 与 `--skip-team-eligibility-check`。
- **Wave-3 & Wave-4 原生 Rust 加速扩展（14 → 22 个 PyO3 Crate · 42 个热点函数）** ——
  - 本版本新增 8 个经差分与端到端跨 FFI 基准验证的 `rust-anything` crate：`payment-wire-rs`（4.6×，Attestation 提取 14.4×）、`account-identity-rs`（2.4×）、`account-recovery-rs`（2.3×，错误脱敏 4.1×）、`account-inspection-rs`（2.1×）、`pay-link-normalize-rs`（2.0×）、`playwright-proxy-rs`（2.0×）、`pp-link-helpers-rs`（1.6×）、`qualification-states-rs`（1.5×）。
  - 全仓 22 个 crate 合计 **5,609 / 5,609 录制用例 100% 一致**、**264 / 264 `rust-anything gate` 全部通过**、0 `unsafe`、0 MinGW 运行时 DLL 依赖，并保留 `RUST_ANYTHING_MODE=old` 与缺省纯 Python 自动回退；详见 [`docs/current/rust-acceleration.md`](../current/rust-acceleration.md)。

## 稳定性修复与架构清理

- **AT 401 批量重登与恢复链路升级** ——
  - `sms_tool/proxy/routing.py` 新增独立 `"relogin"` 代理泳道（`relogin_pool → liveness_pool → 健康兜底`），`recovery_batch.py` 批量并发重登与探活按账号索引轮询分散出口，避免单出口集中触发 429/403。
  - `account_recovery.py` 新增发码前传输/429/403/CSRF 自动换出口重试（绝不重复烧 OTP）、历史旧账号（未知密码 / 2FA 重定向）密码校验失败自动回退无密码 `chatgpt_email_otp` 恢复，以及新 AT 持久化前回探遇到 `status_code == 0` 传输闪断时换出口复探，防止新 AT 被瞬断丢弃。
- **退役模块清理与架构基线收紧** ——
  - 移除已退役的旧版浏览器驱动兼容壳 `sms_tool/registration_drivers/{camoufox,cloak,roxy}.py` 并清理 `sms_tool/pay_link/` 历史机械拆分残留未用导入，使模块边基线从 497 降至 **491**、未用导入基线从 127 降至 **94**，同步更新 CLI 参考、依赖图与全仓文档。

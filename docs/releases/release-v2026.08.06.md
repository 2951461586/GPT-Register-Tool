# v2026.08.06 — 协议注册解耦 + P0/P1/P2 优化入库

此版本引入三项稳定性优化（P0/P1/P2），并完成协议注册模块的职责边界解耦，为后续批量 AT-Only 注册的大规模并发奠定基础。


## 解耦 registration.py（高优先级）

`registration.py` 从 ~1600 行瘦身到 ~1470 行，作为纯粹编排入口：

- **`sms_tool/session_builder.py`**（新增）：从注册最终态拼装 canonical session JSON（35 字段 + 嵌套 `mailbox` dict）。token 优先级链、profile/device_id/paypal 字段、`created_at` 时间戳都在这里收敛。
- **`sms_tool/registration_outcome.py`**（新增）：注册结果归一化 —— 账号创建错误提炼 / 多轮 AT 稳定性探测 / `codex_oauth.require_registration_refresh_token`、`require_registration_phone_verification` 开关。
- **`sms_tool/account_2fa.py`**（新增）：TOTP 2FA 自动 enrollment（密钥生成 / totp URI 校验 / 激活轮询 / secret 入库）。
- **`registration.py`** 通过 `from .session_builder import ...` 与 `from .registration_outcome import ...` 暴露 helper；不再允许本地定义遮蔽其他模块的实现。
- **`cli.py`** 的 `run_batch` 入口改用 `batch_runner.run_batch_impl`（不再经过 `registration.run_batch` 转发），`run_email_func=run_email` 显式传入，消除循环耦合风险。


## P0：TOTP 2FA 自动登记

批量注册后可对已 AT-200 账号自动打开 TOTP 2FA：

- `account_2fa.setup_totp_2fa(account, proxy=...)` 走 `/backend-api/accounts/totp` 完整流程：状态拉取 → secret 生成 → totp URI 校验 → 激活轮询 → secret 入库。
- 注册链路 `totp_secret` / `twofa_enrolled_at` / `twofa_enroll_error` 维度加入 `accounts` 表。
- 依赖 `pyotp>=2.9.0`（见 `requirements.txt`）。


## P1：设备持久化（device_id / auth_session_logging_id）

- Storage `accounts` 表新增列：`auth_session_logging_id` / `device_id_generated_at`。
- 新增 `storage.get_device_context(email)` 查回已入库的 device 上下文，避免同账号重注册生成新的 `oai-did`。
- 注册链路在"Step 3: create account"前即落库 P1 持久化块，解决之前 `UnboundLocalError: resume_email_verification`。


## P2：阶段间 think_time 随机化

- `config.json` `registration.think_time_ms` 默认 3000；`utils.think_stage(label)` 在注册各阶段之间插入随机 sleep，避免固定的阶段性指纹。
- 配合 `curl_cffi` 的 TLS impersonation 与 `secrets.choice` CSPRNG 16 位密码，整体注册指纹更接近真实浏览器。


## 累积修复 / 误漏修补

- `UnboundLocalError: totp_secret` —— `register_loop` result dict 之前提前声明 `totp_secret = ""` / `twofa_result = {}`。
- `sql.Query: no such column: a.registration_batch_id` —— 列实际命名为 `batch_id`，改用 `.format()` 风格。
- `proxy_pool.UpstreamProxy.from_url` 现可解析 Kookeey 四段式 `host:port:user:pass` 格式上游。


## 校验

- 协议 AT-Only 10/10（ReMail 5 并发）压测通过。
- iCloud 别名邮箱 75 → 入库 37 个 AT-200 账号；38 个无法接码已清理。
- 全部 `py_compile` 通过；`import sms_tool.cli / sms_tool.registration / sms_tool.batch_runner / sms_tool.account_2fa` 无 ImportError。

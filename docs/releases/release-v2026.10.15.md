# v2026.10.15

自 v2026.10.12 以来的收口：配置与凭据文件迁出仓库根目录并支持自动迁移、ReMail 批量取号幂等键跨批次碰撞修复、
支付提取器 URL 单源化与代理优先级修正、`services/mail-otp-web` 鉴权门禁，以及桌面端 MVVM 拆分与零警告收口。

## 新增功能

- **配置与凭据迁出仓库根目录（自动迁移）** —— `config.json`、`proxy.json`、`payment.json` 与邮箱池文件默认集中存放到
  `%LOCALAPPDATA%\GPTRegisterTool\config\`；首次启动或执行 CLI 时自动从仓库根目录无损迁移旧配置，彻底避免本地凭据误入工作区（`sms_tool/paths.py`、`SmsWorkbench/LegacyConfigMigration.cs`）。
- **`services/mail-otp-web` 接口鉴权与敏感字段门禁** —— Web 查码服务支持通过 `MAIL_OTP_WEB_API_KEY`（或配置项 `api_key`）启用
  `X-API-Key` / `Bearer` 鉴权；`/api/check-mail` 默认隐藏 `refresh_token`，仅在显式开启 `expose_refresh_token=true` 时返回。
- **提交前别名邮箱守卫与开发依赖分离** —— `scripts/precommit_guard.py` 新增 `+` 别名邮箱泄露拦截；测试与静态检查工具拆至
  `requirements-dev.txt`，生产安装只需 `requirements.txt`。

## 修复问题

- **ReMail 批量取号跨批次命中历史死号（`ReMail batch returned no usable mailboxes`）** —— 相同规格（如相同 `count` 与 `domain`）的新注册批次
  此前会复用同一 `Idempotency-Key`，触发服务端重放上一批已停用邮箱并被本地死号过滤器全量拦截。根因是 `_remail_idempotency_key` 未纳入 `batch_id` 且
  `registration_batch_id` 在拉取邮箱池之后才初始化；现已将 `registration_batch_id` 前置生成并写入幂等键哈希，新批次稳定获取新库存，同批次内重试仍保持幂等。
- **Kakao 支付提取器代理池优先级倒置** —— `services/protocol-payment/kakao/kakao_extract.py` 此前优先读取通用 `proxy_pool.txt`，
  导致配置了方法级 `kakao_proxy_pool.txt` 时仍走通用出口；现已修正为优先使用 `kakao_proxy_pool.txt`。
- **协议支付提取器运行产物散落与临时凭据权限收口** —— 7 个子进程提取器（BLIK / DirectCard / iDEAL / Kakao / MoMo / PIX / TWINT）的日志、
  HTTP 调试抓包与代理状态文件不再写回源码目录，临时邮箱凭据文件统一收口并以 `0600` 权限落盘；`scripts/runtime_retention.py` 同步纳入清理范围。

## 结构性重构（无行为变更）

- **协议注册阶段拆分与输出通道统一** —— 从 `sms_tool/registration/handlers.py` 抽离 `sms_tool/registration/account_stages.py`
  承载 `create_account` 与 `user_register` 阶段；`registration/handlers.py`、`registration/otp_stages.py`、`registration/finalize.py`
  中的 32 处裸 `print` 统一迁至 `operator_output.emit`，并消除 8 处函数内延迟导入。
- **支付提取器 URL 全面单源化与公共助手复用** —— 清理 PayPal 与 `services/protocol-payment/` 共 94 处内联 URL 字面量并统一由 `endpoints.py`
  导出（`endpoints_literal_baseline.json` 归零）；iDEAL / TWINT / BLIK / Kakao 共用 `services/protocol-payment/common/payload_helpers.py`
  及公共模块，消除 BLIK 20 个重复辅助函数与 21 处未用导入。
- **WPF 桌面端 MVVM 化与零警告构建** —— 从 `MainWindow` 抽离 `TaskListViewModel`、`LogViewModel`、`PoolViewModel`、`ExportViewModel`、
  `RegisterViewModel`、`StatusBarViewModel` 与 `AccountExportJson`，点击事件改走命令绑定；合并 4 个微型分部类并清零全部 69 个 C# 编译与分析器警告。
- **测试、文档与脚本目录归档** —— `tests/` 按领域拆分为 8 个子目录，`docs/audits/` 按类型归档为 5 个子目录，顶层设计文档迁入 `docs/current/`，
  一次性历史脚本清理并保留 3 个常用手工诊断工具于 `scripts/manual/`。

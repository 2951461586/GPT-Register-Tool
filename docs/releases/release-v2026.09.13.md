# v2026.09.13

本版本完成 2026-09-12 深度扫描延后项的全部收口：两个 god file 拆解、失败词汇
单一注册表、geo 表合一（运维决策：US 统一 New York）、protocol_core 脱敏改读
policy、WPF 邮箱解析迁 Contracts、构建双胞胎合并、诊断脚本归档。

## god file 拆解（候选1）

- **`registration_handlers`（1133 行）**：检查点 payload 形状、落盘与可续跑判定
  拆至新模块 `sms_tool/registration_checkpoint.py`（数据契约与编排分离）；
  `run()` 的三条 except 臂收敛为单一 `_abort_result` 构造器；
  `registration_outcome._failure_result` 改走 `build_registration_failure_result`
  ——协议路径失败与浏览器路径共享 ADR-0008 全量契约。
- **`account_recovery`（1530 行）**：约 530 行测活批量引擎（线程池、heavy-lane
  信号量、截止线、快照持久化/清理、IPC 批事件）拆至新模块
  `accounts/recovery_batch.py`；恢复策略/终局持久化/冷却守卫留在
  `account_recovery`。引擎对策略层的引用在调用期延迟解析（导入期零循环），
  `account_recovery` 以 PEP 562 `__getattr__` 兼容再导出——既有调用方与测试
  patch 面零改动。
- **失败词汇单一注册表**：新增依赖无关的 `sms_tool/failure_registry.py`
  （8 个 FailureClass 的标记/优先序/retryable/批处理语义 + 硬停标记 + 12 条
  操作建议 + OTP 封禁签名 + 批处理派生集合）。`error_classification` 的全部
  公共元组名保留为派生视图，`classify_error` 的优先序与 internal 的 curl 码
  demotion 语义逐字保持；`registration_policy`/`registration_pulse`/
  `batch_runner` 全部改为派生。"新增一种失败要改 3-5 个文件"收敛为只改注册表。

## geo 表合一（运维决策：US=New York）

- 新增 `sms_tool/geo/profiles.py` 规范表（13 市场单一事实源）；
  `auth_headers._GEO_PROFILES` 与 `browser_fingerprint_pool.BROWSER_LOCALE_PROFILES`
  改为同源派生——新增市场只改一处，两条 lane 同时生效。
- US 时区统一为 America/New_York（浏览器侧原为 Los Angeles/PDT）；浏览器
  `COUNTRY_LOCALE_PROFILE_MAP` 每市场独立档案（CA/AU 不再别名 us/gb，时区与
  出口严格一致）；时区回退偏移改 zoneinfo 导入时求解（弃半年错一小时的 DST
  硬编码）；`TIMEZONE_NAME_BY_IANA` 补 Toronto/Sydney。
- `test_geo_profile_parity` 升级为派生一致性断言（US 例外移除）。

## 支付链路

- `services/protocol-payment/common/protocol_core.py` 脱敏改读
  `sensitive_policy.json`（与 C# SensitiveDataSanitizer、sms_tool.sanitizer
  三端同源）；policy 缺失/损坏时退回内置 LEGACY 规则，提取器绝不漏报密。
  替换串的 .NET `$1` 语法在 Python 侧翻译（与 sanitizer 同一处理）；新增
  `sanitize_log_text`（email 遮蔽）对齐 C# Redact；payload 键判定支持
  safe_key_paths/safe_key_suffixes/sensitive_key_fragments——api_key、
  license_key、session_id 等旧手搓片段表漏掉的键现随 policy 覆盖。
- 双端脱敏一致性测试自本版起真实运行（勘误：该测试在 v2026.09.12 曾落进
  `unittest.main()` 守卫后的死区从未执行；同病文件 test_proxy_entry/
  test_mailbox_graph_pure 各 1 个已一并修复）。

## WPF / 构建 / 仓库

- **邮箱凭据行解析迁 Contracts**：`TryParseMailboxExportParts`/
  `LooksMicrosoftClientId` 从 `MainWindow.Export` 私有方法迁至
  `MailboxCredentialLineParser`（rule 8：解析 window-independent）；
  clientId 回退以 `Func<string>` 工厂注入；新增 14 个 xunit 用例逐条迁移原语义。
- **构建双胞胎合并**：`scripts/build_installer_py.py` 降级为
  `build_installer.ps1`（唯一正本）的薄委托层，只转发 `--version/--skip-publish`；
  "改一边要同步另一边"的维护负担消除。
- **诊断脚本归档**：12 个 `_diag_/_bench/_rotate` 一次性脚本移入
  `scripts/diagnostics/` 本地归档（勘误：它们本就是 gitignore 吞掉的未跟踪
  文件，非入库脚本）；`delayed_import_baseline.json` 是 ratchet 基线，保留原位。

## 验证

- Python：`pytest tests/` — **3492 passed / 6 skipped**（608 subtests 含内），
  新增：failure_registry 派生一致性/分类优先序/curl demotion、geo 派生一致性、
  policy 驱动脱敏、safe_key_paths、邮箱解析 14 例。
- .NET：`dotnet test GPTRegisterTool.slnx -c Release` — **386 passed / 0 failed**。
- 守卫：architecture_scan / docs_consistency_scan / config_schema_check /
  ipc_schema_check / ruff / 三道秘密扫描全部通过。
- delayed_import 基线 400→405：recovery_batch 的 7 个延迟导入为拆解的有意
  产物（避免导入循环 + 保持测试 patch 面），其余净 -2。

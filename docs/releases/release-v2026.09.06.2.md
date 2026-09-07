# v2026.09.06.2

## 注册协作取消接通

- 新增 `sms_tool/registration_cancel.py` 生产者与生命周期管理：`cancel_scope()`
  在批次期间安装 SIGINT/SIGBREAK 处理器（第一次 Ctrl+C 请求优雅取消，第二次
  硬中断），批次结束时恢复处理器并清除全局标志，取消不再泄漏到下一个批次。
- 协议注册路径接入取消检查点：`RegistrationEmailWorkflow.run` 进入前、每个
  stage 边界、OTP 轮询窗口之间（`otp_strategy`）。取消以 `RegistrationCancelled`
  异常表达，stage 边界直通不误分类，最终产出与浏览器路径一致的
  `failure_class="cancelled"` 契约。
- 批次重试睡眠与脉冲波调度改为可中断分片睡眠（`cancellable_sleep`，2s 粒度）；
  脉冲取消会把剩余账号产出终端 cancelled 结果而不是静默丢弃。
- 测试隔离：conftest 新增 autouse fixture，在每个测试前后复位全局取消标志。
- 新增 `tests/test_registration_cancel.py` 覆盖标志语义、批次检查点、脉冲取消
  与协议路径取消契约。

## browser_flow patch 失效面根除

- `browser_flow` 各层内部调用点改为走定义模块的命名空间
  （`form_steps._fill_email(...)` 形式），不再持有 from-import 副本；
  `browser_flow/__init__.py` 只导出三个公共入口。
- 删除测试专用多副本打补丁器 `tests/browser_flow_patch.py`（170 行）及其
  AST/运行时守卫测试 `tests/test_browser_flow_patch_helper.py`（203 行）；
  相关测试改为直接 patch 源模块，打错位置会响亮失败而不是静默放过。
- `run_browser_registration` 的 `session_factory` 默认参数改为调用时解析，
  patch `external_sessions.create_browser_session` 对默认调用真正生效。
- `account_liveness` / `account_recovery` 对 browser_flow 私有符号的函数级
  from-import 同步改为模块属性访问。

## 注册结果契约统一

- 新增 `sms_tool/registration_result.py`：协议与浏览器两条路径的结果 dict
  统一由 `build_registration_result` 装配，`COMMON_RESULT_KEYS` 固化共有核心键。
- 新增 `tests/test_registration_result_contract.py`：契约单元断言 + AST 守卫
  （两个生产文件各恰好一次 builder 调用，禁止手写装配字面量回归）。

## 可观测性

- `run_browser_registration` 顶层兜底接入 logging：预期业务失败按 code 记
  warning，意外异常带 traceback 记 error（操作面文本保持脱敏）。此前该路径
  的原始 traceback 完全丢失。
- `keep_browser_open=True`（Cloak/Camoufox 本地浏览器）现在显式告警浏览器
  进程、驱动句柄与临时 profile 由操作者负责回收，不再静默泄漏。
- Roxy profile 删除三次失败现在记 warning（含 profile id 与错误类型），
  孤儿 profile 不再静默累积直到占满额度。

## 文档对账

- 三个真相源更新到拆分后实况：`architecture.md`（registration_drivers 分层
  结构与 09-06 实测行数、patch 失效面根除记录）、`directory-map.md`
  （补齐 registration 系列 10+ 模块与 browser 驱动组）、`docs/README.md`
  （落码位置规则改指 `registration_drivers/browser_flow/` 与
  `registration_handlers.py` 等）。
- 补齐 6 份 ADR：驱动注册表单一事实源、browser_flow 分层拆分（含本次
  patch 面修订）、协作取消、浏览器进程池与脉冲调度、
  email_verification_stuck 分类、注册结果契约统一。
- 修复 `PROXY_GUIDE.md` 静默失效的验证命令（原
  `python -m sms_tool.gen_pp_link --dry-run` 无入口且无 --dry-run 参数），
  改为 `python -m sms_tool.cli --test-payment-proxies`。
- 归档整理：23 份历史发布说明移入 `docs/releases/`，17 份审计/扫描/评估
  移入 `docs/audits/`；`docs/audits/README.md` 与 `docs/current/README.md`
  的冲突归档规则统一；`docs/current/registration-recovery.md` 首次纳入索引；
  `docs/README.md` 索引补齐缺失条目并统一中文条目。

## 验证

- Python：`2803 passed, 6 skipped`（本版本新增注册取消 10 项、结果契约 5 项测试）。
- .NET：`261 passed`（本版本无 C# 改动）。
- 文档一致性扫描通过（指向 `release-v2026.09.06.2.md`）。
- 发布载荷、敏感字段扫描随构建闸门执行。
- 本版本未运行注册、支付或大批量账号恢复任务。

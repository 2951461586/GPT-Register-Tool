# v2026.09.06.3

本版本包含两个批次：其一是 2026-09-06 晚间无头浏览器注册批次（`2cf9c40d`，
camoufox，21 跑 8 成 11 败）日志诊断驱动的四项修复；其二是工作区中已完成、
随本版本一并发布的**注册链路加固批次**（ADR-0009）。

## 注册链路加固（ADR-0009，显式依赖与 scoped 失效策略）

- 以每次调用绑定的不可变 `RegistrationOperations`（`registration_operations.py`）
  取代整模块工作流注入，保留 facade 导入与调用前 patch 面。
- 运行时状态分组收敛到 `registration_runtime.py`，持久化 checkpoint/结果
  schema 不变；重试决策共享（`registration_policy.py`），跨生命周期的可变
  熔断状态不再共享。
- `external_sessions.py` 拆为 `external_sessions/` 包（生命周期、profile
  配置、出口审计分离）；子进程启动环境 workaround 收敛到
  `platform_patches.py`（发布载荷闸门要求非下划线前缀命名），只作用于子进程。
- 新增 `telemetry.py`：带版本号的 correlation 字段与 test/live 来源标注；
  `mailbox_errors.py` 固化邮箱终端错误分类；`error_advice.py` 并入策略层。
- 桌面端后端退出日志级别修正（`PythonBackendClient.cs`）+ C# 回归测试。
- 新增 `scripts/registration_inventory.py` 与对应守卫测试；文档拆分：
  `docs/current/{configuration,registration-architecture,telemetry-and-runtime}.md`、
  架构快照归档至 `docs/audits/`，详见 ADR-0009。

## create_account 判定放宽（"慢"不再误判为"死"）

- 实测健康注册在 OTP 提交后的 SPA 过渡可达 ~200s，旧判定窗口把它提前判成
  `browser_email_verification_stuck`。`_post_otp_registration_state` 的推进后
  等待从 20s 放宽到 45s（仍受 stage 预算约束）。
- orchestrator 的 create_account 重试从单轮 reload 改为两轮 reload/reprobe
  循环（重试 detail 第二轮带 `_round2` 后缀），`stage_timeouts` 上限从 120s
  放宽到 180s（默认值不变，config.json 的 `create_account=90` 继续生效）。
- 新增两个端到端测试：卡死后第三轮恢复成功、永久卡死恰好两轮后按原错误码
  失败。

## 访问令牌探针假阴性修复

- 实测出现过注册全程走完（含 2FA 绑定）却因最终探针异常被判失败。
  `_browser_access_token_probe` 现在对传输异常（playwright 抛错）自动重试
  一次（2s 间隔）；HTTP 应答（4xx/5xx）是真实回答，不重试。
- 异常记录从 `type(exc).__name__`（playwright 异常类名一律是 `Error`，等于
  零信息）改为完整异常消息（脱敏 + token 替换 + 300 字符截断），重试仍失败
  时落 `access_token_probe_failed:{完整消息}`。
- 此前该场景落库为 `pending`（`at_probe_pending` + 有 token），账号本就未
  丢失，本版本减少它的发生并让它可诊断。

## register_method 落库断链修复

- `session_builder.build_session_file` 的返回白名单漏掉
  `register_method`/`session_type`/`plan_type` 三个键，导致 accounts.sqlite3
  全部 839 条账号（含协议路径）落库为 `unknown`。现在从注册结果透传，
  两路径的值（`email`/`phone`、`web` 等）如实落库。

## 失败诊断持久化

- `registration_progress.persist` 失败行现在附带结果里的
  `browser_diagnostics`（URL host/path、标题、DOM 地标计数——本就无 token
  设计，整行仍过脱敏）。此前浏览器失败只有一个错误码，无法从进度日志判断
  卡死时页面停在哪。
- 成功行与无诊断的失败行不写该键，行体积不变。

## WPF 日志面板整理（分阶段、自动换行、折叠原始 JSON）

- 新增 `BackendLogPresenter` / `BackendLogFolder`：日志面板改为展示操作者
  视角的过程叙事，机器通道（信封、事件流、结果弹窗抓取的原始行）不受影响。
  此前测活/查优惠/批量注册的终端结果信封（整包 payload JSON）会原样打进
  日志框。
- 结果信封行折叠为一行提示；多行 pretty-JSON 块缓冲解析后折叠为一行汇总
  （成功/注销/可试优惠计数，明细保留在结果弹窗与 SQLite）；`====`/`####`
  横幅条隐藏，`Batch Registration - N accounts` 与 `Account i/N` 横幅翻译为
  分阶段标题。
- 任务启动行不再显示 `python --mailbox-file ...` 完整 CLI（完整参数保留在
  任务列表与文件日志），改为 `▶ 启动：<任务名>`。
- 日志框开启自动换行（`TextWrapping="Wrap"`，横向滚动条关闭）。
- 后端补两条分阶段输出：批量注册每账号成功判定行
  `[*] Account i/N email: registered`；测活批次结束输出一行汇总
  `[*] Liveness check: ok=X/N deactivated=Y` 加失败账号逐行原因
  （结果本体仍走 `emit_result` 信封通道）。
- 新增 `BackendLogPresenterTests` 11 项 C# 测试。

## 验证

- Python：`2858 passed, 6 skipped`（含四项修复新增的 8 项测试与加固批次的
  runtime/policy/operations/telemetry/inventory/字段就绪等回归）。
- .NET：`288 passed`（含日志折叠器 11 项新测试与后端退出日志级别 6 项）。
- 文档一致性扫描通过（指向 `release-v2026.09.06.3.md`）。
- 发布载荷、敏感字段扫描随构建闸门执行。
- 本版本未运行注册、支付或大批量账号恢复任务。

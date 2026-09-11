# Documentation Index

This directory contains source-owned project documentation. Runtime files, local
configuration, generated sessions, and debug output stay outside this directory.

Directory layout (since 2026-09-06):

- `docs/*.md` — living documents only (architecture, directory map, CONTEXT,
  current feature guides). Dated snapshots are archived.
- `docs/releases/` — one immutable release note per published tag.
- `docs/audits/` — historical audit rounds, scans, and assessments.
- `docs/adr/` — accepted architecture decisions.
- `docs/current/` — current-state entry points.

## Core documents

- [Architecture and Boundaries](architecture.md) - module ownership, command
  seams, state flow, Checkout/capability and wallet contracts, PayPal return
  reconciliation, Agent Identity/SUB2API boundaries, and forbidden cross-module
  dependencies.
- [Directory Map](directory-map.md) - physical repository classification and
  where new code should be placed.
- [Project Context](CONTEXT.md) - 领域术语与模块归属规则。
- [Architecture Decisions](adr/README.md) - 已接受的架构决策。
- [Registration Recovery & Cancellation](current/registration-recovery.md) -
  注册恢复、邮箱验证卡死重试与协作取消的当前行为。
- [Registration & Proxy Architecture](registration-and-proxy-architecture.md) -
  注册与代理链路的设计与风险控制说明。
- [PayPal Zero-Due Link](paypal-zero-due-link.md) - promotion-update stage
  protocol, config keys, and region matrix search.
- [Protocol Payment Enhancement](protocol-payment-enhancement.md) - 协议支付
  提取器与终端报告契约。

## Release notes（docs/releases/，一个发布标签一份，不回写）

- [v2026.09.13 发布说明](releases/release-v2026.09.13.md) - 深度扫描延后项收口：
  双 god file 拆解与失败词汇单一注册表、geo 表合一（US=New York）、
  protocol_core 改读 policy、WPF 邮箱解析迁 Contracts、构建双胞胎合并。
- [v2026.09.12 发布说明](releases/release-v2026.09.12.md) - 深度扫描第一批收口：
  优惠状态跨语言机器契约、失败结果入约与终态判定唯一 owner、测活去双探与队列
  浏览器槽修复、代理池静默默认值清零、BA-token 与 campaign 常量单源、Rule 6
  关闭与 Rule 14 决策落档。
- [v2026.09.11 发布说明](releases/release-v2026.09.11.md) - 注册代理出口切换至
  越南（VN）、9http `geo-XX` 供应商模板与两侧地理档案补齐、PayPal 链接生成的
  代理地区重写收敛到单一范式。
- [v2026.09.10 发布说明](releases/release-v2026.09.10.md) - 协议注册依赖修复、
  内部异常分类、持久化 journal 终态和 WPF 发布验证。
- [v2026.09.08 发布说明](releases/release-v2026.09.08.md) - 桌面注册结果契约、
  阶段计时与关联修复、浏览器 admission 前置、测活队列租约，以及日志脱敏和轮转。
- [v2026.09.06.3 发布说明](releases/release-v2026.09.06.3.md) - 注册链路加固
  （ADR-0009）、create_account 判定放宽、探针假阴性修复、register_method 落库
  断链修复、失败诊断持久化。
- [v2026.09.06.2 发布说明](releases/release-v2026.09.06.2.md) - 协作取消接通、
  browser_flow patch 失效面根除、注册结果契约统一、可观测性修复与文档对账。
- [v2026.09.06.1 发布说明](releases/release-v2026.09.06.1.md) - 修复 GitHub
  Actions Windows 工作目录、路径和配置检测测试兼容性。
- [v2026.09.06 发布说明](releases/release-v2026.09.06.md) - 测活异步并发、部分
  结果快照、邮箱池恢复闸门和 WPF 超时恢复。
- [v2026.09.01 发布说明](releases/release-v2026.09.01.md) - 安全重建版本、发布
  闸门和凭据扫描。
- [v2026.08.20 发布说明](releases/release-v2026.08.20.md) - PayPal 标准
  Checkout 顺序、blocked 重建、能力预检、显式断点恢复、持久事件和代理诊断。
- [v2026.08.19 发布说明](releases/release-v2026.08.19.md) - 注册认证状态修复、
  429 冷却、代理格式兼容和桌面发布验证。
- [v2026.08.18 发布说明](releases/release-v2026.08.18.md) - 常驻 desktop-serve
  JSONL 后端、代理预检并行化、支付批次 checkpoint 节流与跨进程注册闸门。
- [v2026.08.09 发布说明](releases/release-v2026.08.09.md) - 注册 P0/P1 一致性
  与恢复、ReMail 本地化 OTP、配置/敏感数据边界、桌面任务生命周期和支付适配器整理。
- [v2026.08.06 Release Notes](releases/release-v2026.08.06.md) - protocol
  registration decoupling (session_builder / registration_outcome / account_2fa),
  P0 TOTP 2FA auto-enrollment, P1 device_id persistence, P2 think_time jitter.
- [v2026.08.04 Release Notes](releases/release-v2026.08.04.md) - payment command
  modularization, shared Checkout and wallet contracts, result semantics, and
  repository hygiene.
- [v2026.08.02 Release Notes](releases/release-v2026.08.02.md) - GoPay removal,
  focused account health modules, registration concurrency ownership, and
  desktop payment-method catalog cleanup.
- [v2026.08.01.2 Release Notes](releases/release-v2026.08.01.2.md) -
  account-pool cleanup, retired module removal, and inbox plain-text rendering.
- [v2026.07.29.1 发布说明](releases/release-v2026.07.29.1.md) - 桌面菜单对齐、
  代理路由拆分与有序动态代理回退。
- 其余历史发布说明（v2026.07.29 – v2026.08.31 未列出的标签）在
  [docs/releases/](releases/) 目录内，按文件名即版本排序，恕不逐一索引。

## Historical audits（docs/audits/，只读证据快照）

- [Historical Audits](audits/README.md) - 审计/扫描/评估快照的存放规则与解读方式。
- [Current Documentation](current/README.md) - canonical current-state entry
  points.
- [Test Layout](../tests/README.md) - 测试归属与离线测试策略。
- 中文优先说明见根目录 [README](../README.md)。

## Root-level references

- [README](../README.md) - quick start, common commands, mailbox formats, and
  operator workflow.
- [Proxy Guide](../PROXY_GUIDE.md) - local proxy setup and safe verification.

## Documentation rules

- Document the owner module before adding a new feature surface.
- Keep local paths, mailbox credentials, refresh tokens, cookies, and payment
  artifacts out of docs.
- Prefer repository-relative paths in examples.
- If a module starts calling another module's private helper, update the
  boundary document or add a public seam first.
- Keep one immutable release-note file per published tag, placed in
  `docs/releases/`. Update this index and the root READMEs to point at the
  newest release; do not rewrite historical release notes to describe current
  behavior. Older notes may be left unindexed in `docs/releases/`.
- GitHub Release 标题和正文统一使用中文；代码符号、命令、文件名和协议错误码保留原文。
  本索引条目亦统一使用中文。
- Generated test output, IDE metadata, local agent memory, installer payloads,
  and published binaries do not belong in documentation or source commits.
- 落码位置（2026-09-06 起）：
  - 浏览器注册流程改动落在 `sms_tool/registration_drivers/browser_flow/` 分层包
    （dom_fields → page_state → form_steps → flow_steps → orchestrator + session），
    驱动接入在 `sms_tool/registration_drivers/`（`base.py` 注册表 +
    `external_sessions/` 子包）；`playwright.py` 只是公共 API 薄壳。
  - 协议（HTTP 直连）注册流程改动落在 `registration_handlers.py`、
    `registration_state.py`、`otp_strategy.py`、`account_creation.py`、
    `auth_flow.py`、`auth_headers.py` 等 focused modules；`registration.py` 是
    兼容门面，只加 re-export，不加实现。
  - 注册批次、并发与取消落在 `batch_runner.py`、`registration_pulse.py`、
    `registration_concurrency.py`、`registration_cancel.py`；结果装配统一走
    `registration_result.build_registration_result`。
  - 邮箱逻辑仍在 `mailbox.py`、`providers/`、`mailbox_service.py` 等模块；
    `k12_*`、`agent_identity.py`、`sub2api_import.py` 保持原有归属。
- 新增 Agent Identity、SUB2API、导入导出逻辑时，在 `agent_identity.py`、
  `sub2api_import.py`、`session_converter.py` 中落实现，不侵入注册或支付模块。

当前专题：[注册架构](current/registration-architecture.md)、
[配置分片](current/configuration.md)、[日志与运行数据](current/telemetry-and-runtime.md)。

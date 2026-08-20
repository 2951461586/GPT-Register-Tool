# Documentation Index

This directory contains source-owned project documentation. Runtime files, local
configuration, generated sessions, and debug output stay outside this directory.

## Core documents

- [v2026.08.20 发布说明](release-v2026.08.20.md) - PayPal 标准 Checkout
  顺序、blocked 重建、能力预检、显式断点恢复、持久事件和代理诊断。
- [v2026.08.19 发布说明](release-v2026.08.19.md) - 注册认证状态修复、429
  冷却、代理格式兼容和桌面发布验证。
- [v2026.08.09 发布说明](release-v2026.08.09.md) - 注册 P0/P1 一致性与恢复、
  ReMail 本地化 OTP、配置/敏感数据边界、桌面任务生命周期和支付适配器整理。
- [Architecture and Boundaries](architecture.md) - module ownership, command
  seams, state flow, Checkout/capability and wallet contracts, PayPal return
  reconciliation, Agent Identity/SUB2API boundaries, and forbidden cross-module
  dependencies.
- [Directory Map](directory-map.md) - physical repository classification and
  where new code should be placed.
- [v2026.08.06 Release Notes](release-v2026.08.06.md) - protocol
  registration decoupling (session_builder / registration_outcome / account_2fa),
  P0 TOTP 2FA auto-enrollment, P1 device_id persistence, P2 think_time jitter.
- [v2026.08.04 Release Notes](release-v2026.08.04.md) - payment command
  modularization, shared Checkout and wallet contracts, result semantics, and
  repository hygiene.
- [v2026.08.02 Release Notes](release-v2026.08.02.md) - GoPay removal, focused
  account health modules, registration concurrency ownership, and desktop
  payment-method catalog cleanup.
- [v2026.08.01.2 Release Notes](release-v2026.08.01.2.md) - account-pool cleanup,
  retired module removal, and inbox plain-text rendering.
- [PayPal Zero-Due Link](paypal-zero-due-link.md) - promotion-update stage
  protocol, config keys, and region matrix search.
- [v2026.07.29.1 Release Notes](release-v2026.07.29.1.md) - desktop menu alignment, split proxy routing, and ordered dynamic proxy fallback.
- 中文优先说明见根目录 [README](../README.md)。

## Root-level references

- [README](../README.md) - quick start, common commands, mailbox formats, and
  operator workflow.
- [Proxy Guide](../PROXY_GUIDE.md) - local proxy setup and safe verification.
- [Test Layout](../tests/README.md) - test ownership and offline-test policy.

## Documentation rules

- Document the owner module before adding a new feature surface.
- Keep local paths, mailbox credentials, refresh tokens, cookies, and payment
  artifacts out of docs.
- Prefer repository-relative paths in examples.
- If a module starts calling another module's private helper, update the
  boundary document or add a public seam first.
- Keep one immutable release-note file per published tag. Update this index and
  the root README to point at the newest release; do not rewrite historical
  release notes to describe current behavior.
- GitHub Release 标题和正文统一使用中文；代码符号、命令、文件名和协议错误码保留原文。
- Generated test output, IDE metadata, local agent memory, installer payloads,
  and published binaries do not belong in documentation or source commits.
- 新增注册、邮箱、K12 逻辑时，优先在 `auth_flow.py`、`account_creation.py`、
  `batch_runner.py`、`mailbox_*`、`k12_*` 等 focused modules 中落实现；
  `registration.py`、`mailbox.py` 主要保留编排和兼容 wrapper。
- 新增 Agent Identity、SUB2API、导入导出逻辑时，在 `agent_identity.py`、
  `sub2api_import.py`、`session_converter.py` 中落实现，不侵入注册或支付模块。

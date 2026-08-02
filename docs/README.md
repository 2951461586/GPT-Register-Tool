﻿# Documentation Index

This directory contains source-owned project documentation. Runtime files, local
configuration, generated sessions, and debug output stay outside this directory.

## Core documents

- [Architecture and Boundaries](architecture.md) - module ownership, command
  seams, state flow, payment responsibilities, Agent Identity/SUB2API boundaries,
  and forbidden cross-module dependencies.
- [Directory Map](directory-map.md) - physical repository classification and
  where new code should be placed.
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
- 新增注册、邮箱、K12 逻辑时，优先在 `auth_flow.py`、`account_creation.py`、
  `batch_runner.py`、`mailbox_*`、`k12_*` 等 focused modules 中落实现；
  `registration.py`、`mailbox.py` 主要保留编排和兼容 wrapper。
- 新增 Agent Identity、SUB2API、导入导出逻辑时，在 `agent_identity.py`、
  `sub2api_import.py`、`session_converter.py` 中落实现，不侵入注册或支付模块。

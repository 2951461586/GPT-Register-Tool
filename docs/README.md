# Documentation Index

This directory contains source-owned project documentation. Runtime files, local
configuration, generated sessions, and debug output stay outside this directory.

## Core documents

- [Architecture and Boundaries](architecture.md) - module ownership, command
  seams, state flow, payment responsibilities, and forbidden cross-module
  dependencies.
- [Directory Map](directory-map.md) - physical repository classification and
  where new code should be placed.
- 中文优先说明见根目录 [README](../README.md) 的“中文运行要点（近期改造后）”、
  “Mailbox Inputs”、“Common Commands”和“K12”相关段落。

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
  `registration.py`、`mailbox.py`、`k12.py` 主要保留编排和兼容 wrapper。

# Current Documentation

The current architecture and operating entry points remain at the stable paths
below so existing links do not break:

- [`architecture.md`](../architecture.md)
- [`directory-map.md`](../directory-map.md)
- [`CONTEXT.md`](../CONTEXT.md)
- [`registration-recovery.md`](registration-recovery.md) - 注册恢复与协作取消的当前行为说明
- [`registration-architecture.md`](registration-architecture.md) - 注册依赖、状态、生命周期与重试
- [`protocol-registration.md`](protocol-registration.md) - 协议注册的端点、步骤、Sentinel 与落地页词汇契约
- [`registration-ab-runbook.md`](registration-ab-runbook.md) - P0-1/P0-2/P1-3 线上受控对照的操作与判定 runbook
- [`configuration.md`](configuration.md) - 配置分片优先级与迁移约定
- [`telemetry-and-runtime.md`](telemetry-and-runtime.md) - 日志关联、测试隔离与只读清理清单
- [`runtime-packaging.md`](runtime-packaging.md) - 自包含安装包里的 CPython 固定版本、校验与解释器优先级
- [`typing.md`](typing.md) - pyright 类型门禁（逐文件 ratchet）的安装、运行与修复约定
- [`randomness.md`](randomness.md) - `random` 与 `secrets` 的人工分类判据与已复核调用点
- [`account-health.md`](account-health.md) - 测活、恢复、优惠检查的副作用与代理契约
- [`mailbox.md`](mailbox.md) - 邮箱 provider 能力、配置注入、轮询和终止错误契约
- [`one-click-sms.md`](one-click-sms.md) - 一键接码供应商、租号与持久化当前契约
- [`registration-and-proxy-architecture.md`](registration-and-proxy-architecture.md) - runtime control flow and code locations for registration and the proxy lanes
- [`protocol-payment-enhancement.md`](protocol-payment-enhancement.md) - protocol payment architecture and enhancements
- [`paypal-zero-due-link.md`](paypal-zero-due-link.md) - PayPal zero-due link contract
- [`../README.md`](../README.md)

New current-state documents should be linked here first and indexed from
`docs/README.md`.

Historical audits live in `docs/audits/` (moved there on 2026-09-06; previously
they sat as `docs/audit-*.md` in this directory's parent). Release notes live in
`docs/releases/` and are indexed from `docs/README.md`.

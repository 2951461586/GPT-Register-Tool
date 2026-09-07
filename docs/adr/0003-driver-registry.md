# ADR-0003: 驱动注册表单一事实源（BROWSER_REGISTRATION_DRIVERS）

- Status: Accepted
- Date: 2026-09-05（2026-09-06 复核）

浏览器注册驱动的归一化、能力声明与校验白名单收敛到
`sms_tool/registration_drivers/base.py` 的 `BROWSER_REGISTRATION_DRIVERS`
注册表：`normalize_registration_driver`、`driver_capabilities`、
`RegistrationDriver` enum 全部从这一张表派生。此前"支持哪些驱动"散落在
cli choices、config 校验白名单、分派分支等六七处硬编码字符串里，新增驱动
要改多处且容易漏。

新驱动只改 `base.py` 一张表 + 一个 Session 实现；`registration.py` 的字符串
分派和 `config.py` 的白名单都读注册表。

## Consequences

- `driver_capabilities` 成为 orchestrator 结果契约字段 `driver_capabilities`
  的唯一来源。
- `roxy.py` / `camoufox.py` / `cloak.py` 三个 wrapper 退化为文档化示例，
  无生产调用（分派在 `registration.py`）。

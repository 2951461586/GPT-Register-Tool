# Current Documentation

## 注册链路文档分工

五篇注册文档经常被一起打开。先按问题定位，再只读那一篇；**同一事实只有一个归属**，其它地方给链接而不是复述。

| 你想确认… | 读这篇 | 它不负责 |
| --- | --- | --- |
| 阶段顺序、`RegistrationOperations` / runtime 状态组、session 归属、错误与重试策略 | [registration-architecture.md](registration-architecture.md) | 端点与请求体、代理选址 |
| 协议通道的端点、步骤顺序、Sentinel 契约、落地页词汇、edge challenge、device identity | [protocol-registration.md](protocol-registration.md) | 状态机与重试策略、浏览器侧恢复 |
| 双路径的运行时控制流、指纹与鉴权头、代理三车道与 `ProxyEntry`、驱动→池矩阵、桌面 IPC | [registration-and-proxy-architecture.md](registration-and-proxy-architecture.md) | 失败后的处置与取消、A/B 判定 |
| 失败之后怎么办：分类、协作取消、并发、可观测性 | [registration-recovery.md](registration-recovery.md) | 正常路径的端点与步骤 |
| 怎么做受控对照、开哪个开关、看什么指标判定 | [registration-ab-runbook.md](registration-ab-runbook.md) | 系统本身的行为（前四篇） |
| 账号健康 / 优惠检查 / 恢复的副作用与代理优先级 | [account-health.md](account-health.md) | 注册成功判定 |
| 谁 import 谁、哪些模块成环、哪些目录互指 | [dependency-graph.md](dependency-graph.md) | 单模块职责与边界（看 [architecture.md](../architecture.md)）；它只给测量，不给处置 |
| 邮箱 provider 能力、轮询与终止错误 | [mailbox.md](mailbox.md) | 注册流程 |
| 一键接码的供应商与租号 | [one-click-sms.md](one-click-sms.md) | 注册流程 |

**事实的唯一归属**（改一处就别改第二处）：

| 事实 | 权威文档 | 权威代码 |
| --- | --- | --- |
| 阶段顺序 / 状态组 / 重试与错误分类 | `registration-architecture.md` | `registration_handlers.py`、`registration_policy.py`、`failure_registry.py` |
| 端点、请求体、Sentinel 契约 | `protocol-registration.md` | `auth_flow/`、`sentinel/` |
| 代理串解析、车道选择、驱动→池映射 | `registration-and-proxy-architecture.md` | `proxy_entry.py`、`proxy_routing.py` |
| 取消 / 并发 / 恢复流程 | `registration-recovery.md` | `registration_cancel.py`、`registration_concurrency.py` |
| A/B 开关与判定口径 | `registration-ab-runbook.md` | `config.example.json` 中对应开关 |

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
- [`dotnet-gates.md`](dotnet-gates.md) - C# 构建告警门禁：为什么是零基线、为什么解析控制台而不是 SARIF、如何退债
- [`testing.md`](testing.md) - 测试执行：并行分片（`-n 4 --dist loadfile`）的实测收益、为什么顺序敏感分片仍是独立串行进程
- [`cli-reference.md`](cli-reference.md) - 由 argparse 生成的完整 CLI 选项表
- [`performance-budgets.md`](performance-budgets.md) - 入口延迟/RSS 的回归预算、测量口径与排查方法
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

# Historical Audits

Everything in this directory is a historical evidence snapshot: audit rounds,
scan reports, exposure assessments, credential-rotation checklists, and
one-time migration records. Individual files may describe superseded code and
must not be treated as the current architecture contract. Current ownership and
rules live in `docs/architecture.md`, `docs/directory-map.md`, and
`docs/CONTEXT.md`.

## Naming conventions for files placed here

- `audit-YYYY-MM-DD-*.md` — dated audit rounds (cleanup, architecture,
  correctness, performance, quality backlog items).
- `scan-YYYY-MM-DD-<topic>.md` — one-off scan reports.
  Exception: `scan_headless_browser_proxy_fingerprint_2026-08-29.md` keeps the
  older underscore form verbatim (it is cross-referenced from three places).
- `landing-YYYY-MM-DD-*.md` — landing records: which items from a given scan
  were actually implemented, with verification evidence.
- `*-assessment-*.md` — security/behaviour assessments.
- `credential-rotation-*.md` — credential rotation runbooks and evidence.
- `headless-browser-registration-audit.md`,
  `browser-registration-risk-control-gap-*.md` — module-specific audits, kept
  verbatim with their original banners marking superseded conclusions.
- `sentinel-account-health-migration.md` — one-time migration record.

> **Why `scan-` and not `scan_`:** the convention line below used to mandate
> `scan_*_YYYY-MM-DD.md`, but 4 of the 5 scan reports already used hyphens and
> were cross-referenced from 11 places across `docs/` and the workspace memory.
> Renaming them would have churned every reference for zero benefit, so as of
> 2026-09-09 the convention follows the files. The single underscore file is
> explicitly grandfathered above.

Release notes were archived to `docs/releases/` on 2026-09-06 and are indexed
from `docs/README.md`.

## Contents

24 files. Grouped by kind; newest first within each group.

### Scan reports（针对具体链路的横向扫描）

| File | Date | Subject |
|---|---|---|
| `scan-2026-09-09-round3-architecture-coupling-docs.md` | 09-09 | 注册 / 支付 / 测活 / 日志四个方向：架构、耦合、文档规范。P0 三条 + P1 七条已落地，P2 部分落地（§17–§20 为落地记录） |
| `scan-2026-09-08-round2-protocol-browser-registration.md` | 09-08 | 第二轮：注册链路本体（协议注册 + 无头浏览器），对照 `Regert888/gpt-auto-register`。四个 P0 |
| `scan-2026-09-08-proxy-fingerprint-pool-refactor.md` | 09-08 | 第一轮：代理池 / 指纹池重构，geo 解析三份实现、指纹池裸 round-robin、代理健康状态两套 |
| `scan-2026-09-07-module-optimization-review.md` | 09-07 | 协议注册 / 无头浏览器注册 / 查优惠 / 账号测活四个模块的优化空间；落地见 `landing-2026-09-07-items-1-9.md` |
| `scan_headless_browser_proxy_fingerprint_2026-08-29.md` | 08-29 | 首轮：无头浏览器注册 / 代理池 / 指纹池基线（浏览器进程池名不副实、代理子系统拓扑） |

### Audit rounds（按轮次编号的全仓审计）

| File | Date | Subject |
|---|---|---|
| `audit-2026-09-02-round6-architecture-hygiene.md` | 09-02 | 第六轮：可观测性 / 契约一致性 / 死代码清理 |
| `audit-2026-09-02-round5-tests-security.md` | 09-02 | 第五轮分组：测试质量 + 安全卫生 + 依赖与仓库工程 |
| `audit-2026-09-02-round5-summary.md` | 09-02 | 第五轮总纲：代码质量 / 解耦 / C# 剩余面 / 测试安全 / 文档 |
| `audit-2026-09-02-round5-python-quality.md` | 09-02 | 第五轮分组：Python 侧代码级质量（克隆函数 / 超大函数 / 硬编码 URL） |
| `audit-2026-09-02-round5-decoupling.md` | 09-02 | 第五轮分组：耦合与解耦机会 |
| `audit-2026-09-02-round5-csharp-wpf.md` | 09-02 | 第五轮分组：C#/WPF 侧（`SmsWorkbench` / `Contracts` / 测试） |
| `audit-2026-09-01-round4-performance-backlog.md` | 09-01 | 第四轮：性能 / 前端层 / 数据层 / IPC / 依赖 |
| `audit-2026-08-31-round3-correctness-backlog.md` | 08-31 | 第三轮：正确性 / 安全出口 / 资源生命周期 / 交付工程 |
| `audit-2026-08-31-round2-architecture-backlog.md` | 08-31 | 第二轮：架构 / 并发 / 文档 / 测试有效性 |
| `audit-2026-08-31-cleanup-backlog.md` | 08-31 | 第一轮：待清理 / 待优化清单（首批 5 项已全部完成，净减 299 行） |

### Module-specific audits（单模块专项）

| File | Date | Subject |
|---|---|---|
| `headless-browser-registration-audit.md` | 08-29 | 无头浏览器注册：能力盘点与缺口分析（只读基线） |
| `browser-registration-risk-control-gap-2026-08-30.md` | 08-30 | 指纹浏览器注册风控缺口，对照 `turb-gpt-free-register` / `aBaiFreeGPT` |

### Security & credentials

| File | Date | Subject |
|---|---|---|
| `security-exposure-assessment-2026-08-31.md` | 08-31 | 凭据与 PII 暴露面评估：两起泄漏的**实际**暴露面核实（CI 运行记录 / GitHub API / fork 副本） |
| `credential-rotation-2026-09-01.md` | 09-01 | 凭据轮换清单与证据：凭据本体从未进过 GitHub，发布包确认不含 PII |

### Snapshots & one-off evidence

| File | Date | Subject |
|---|---|---|
| `landing-2026-09-07-items-1-9.md` | 09-07 | 09-07 审计清单 1→9 的落地记录（纯函数下沉，而非把 import 挪进函数） |
| `architecture-before-registration-hardening-2026-09-06.md` | 09-06 | 文档拆分时保留的架构快照；其中的行号与文件尺寸**不是**当前实现的声明 |
| `sentinel-account-health-migration.md` | — | Sentinel / account-health 迁移记录：注册 / 恢复 / 手机注册 / 结账审批改为发 flow-bound token |
| `browser_profiles_manifest_20260909.txt` | 09-09 | 一次性证据：`runtime/browser_profiles/camoufox` 清除前的 profile 清单（目录名 / 文件数 / 字节数 / mtime） |

### Not in this directory

- Release notes → `docs/releases/`（2026-09-06 归档，索引在 `docs/README.md`）
- 当前架构契约 → `docs/architecture.md`、`docs/directory-map.md`、`docs/CONTEXT.md`

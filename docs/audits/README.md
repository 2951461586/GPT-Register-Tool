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
- `plan-YYYY-MM-DD-*.md` — 立项 / 方案文档：**尚未拍板或尚未落地**的提案，
  含待验假设、实验设计、明确否决项。与 `landing-*` 相反：`plan-*` 是**提案**，
  `landing-*` 是**已完成的落地记录**。拍板并落地后，结论应回流到
  `landing-*` 或对应模块文档，`plan-*` 保留为历史快照。
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

44 files. Grouped by kind; newest first within each group.

> Recount: `ls docs/audits/*.md docs/audits/*.txt | wc -l` — this number is
> pinned by `tests/test_audits_readme_index.py`, which fails when it and the
> directory disagree. It read "31 files" while the directory held 39 for weeks,
> and no gate could see it: `docs_consistency_scan.py` checks line-number
> pointers, symbol tables, release pointers and `sms_tool/providers/*.py`
> paths — never a document's self-reported entry count.
### Scan reports（针对具体链路的横向扫描）

| File | Date | Subject |
|---|---|---|
| `scan-2026-09-23-sms-provider-key-crosswrite.md` | 09-23 | 一键接码四家供应商「三家同时报 `401` / `NOKEY` / `403`」归因为**两个独立原因**：①设置界面的供应商**下拉框与输入框是两个独立控件**，切换下拉框不刷新 provider 作用域字段 ⇒ `Save()` 把打开时那家的 `api_key` / `endpoint` / `sms_timeout` 写进**新选中**的那家（`proxy.json` 3949 B 那份里四家 `api_key` 逐字相同、三家 `endpoint` 被写成 `""`、`sms_timeout` 全变 60）；②`dist/net10` 二进制是 11:25 构建，早于两个修复提交 ⇒ nexsms 走 sms-activate 协议族直接 403。**关键判据：差异 100% 落在设置界面拥有的字段上**（用「最后一次已知良好备份 + geo 脚本复现 + 逐叶子比对」证明）。修复 = 数据恢复（`runtime/tmp/restore_provider_keys_20260923.py`，按 provider 切块 + 块内字节级替换 + **按 start 降序**拼回，6 道校验全过才落盘）· `SettingsService.ReadValue` 显式 provider 重载 + `ReloadProviderScopedFields` 订阅 · `--doctor` 新增**离线**撞车判据（两家以上解析后 key 逐字相同即告警，从不打印密钥）· 排障文档 §16。落地与验证见 `landing-2026-09-23-config-shard-writer-alignment.md` 同日的后续三项 |
| `scan-2026-09-23-six-modules-architecture-coupling-docs.md` | 09-23 | 六个功能模块（协议注册 / 查优惠 / 账号测活 / 一键接码 / `config.json` / 协议支付面板）逐模块体检：门禁 10 项全绿。**头条是交付风险而非代码缺陷** —— 工作区领先 HEAD 43 改 / 2 删 / 19 新增，其中 4 个生产模块（`sms_providers.py` / `mailbox_pool_writer.py` / `page_truth.py` / `browser_profile_reclaim.py`）**在 HEAD 里不存在**，而 **10 个已跟踪文件已在 import 它们** ⇒ `git add -u` 产出 `ModuleNotFoundError` 的破损提交，且 4 个守卫测试本身也未跟踪（守卫与被守代码被拆散）。另四条：`docs/directory-map.md` **内部自相矛盾**（`:11` 用旧名 SMSBower / `:179` 用新名，C# 类名在文档里**零门禁**）；支付域用 5 种命名形态铺了 40 个模块且 `sms_provider.py` vs `sms_providers.py`（**该对已按本轮结论改名消除：`sms_provider.py` → `sms_provider_adapter.py`，见报告 §5.1**）、`pay_link/` vs `paypal_link/` 只差一字母；把目录表自述的「60 个未归属模块」**首次按族拆开**（mailbox 9 · accounts 7 · payment 6 · paypal 4 · proxy 4 · sentinel 4 · paypal_link 3 · registration 2，含 Rule 13 的 `proxy_entry.py` 与账号测活两个模块）；未读配置键判定**只扫 Python** ⇒ `runtime.python_path` 被 C# 读 3 处却报「设了没用」（1/61 假阳性）。**结论：协议注册 / 查优惠 / 账号测活三模块无新增结构性问题**；参考仓库 `gpt-register-pro` **无新提交**（HEAD 仍 `7b2304f` / 09-19），昨日对标结论继续有效。**本轮 7 项建议已全部落地**（含 `sms_provider.py` → `sms_provider_adapter.py` 改名与第三方凭据脱敏），提交 `083e1d5` |
| `scan-2026-09-22-architecture-coupling-dirs-docs.md` | 09-22 | 四轴只读扫描（耦合 / 架构 / 目录 / 文档）：门禁 9 项全绿、**0 个 import-time 环**（8 组静态互指边全由懒加载或门面断开）；A1 `account_recovery` 兼容壳只被 6 文件消费却逼出 `recovery_batch` 31 处函数体内导入（删壳即消 SCC-8）；A2 `docs/audits/README.md` 自述「31 files」实际 40；A3 13 个库模块从未入文档（`cli_parsers/`、`geo/`、`pay_link/` 三整包）；A4 `upi_link.py` 2914 行不在任何拆分计划内；A6 初判「C# 第 6 轮死成员 10/16 项仍在」**在落地阶段被自己推翻**（第 6 轮当天已全删，只剩墓碑注释 —— 数命中次数不读命中行的误报）。**含对第 6 轮两处结论的纠正**（`store/connection` 反向依赖属有意设计；`account_deatived` 有跨语言契约测试钉住，非死分支）与 8 条探针误报坑清单 |
| `scan-2026-09-22-gpt-register-pro-benchmark.md` | 09-22 | 对标 `cxqc168-wq/gpt-register-pro`（Node/Electron 浏览器路线，**AGPL-3.0** ⇒ 只借机制不搬代码）：§2 对方公开仓库**正在泄露**三家接码平台真实 API Key 与账号明文密码（`.gitignore` 配了也挡不住，密钥在源码里）；§3 十项可借鉴清单；§4 四个不要抄（`writeLock` 纯进程内不可重入 / `licenseGuard` 整套失效 / 零测试 / 仓库卫生）。**§7 为落地记录**：① 邮箱池 OAuth RT 轮换回写（本仓确认缺失，`mailbox_pool_writer.py`）· ② 卡密占位符预校验 · ③ **新建** `docs/TROUBLESHOOTING.md`（本仓原本没有任何排错文档）· ④ 截图配 DOM 真值 · ⑤ 未捕获异常原本**不进**运行日志（已补两级钩子）· ⑥ 浏览器 profile 占用原本只能拿到不含持有者的报错 · ⑦ **前置条件实测为「不可判」**（供应商答复被三重销毁）⇒ 只补观测不实现 · ⑧ Python 侧达标、C# 驱动枚举是未受守卫漂移面（已补平价测试）· ⑨ **建议不做**（legacy 分支是 C# 侧承重墙：C# 测试 30 处只写 `config.json`，Python 测试 32 处写分片）· ⑩ 发布前扫描原实现**三类整类漏检**（含名字恰好等于凭据词的变量三模式全漏） |
| `scan-2026-09-16-icloud2-api798-false-negative.md` | 09-16 | `api798.com` 的「0/33」是**测量假象**而非渠道故障：4 段格式的尾巴被拼进 `auth_code` ⇒ 403「授权码无效」；同日同出口实测干净 URL 回 200、带尾巴回 403、`GARBAGE` 对照回同样的 403 ⇒ 渠道活着，33 个邮箱曾被 `icloud2_remove403.py` 误删。含 50 条按 2 段形态导入的处置、残留风险与回收路径 |
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
| `scan-2026-09-12-localflow-register-reference.md` | 09-12 | 协议注册专项扫描：P1 rola 模板 / P2 注册 lane 重试覆盖 / P3 代理池 http 上游不兼容；附 `LocalFlow Register` 参考评估 |
| `scan-2026-09-16-protocol-registration-optimization.md` | 09-16 | 协议注册优化扫描：P0-1 取码前止损（B 已落地 / A 只落埋点）/ P0-2 脉冲 ban 判据分辨化（已落地）/ P1-1 密码优先立项 / P1-2 恢复窗口不匹配（已落地）/ P2-1 降噪保留键名（已落地）；含 wave 时间线、落地记录 §六 与坑清单 |
| `scan-2026-09-16-invalid-auth-step-silent-drop.md` | 09-16 | `invalid_auth_step` 静默丢弃取证：两层根因（服务端把「地址已存在」提前到 `login_or_signup` 路由落 `/log-in/password`；代码拼 `err_msg` 而非 `err_code` ⇒ 落 `unknown`，既不重试也不记掉号）；含 19 个历史批次落点表、已排除假设（出口 / `passwordless_disabled` / 本地已注册）、P0–P3 修复建议与可复现命令 |
| `scan-2026-09-16-partial-registered-chain-break.md` | 09-16 | 最新一轮 19/19「半注册」链路断点定位：探针在 `existing_account` 路径上**确定性失效**（跳过 `authorize/continue` ⇒ `continue_url` 恒空 ⇒ 探针恒 `None` ⇒ `allow_passwordless=False` 把兜底变终局，**0 例外**）；与 abai 三条差距（照发 continue / `screen_hint:"login"` / fallback 默认值不为空）；根因=选源（`+oai02` 条目被占；同主地址 `+oai01` 20/20 成功、`22chariot` 三变体全成功 ⇒ OpenAI 不归一化 `+tag`）；P0-1 选源 / P0-2 探针（已落地）/ P1 观测 |
| `scan-2026-09-18-sentinel-runner-hash-mismatch.md` | 09-18 | 最新一轮协议注册 **126/126 全灭**根因：`sentinel-runner.js` 被 **CRLF→LF**（内容逐字未变，57592→56154 B，差=行数）⇒ SHA256 变而 `bundle.py` 的 `RUNNER_SHA256` 未同步 ⇒ `validate_runtime_bundle` 每次抛 `SentinelBundleError` ⇒ **静默降级** legacy ⇒ 走到 `create_account` 的 **42/42 必死**（`sentinel_legacy_incomplete`）；`git` 因 `autocrlf` **完全看不见**、`tests/test_sentinel_runner.py` **早已是红的**、`line_ending_guard` 只防「单文件内混用」防不住「整文件统一转 LF」；P0（`_digest` 归一化 + 常量同步）/ P1（降级失败暴露原因 + 新增 `sentinel_asset_guard` 第 4 道门禁）已落地；另记 `auth_flow` 属 `auth` 组 cap=1 致中位排队 64.5 s 的独立吞吐问题 |

| `scan-2026-09-18-latest-protocol-batch-diagnosis.md` | 09-18 | 最新一轮协议注册（PID 29896，20:40–20:53）**4/10 成功**归因：失败 = `email_otp_send_stuck`×4（**地址属性**，跨批次/跨出口/隔数小时连续撞同一形状 ⇒ 止损正确）+ `registration_internal_error:…curl: (56)`×2（**代码缺陷**：`CURL_TRANSPORT_MARKERS` 从 network 清单派生，漏登记 `(52)/(56)/(18)` ⇒ 无法把 `internal` 降级成 `network` ⇒ 不重试/不进守卫/不触发熔断）；含逐账号去向表、与 35192（85 账号/76%/20 条池）的**不可单一归因**说明、根因链；P0-1（curl 标记）/ P0-2（隔离加 TTL，方案 B）**均已落地**并附线上账本验证 |
| `scan-2026-09-19-selected-22-vs-reported-3of6.md` | 09-19 | 「勾选 22 个邮箱，报告却是 3/6」口径归因：**22 与 3/6 分属两次运行**（20:40 `--count 22` → 4/10；23:58 `--count 12` → 3/6，后者的 12 条是前者 22 条的**真子集**，0 条新增）。报告分母 = `effective_count`（穿过两道过滤后的存活数），既非勾选数也非 WPF 写文件的行数。三段过滤逐条对账 `4 已注册 + 6 半注册 + 6 隔离 + 6 尝试 = 22`；定位**静默断链**在 WPF `IsUnregisteredMailboxRowAsync`（只把存活数塞进对话框、丢掉的 10 条一字不说）；附 `blocked_email_states` 三种封锁（`dead_end` 永久 / `otp_pending_quarantine` 有 TTL / `cooldown` 仅 300s）⇒ 同一批勾选在不同时刻尝试集合本就不同；含 6 条隔离的自愈时间表（09-19 17:50–20:48 自然放行） |
| `scan-2026-09-20-icloud-no-trial-2fa-census.md` | 09-20 | iCloud「无试用优惠」普查：1718 个 iCloud 账号中 `Free·无优惠` **1684**，其中已设 2FA **517** / 未设 **1167**。三项口径确认：①2FA 真源 = `accounts.totp_secret` 非空（桌面端也认 `totp_present`/`totp_enrolled`/`has_totp`，但**全库零出现**）②「未设置」绝大多数是 WPF 默认 `--no-2fa` 的**主动跳过**（`registration_finalize.py:116-118` 直接 return，**连 `twofa_enroll_error` 都不写**）⇒ 1167 中仅 19 条有失败记录（12 条 `pyotp_missing`）③优惠状态是**探测时快照**不是实时（跨 08-30→09-20，306 行已 3 周） |
| `scan-2026-09-21-batch-2fa-enrollment-blocked.md` | 09-21 | 批量 2FA 阻塞诊断（**已结案**）：`mfa/enroll` 的 `recent_auth_required` 硬拦四条通路（A 复用 AT / B cookie 重放 / C 邮箱 OTP 重登 / D 密码登录）。🔴 根因 = `_send_existing_login_otp` **POST** `/api/accounts/email-otp/send`（服务端要求 **GET**，与 `registration_handlers.user_register` 属同一类铁律）⇒ 武装出 `login_challenge` 而非 `passwordless_login` 挑战，`email-otp/validate` **恒定 409 invalid_state**（批量 5/5、定点探针 4/4），事后 `client_auth_session_dump` 转 404。判据链：换垃圾码 → `401 wrong_email_otp_code`（会话确证有效）；静置 20s / 真读邮箱 / 出口 IP 不变 → 仍 401；**只有提交真码**才翻成 409。改 GET 后 validate 200 + NextAuth callback，批量 2/2 `enrolled`。附我造成的一次 `raw_json` 回归（`upsert_account` 白名单无 `promotion*`）及修复 |
| `scan-2026-09-21-icloud-2fa-backfill-run.md` | 09-21 | iCloud 1167 账号 2FA 回填执行记录：`scripts/batch_enable_2fa.py` 端到端跑批的产出、失败分类与导出对账 |
| `scan-2026-09-21-payment-eligibility-in-promotion-column.md` | 09-21 | 查优惠阶段支付资格检测扫描（**已落地**）：结论 = **能力早已存在（`payment_capability.payment_method_capability_probe`，走到 Checkout + Stripe init 即停，零副作用）但三条链路互不相通** —— 链路 A 查优惠只打 `GET accounts/check`（结构上拿不到 `payment_method_types`）· 链路 B 只藏在「协议支付」对话框的 `--payment-probe-only` · 链路 C 批量支付只落脱敏报告、不回写 `raw_json`。逐方式：card/upi/momo 已有 catalog 条目；**link / apple_pay 全仓 0 命中**，需实测确认 Stripe token 名。落地 = 新增 `accounts/account_payment_eligibility.py`（薄封装，避免 `account_promotion` 直依 5 个 provider）+ 查优惠阶段接线 + 徽章并入「优惠状态」列（新字段 `promotion_display`）。**用户拍板否决了原报告的三条倾向**：并入拼接（非新增列）/ 全量探测 / **默认开**（`--no-payment-eligibility` 作逃生阀）。附 6 条坑：`safe_snapshot()` 是**唯一**键级关卡（勘误：`desktop_read._PUBLIC_COLUMNS` 是数据库列白名单，不约束 `raw_json` 键）· **出口地区错配 = 静默错误**（US 出口探 IN 的 UPI 会拿到 US 方式表，判 ineligible 看着像「不支持」）· `promotion_marker_is_stale` 过期语义必须同步清空徽章 · 徽章组合单一 owner 放在无依赖的 `promotion_states.py`（否则 `desktop_read` 被迫导入支付目录）· `billing_currency` 不能改 `paypal_extract.CURRENCY_MAP`（会静默改其 EUR 回落）· 调 `payment_capability` 必须用**模块限定访问**（`from .. import payment_capability`），用 `from X import f` 会让 `patch("sms_tool.payment_capability.f")` 失效且顶层导入不涨 delayed-import ratchet |

### Plans（立项 / 方案，**待拍板**，未落地生产默认行为）

| File | Date | Subject |
|---|---|---|
| `plan-2026-09-17-protocol-payment-extractor-consolidation.md` | 09-17 | 协议支付提取器公共骨架抽取立项：ideal/twint **81%（108/134）顶层函数在标识符归一化后 AST 完全相同**、逐行 91.4%，但 35 个函数为**非等价差异**且在真实支付链路 ⇒ 建议三阶段（先零风险的度量基建 + 重复度 ratchet，再抽纯工具层，成对函数参数化单独拍板）；含 5 条明确否决项（**反对**合并为单一参数化引擎）、4 个待答开放问题（首要是 blik 独有的 32 个代理选址函数属"特有需求"还是"ideal/twint 落后版本"） |
| `plan-2026-09-16-password-first-registration.md` | 09-16 | 密码优先注册立项：唯一待验假设 H1（POST `user/register` 后**采纳响应**能否让 `validate` 不再 409）、灰度 S0–S3 设计、与已删探针的差异、回滚 |
| `plan-2026-09-16-partial-account-protocol-login.md` | 09-16 | 半注册账号协议登录方案：分「有密码 / 无密码」两类结论；aBai 与 turb **都不处理** `user_already_exists`；方案 A（预防）/ B（止损分流）/ C（`/about-you` 页面取证，唯一未试路线）/ D（明确否决） |

### Security & credentials

| File | Date | Subject |
|---|---|---|
| `security-exposure-assessment-2026-08-31.md` | 08-31 | 凭据与 PII 暴露面评估：两起泄漏的**实际**暴露面核实（CI 运行记录 / GitHub API / fork 副本） |
| `credential-rotation-2026-09-01.md` | 09-01 | 凭据轮换清单与证据：凭据本体从未进过 GitHub，发布包确认不含 PII |

### Snapshots & one-off evidence

| File | Date | Subject |
|---|---|---|
| `landing-2026-09-23-config-shard-writer-alignment.md` | 09-23 | 配置分片写入器对齐三项：①归档并删除死 `config.json`（386 叶子 / **0 独有** / 88 值不同；sha256 核对后才删；连带修 `default_config_path().parent` 在根文件缺失时漂移到 `sms_tool/` 导致 doctor 报错目录）②两侧原子写与空片语义对齐（C# 补 `.bak` + fsync，空片由**删文件**改为写 `{}`，否则「三片删光 ⇒ `AnyShardExists()` 假 ⇒ legacy 分支复活旧配置」）③写入按内容变化裁剪（分片 mtime 重新成为审计信号）。**含本轮新发现**：C# 默认 `JavaScriptEncoder` 把 `+` / CJK 转义成 `\u002B` / `\u667A\u5229`，与 Python `ensure_ascii=False` 不一致 ⇒ 两侧互判对方文件为「已变更」；另记一条坑：引入裁剪会让「写两次」的测试变成**同义反复**（`os.replace` 不再被调用） |
| `landing-2026-09-07-items-1-9.md` | 09-07 | 09-07 审计清单 1→9 的落地记录（纯函数下沉，而非把 import 挪进函数） |
| `architecture-before-registration-hardening-2026-09-06.md` | 09-06 | 文档拆分时保留的架构快照；其中的行号与文件尺寸**不是**当前实现的声明 |
| `sentinel-account-health-migration.md` | — | Sentinel / account-health 迁移记录：注册 / 恢复 / 手机注册 / 结账审批改为发 flow-bound token |
| `browser_profiles_manifest_20260909.txt` | 09-09 | 一次性证据：`runtime/browser_profiles/camoufox` 清除前的 profile 清单（目录名 / 文件数 / 字节数 / mtime） |

### Not in this directory

- Release notes → `docs/releases/`（2026-09-06 归档，索引在 `docs/README.md`）
- 当前架构契约 → `docs/architecture.md`、`docs/directory-map.md`、`docs/CONTEXT.md`

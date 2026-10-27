# 扫描：审查图刷新 / 支付提取器公共骨架 / 死码候选核实（2026-10-28）

- **日期**：2026-10-28
- **HEAD**：`9a87ef3`（`feat(payment,proxy): contract-scoped checkout reuse, rotation fix, credential masking, new logo`）
- **范围**：pi-lens 审查图的覆盖率与循环口径、`services/protocol-payment/` 七个提取器的重复度现状与下一批抽取建议、pi-lens 报出的 15 条「死码」候选
- **不在范围**：任何生产行为变更。本文只记录测量、口径澄清与核实结论；§2 的抽取建议**未落地**（提案）
- **相关**：`docs/architecture.md` Rule 10 / Rule 13 / Rule 19、`docs/directory-map.md`、
  `docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md`、
  `docs/audits/plans/plan-2026-09-28-stage2-replay.md`

---

## 0. 结论先行

1. **图没有「丢子系统」**。`project_report` 的「61% 覆盖率」是**分母口径**问题：分母把 `tests/`（464）与 `scripts/`（62）也算进项目文件，而这两棵树被策略排除在审查图之外。真正的缺口只有 11 个 `.xaml`（不支持的语言）与 5 个 `sms_tool/upi_link/_vendor/`（被 `.pi-lens.json` ignore）。`sms_tool/` 自身覆盖 323/330（缺的 5 个全在 `_vendor/`）。
2. **重构后图确实变了**：全量重建把边数从 63,787 拉到 **64,395**（+608），stamp 从 `aed6ea9` 进到 `9a87ef3`。但 pi-lens MCP 进程里仍是旧快照，`project_report` 的 stale 警告会一直显示到重启该进程为止——**警告本身不代表磁盘快照旧**。
3. **循环是「一个环」，不是「散落的环」**：19 个包构成单一强连通分量，`sms_tool/kernel` 是唯一无环核心包，`services/` 整棵树无环。包间双向边 28 对。
4. **提取器重复度已从 81% 降到「局部」**：`ideal:twint` 结构性重复函数 **26**（基线棘轮值），`ideal:blik` 13，`twint:blik` 10，`momo:kakao` 0。三份文件里仍有 **10 个函数三边逐字节等价**，其中 `create_checkout` 单函数 109/109/102 行——这是下一批抽取唯一值得先做的目标。
5. **pi-lens 的 15 条死码候选 15/15 全是误报**，且根因可解释：提取器靠运行时 `sys.path.insert(PROTOCOL_ROOT)` 导入 `common.*`、由 `subprocess` 启动，静态图解析不到（图里 `imports` 边解析率 0%）。本次也确认 `vulture` 未安装（`dead-code-python.json` 写着 `skipped`），那 15 条**只有可达性一个信号**，不足以支撑删除。
6. **全量测试 7153 passed / 10 skipped / 0 failed**（587.62 s，含 1333 subtests）。

---

## 1. 审查图刷新与复核

### 1.1 执行与判据

```powershell
# 强制全量重建（先备份并移走快照，否则 CLI 判定 mode=cached 直接复用）
pi-lens build-graph --cwd .
```

`build-graph` 的 `mode=cached` 只表示**内容签名一致**（文件 mtime+size 签名未变），不表示 git stamp 新：
HEAD 从 `aed6ea9` 前进到 `9a87ef3` 时工作树签名没变，因此第一次运行输出「snapshot already current」并保留旧 stamp
（`review-graph.log` 里那条 `snapshot_read_drifted / reason=head_moved` 就是同一现象的记录）。

| 指标 | 重建前（快照 11:22Z） | 重建后（16:00Z） |
| --- | --- | --- |
| `gitStamp.headCommit` | `aed6ea9`（落后 HEAD） | `9a87ef32`（= HEAD） |
| files | 456 | 456（+1 个 file 节点，共 457 节点含 1 个非源文件） |
| nodes | 11,408 | 11,402 |
| edges | 63,787 | **64,395** |
| 持久化 | partial=false | partial=false |

边数 +608 而节点数 −6 的组合说明：重建丢掉了一些旧的符号节点，同时补齐了更多引用边（`references` 48,957 / `imports` 3,352 / `calls` 620 / `defines`+`contains` 各 5,733）。

> **MCP 进程内的快照不会自动跟随磁盘**。本轮 `project_report` 直到最后仍打印「built at `aed6ea9`，stale 278m」——那是服务进程内存里的旧副本。要让它消失需重启 pi-lens 服务；磁盘快照已经是 `9a87ef32`。

### 1.2 覆盖率口径（为什么 61% 不是「丢了 39% 的代码」）

按图的 `signature` 逐文件比对磁盘源码（排除 `.venv` / `obj` / `bin` / `dist` / `runtime` 等）：

| 树 | 磁盘源文件 | 在图中 | 缺失 |
| --- | --- | --- | --- |
| `sms_tool/` | 327 | 323 | 5（`upi_link/_vendor/`，被 `.pi-lens.json` ignore）+ 其余为 `__pycache__` |
| `SmsWorkbench/` | 89 | 78 | **11 个 `.xaml`**（XAML 不在图的语言集内） |
| `SmsWorkbench.Contracts/` | 18 | 18 | 0 |
| `services/` | 34 | 34 | 0 |
| 根目录脚本 | 5 | 3 | 0（2 个是 `.ps1`/其他） |
| `tests/` | 464 | 0 | 464 —— **策略排除**（图只覆盖产品代码） |
| `scripts/` | 62 | 0 | 62 —— **策略排除**（操作脚本） |

即：**产品代码零缺口**，「61%」= `456 / 748`，而 748 把 `tests/`+`scripts/` 算进了分母。唯一值得记录的盲区是 `.xaml`——WPF 的布局与事件绑定连线（`Clicked=`、`x:Name`）不在图内，所以「谁引用了某个 `MainWindow.*.cs` 分部」只能靠 C# 侧或人工判断。

### 1.3 循环与分层（在重建后的快照上重算）

对 26 个包（`sms_tool/<子包>` 一级切分）做 Tarjan：

- **强连通分量 1 个，size 19**，包内（file→file 去重后）601 条边：

  `sms_tool` ⇄ `accounts` ⇄ `auth_flow` ⇄ `cli_parsers` ⇄ `commands` ⇄ `config` ⇄ `geo` ⇄ `mailbox` ⇄ `pay_link` ⇄ `payment` ⇄ `paypal` ⇄ `paypal_link` ⇄ `providers` ⇄ `proxy` ⇄ `registration` ⇄ `registration_drivers` ⇄ `sentinel` ⇄ `store` ⇄ `upi_link`

- **无环的包**：`sms_tool/kernel`、`services/`（全树）、3 个根级入口脚本。`kernel` 仍是唯一被 Rule 10 保护住的核心叶子——`scripts/kernel_boundary_ratchet.py` 的约束在图上成立。
- **双向边 28 对**，量级最大的几对（左 a→b，右 b→a）：

  | 包对 | a→b / b→a | 读法 |
  | --- | --- | --- |
  | `pay_link` ⇄ `payment` | **27 / 1** | 接近单向，反向那 1 条是泄漏点，最便宜 |
  | `mailbox` ⇄ `providers` | 12 / 8 | 已被 `provider_decoupling` / `mailbox_private_import` 两个棘轮盯住 |
  | `sms_tool` ⇄ `registration` | 12 / 55 | 顶层 re-export 面过大，`sms_tool/__init__` 是 77 引入者的枢纽 |
  | `sms_tool` ⇄ `proxy` | 9 / 4 | |
  | `sms_tool` ⇄ `geo` | **7 / 1** | 同样接近单向 |
  | `accounts` ⇄ `proxy` | 6 / 2 | |
  | `mailbox` ⇄ `sms_tool` | 5 / 5 | 完全对称，比 `sms_tool`⇄子包更贵 |

- **解析质量**（决定上面这些数字的可信度）：`exact` 10,712（16.6%）、`name-only` 38,420（59.7%）、无 `resolution` 15,263（23.7%）。`imports` 边 3,352 条但 `import` 类解析率 0%——因为 `services/protocol-payment` 的导入根是运行时注入的（见 §4），图看不到。

> 上一轮报告里的「694 edges」与本轮的 601 不是同一口径：前者统计含符号级引用，本轮只统计 file→file 去重边。两个数都自洽，跨报告比较前先对齐口径。

---

## 2. 七个支付提取器的「公共骨架」评估（提案，未落地）

### 2.1 现状尺寸

| 供应商 | 文件 | 行数 | 顶层/方法函数数 |
| --- | --- | --- | --- |
| blik | `blik/blik_qr_extract.py` | 3,095 | 145 |
| ideal | `ideal/ideal_qr_extract.py` | 2,594 | 127 |
| twint | `twint/twint_extract.py` | 2,584 | 127 |
| momo | `momo/momo_qr_extract.py` + `momo/ac_paylink_core.py` | 2,120 + 917 | 91 |
| kakao | `kakao/kakao_extract.py` | 1,590 | 65 |
| pix | `pix/pix_core.py` + `pix/pix_extract.py` | 1,378 + 862 | 64 |
| direct_card | `direct_card/direct_card_extract.py` | 1,207 | 50 |
| **合计** | 9 个模块（+ `run_momo.py` 188 / `run_pix.py` 100 两个薄入口） | **16,347** | 592 |

`common/` 21 个模块共 4,641 行，已被三档不同深度地使用：

- **深**：`blik` / `ideal` / `twint`，各自 15–19 条 `from common.*` 导入（连 `geo`、`stripe_flow`、`stripe_setup` 都走共享层）。
- **浅**：`momo` 2 条、`pix` 2 条、`kakao` 6 条、`direct_card` 3 条——它们停留在「只共享 `endpoints` / `protocol_core`」的早期状态。

### 2.2 重复度实测（两把独立的尺）

**仓库自有尺（权威）**：`scripts/extractor_parity_report.py`（名字归一化 + AST 比较，`is_delegating_stub` 不计重复）：

```text
extractor-parity ratchet OK (ideal:blik=13, ideal:twint=26, momo:kakao=0, twint:blik=10)
provider-decoupling ratchet OK (263 same-named def(s): 231 delegating, 0 identical, 32 divergent)
```

对比 `plan-2026-09-17` 的起点（ideal 134 个函数中 108 个与 twint AST 相同 ≈ 81%）：**已在阶段 1 + 阶段 2 两个小批次里把 ideal:twint 从 108 降到 26**。

**本轮独立尺**（把函数体里的标识符/字面量/数字全部掩码后取哈希；口径比仓库工具**更松**，会高估，仅用于排序）：

| 供应商 | 函数行中被「精确归一化克隆」覆盖的比例 |
| --- | --- |
| ideal | 542 / 2,048（26%） |
| twint | 476 / 2,039（23%） |
| blik | 168 / 2,454（7%） |
| momo / kakao / pix / direct_card | 0% |

整文件归一化 token 相似度：`ideal ~ twint` **0.99**（2,594 vs 2,584 行，`diff` 仅 480 行 / 71 个 hunk——twint 实质是 ideal 的 fork），`blik ~ ideal` 0.87，`momo/ac_paylink_core ~ pix/pix_extract` 0.93（小文件，共同骨架是「取 token → 开 Checkout → 抽 QR」流程）。
**四家（momo / kakao / pix / direct_card）与这三家之间 0 个精确克隆**——重复是**三家的簇属性**，不是 `services/` 的普遍属性，所以「统一的提取器框架」没有证据支持。

### 2.3 下一批候选：三边逐字节等价的 10 个函数

交集 `ideal∩twint ∩ ideal∩blik ∩ twint∩blik` 的 identical 集合（`--list --pair` 三次取交集）：

| 函数 | ideal / twint / blik 行数 | 三份合计 | 建议 |
| --- | --- | --- | --- |
| `create_checkout` | 109 / 109 / 102 | **320** | **先做**（见下） |
| `warmup_approve_context` | 22 / 22 / 22 | 66 | 可与上一项同批 |
| `fetch_redirect_page` | 14 / 14 / 14 | 42 | 纯 HTTP 取页，低风险、低收益 |
| `print_failure_result` | 14 / 14 / 14 | 42 | 输出格式，建议先统一输出契约再动 |
| `approve_attempt` | 12 / 12 / 12 | 36 | 可做 |
| `log` | 8 / 8 / 8 | 24 | **不做**：每个提取器的 logger 名字/文件是各自的 |
| `set_proxy` | 6 / 6 / 6 | 18 | **不做**：必须写进**自己**模块的 redaction 注册表（`extractor_runtime.py` docstring 已明文否决） |
| `log_payment_page_summary` | 6 / 6 / 6 | 18 | 随 `print_*` 一起 |
| `remove_failed_proxy` | 2 / 2 / 2 | 6 | 可做 |
| `print_already_paid_result` | 2 / 2 / 2 | 6 | 随 `print_*` 一起 |

三份合计 **578 行**；按「抽 1 份 + 3 个薄封装」计，净减约 **385 行**（扣除参数化与注入带来的新增行）。

**推荐批次（按风险/收益排序）**：

1. `create_checkout` + `warmup_approve_context`（386 行副本 → 1 份实现）。两者都读 provider 前缀环境变量并按 provider 选 payment-method 形状，因此**必须**沿用阶段 2 的成对参数化：`env_reader` / `provider_profile` / `log` 由调用方注入，共享模块不读 env。
2. `fetch_redirect_page` + `approve_attempt` + `remove_failed_proxy`（84 行）。接近纯函数，可作为批次 1 的附带项。
3. `print_*` 三个（66 行）**暂缓**：它们承载终端输出契约，值是「先定输出契约（schema/`terminal` 行），再合并」，否则会把三家的输出差异压成一个。

**明确不做**（本轮评估的否决项）：

- `set_proxy`、`log` —— 见上表理由，属「调用方上下文」，`plan-2026-09-17` §6 已经写过。
- `run_*` / `main` / `new_session` / `build_ctx` / `PROVIDER_proxy_chain`（`ideal:twint` identical 里的 16 个）—— 编排与代理分组，差异是真实语义（`ideal:twint` 的 `different` 桶 15 个里全是这类）。
- momo / kakao / pix / direct_card 的任何抽取 —— 与三家之间 0 克隆，没有证据支持抽象。

**证据门槛（沿用既有 plan，不新造）**：按 `plan-2026-09-28-stage2-replay.md` 用 `git show HEAD:...` 冻结迁移前函数体做 HEAD-vs-参数化差分（`tests/payment/stage2_*_head.py` + `tests/governance/test_stage2_replay_selfcheck.py` 的形状），迁移后同步 `--update-baseline` 收紧 `ideal:twint` / `ideal:blik` / `twint:blik`，并保证 `provider_decoupling` 的 `divergent` 不涨（当前 32）。

---

## 3. 全量测试

```text
python -m pytest -q
7153 passed, 10 skipped, 5 warnings, 1333 subtests passed in 587.62s (0:09:47)
```

- 0 failed / 0 error；10 skip 全在既有 `skipUnless` 分支（浏览器/凭据类）。
- 5 条 warning 全部来自第三方（`playwright_stealth` 重复应用、`datetime.utcnow` 等），无仓内代码告警。
- `def test_` 计数 6,360 与 pytest 的 7,153 之差来自 `unittest` subtests（1,333）与参数化展开。

---

## 4. 「死码」候选核实：15/15 误报

pi-lens 在旧快照上给出 15 条 dead weight。逐条核实结果：

| # | 候选 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | `services/protocol-payment/blik/blik_qr_extract.py` | 活 | `sms_tool/pay_link/adapters.py` 以 `subprocess.run` 启动提取器（`_run_extractor_subprocess` / `_prepare_extractor` 解析脚本路径） |
| 2 | `.../ideal/ideal_qr_extract.py` | 活 | 同上（`_build_ideal_env` → 同一派发表） |
| 3 | `.../twint/twint_extract.py` | 活 | 同上（`_build_twint_env`） |
| 4 | `.../direct_card/direct_card_extract.py` | 活 | 同上 + `sms_tool/payment/link_manager.py` 的 `_run_direct_card` |
| 5 | `.../common/endpoints.py` | 活 | 23 处 `from common.endpoints import` |
| 6 | `.../common/extractor_helpers.py` | 活 | 6 处导入 |
| 7 | `.../common/file_loading.py` | 活 | 6 处导入 |
| 8 | `.../common/http_dump.py` | 活 | 4 处导入 |
| 9 | `.../common/logging_setup.py` | 活 | 8 处导入 |
| 10 | `.../common/proxy_seed_file.py` | 活 | 6 处导入 |
| 11 | `.../common/proxy_selection.py` | 活 | 6 处导入 |
| 12 | `.../common/proxy_state.py` | 活 | 6 处导入 |
| 13 | `.../common/redaction.py` | 活 | 8 处导入 |
| 14 | `services/protocol-payment/__init__.py` | 活（**设计上无人导入**） | 文件自身 docstring：目录名带连字符 ⇒ 非法模块名，标记只为「静态分析 / 覆盖分片 / 死码启发式」识别，`pyrightconfig.json` 的 `executionEnvironments` 与 `scripts/services_import_resolution.py` 依赖它 |
| 15 | `services/mail-otp-web/app.py` | 活 | 独立 Web 服务入口（`#!/usr/bin/env python3` + `README.md`），且 `tests/mailbox/test_mail_otp_web.py` 用 `importlib.util.spec_from_file_location` 直接加载并断言 `parse_account_line` / `is_request_authorized` |

**权威反证**：

```text
$ python scripts/services_import_resolution.py
services import resolution OK (11 extractor(s) -> 21 common module(s), 6 common->common edge(s), 0 unresolved, 0 orphan)
```

**根因（本轮的真正结论）**：这三类文件全部处在「静态图看不见但运行时存在」的边界上——

1. 提取器在模块顶部 `sys.path.insert(0, str(PROTOCOL_ROOT))`，再 `from common.* import ...`。图按仓库根解析，`common` 不在 `sys.path` 里 ⇒ `imports` 边解析率 0%，`common/*` 于是全都「没人引用」。
2. 提取器不是被 import 的，而是被 `subprocess` 以脚本方式启动的 ⇒ 图里没有引用边。
3. `dead-code-python.json` 显示 `vulture not installed; skipped` ⇒ 该清单**只有可达性一个信号**，没有第二个独立证据源。

因此这 15 条**不能作为删除依据**，且这条规则的适用范围要写清楚：**只要一个模块的消费者是 subprocess 或 sys.path 注入，可达性信号就是零信息**。

---

## 5. 复现命令

```powershell
# 图：强制全量重建 + 复核
pi-lens build-graph --cwd .
python scripts/services_import_resolution.py

# 提取器重复度（权威尺）
python scripts/extractor_parity_report.py
python scripts/extractor_parity_report.py --list --pair ideal:twint
python scripts/provider_decoupling_ratchet.py

# 全量测试
python -m pytest -q
```

本次用于复核的一次性脚本（覆盖率/循环/克隆统计）写在被忽略的 `runtime/tmp/lens/`，不进库。

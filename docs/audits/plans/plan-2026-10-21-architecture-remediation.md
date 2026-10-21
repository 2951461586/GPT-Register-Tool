# 方案：架构健康度整改（图谱可信度 → 分层门禁 → 环 → 根目录）— 2026-10-21

> **状态（2026-10-22 复核）**：**P0 全部落地，P1-1 落地，P1-2 落地，P2 的解环部分落地（SCC 5 → 0），P2-4（`proxy_*` 迁移）已落地**；**P2-5…P2-8 四个模块族迁移与 P3-1 spec 图未开始**。落地结果回写在 §8，并已同步 `docs/architecture.md` / `docs/directory-map.md` / `docs/current/dependency-graph.md`。
>
> 未完成的项（`P2-5`…`P2-8` 四个模块族迁移、`P3-1` spec 图）**没有留下半成品**：工作区在每个提交点都是绿的，剩余项按原顺序可直接接续。
>
> 2026-10-22 复核实测（全部为仓库现有门禁的原始输出）：`dependency_graph` 300 模块 / 957 条模块级边 / 274 条函数内边 / 31 条 type-only / **0 个 SCC** / 11 对互指；`import_layer_ratchet` 281 边 / 35 对 / 11 mutual / 45 minority / 205 delayed / 2 条 allow、0 个未认领模块；`root_module_ratchet` **148 冻结**（155 − 7 个 `proxy_*`）。
>
> **性质**：方案文档 + 落地记录。所有数字来自 2026-10-21 用 AST 重测（见 §1 的测量口径），不引用 pi-lens 的目录级数字。

## 0. 与既有扫描的关系（先读这段）

`docs/audits/scans/scan-2026-10-04-import-cycle-decomposition.md` 已经把 pi-lens 报的
「470 边循环依赖」分解过一次，结论是：**288 条模块级 + 182 条函数内**，真正互指的只有
**11 对 / 60 条反向边**，其中 **46 条拆不得**（5 条 wildcard 门面、17 条 `mailbox_*` 门面、
29 条惰性边、`auth_flow/deps.py` 的 DI 注入面），并给出 §10 的增量收敛顺序。

本方案**不推翻那份扫描**，只做它没做的三件事：

1. **把「层」定义到模块粒度**。扫描停在目录粒度，于是 `sms_tool/` 这一个节点同时装着
   应用工作流和叶子契约，任何一对目录都"两个方向都合法"⇒ 层无法判定。§4 P1-1 解决。
2. **把测量变成生成物**。扫描是快照，两天后就会过期（本轮复测已从 288/31/11 降到
   267/28/10）。§4 P0-2 让依赖图成为 CI 生成并校验的文件。
3. **处理扫描 §10 中标注「需先拍板」的 providers 平局的一个有界切片**。扫描把
   `sms_tool ↔ providers` 的 19/19 平局列为「高」风险、需要「共同词汇层」的架构决策。
   本方案不整体推进它，只先切两条最没有道理的边（provider 直接写池文件、provider 发 UI 事件），
   并把「共同词汇层」记为明确延后项（§6）。

## 1. 问题与证据（2026-10-21 复测）

测量口径：`scripts/dependency_graph.py`（本方案 P0-2 新增）用 AST 解析 `sms_tool/**/*.py`
与 `services/**/*.py`，相对导入按包深度还原，函数体内导入单独计为 `delayed`。

| # | 问题 | 实测 | 根因 |
|---|---|---|---|
| 1 | 图谱解析率低、死代码假阳性 | 一个 `blik_qr_extract.py` 引 13 个 `common/*` 模块，图谱全判死 | `services/protocol-payment` 不是包：无 `__init__.py`，靠 `sys.path.insert` + `from common.x import`；测试用 `spec_from_file_location` 按路径加载 |
| 2 | 图谱过期、与门禁两套算法 | — | 图谱是快照，`import_layer_ratchet.py` 才是 AST 精确的权威；两者从不对账 |
| 3 | 互指关系无法判定方向 | 267 条模块级跨目录边 / 28 对 / 10 对互指（历史 441/43/30） | `sms_tool/` 根节点混装两层；`SUBPACKAGES` 只认 13 个子包 |
| 4 | 模块级 SCC | 重测后 **2 个真环**：`auth_flow` 包内 7、`upi_link` 包内 4（都是包门面环：`__init__` re-export 子模块 + 子模块 `from . import …` 回边） | 初次测量得到 5 个，其中 **3 个是 `if TYPE_CHECKING:` 造成的假环**（registration stage 簇 9、`browser_flow` 6、`phone_pool` 2）—— 注解导入不执行，不可能成环 |
| 4b | 被误报的「环」 | 另有 5 个「看起来的环」由**函数内导入**断开（`account_liveness` ↔ `browser_flow` 等） | 本仓把「改成函数内导入」作为既定的破环手段（`delayed_import_ratchet` 按总量单独守），与真环混算会得出 10 个 SCC 的错数 |
| 5 | 枢纽模块 | `config` 73 依赖者 / 1042 行；`kernel/sanitizer` 38；`kernel/paths` 32 | `config.py` 同时是 loader / schema / runtime view / legacy view / migration |
| 6 | provider 反向依赖 | 21 条边、11 个根模块（`mail_otp` 4、`mailbox_types` 3、`mailbox_poll` 2、`mailbox_pool_writer` 2、`config` 2、`phone_proxy` 2、`storage` 2、`mailbox_errors` 1、`mailbox_quarantine` 1、`desktop_ipc` 1） | Rule 03 只禁了 `registration` / `payment` / `paypal` 三个包，漏掉这些根模块 |
| 7 | 根目录扁平 | 155 个冻结模块，53,144 行；`registration_*` 29、`payment_*` 15、`mailbox_*` 15、`paypal_*` 9、`proxy_*` 7 | `root_module_ratchet` 只冻结名字、不允许新增，但没有迁出路径 |
| 8 | 未使用导入集中在门面 | 330 条 / 75 文件（历史 456 / 84），`upi_link/pipeline.py` 72、`payment_link_manager.py` 42、`registration.py` 26、`paypal_link/gen_link.py` 25 | 门面 re-export 的历史残留，不是死代码 |
| 9 | spec graph 缺失 | 无 `SPEC.md`、无 `id/type` frontmatter | 由 `docs/architecture.md` + `docs/rules/rule-NN-*` + ADR 承担，但没有可查询的图 |

## 2. 目标 / 非目标

**目标**

- 依赖图由代码生成、CI 校验，不再过期；死代码判定不再被 `sys.path` hack 污染。
- 分层从「逐对计数」升级为「逐模块方向」，新增违规精确报点，基线只能降。
- 三个模块级环各有一个可验证的拆法。
- 根目录 5 个模块族迁入子包，`root_module_ratchet` 的冻结数从 155 降到约 80。
- 边界/契约有可查询的 spec 图，且与 `rule-NN` / ADR 不冲突。

**非目标（本轮明确不做）**

- 不整体推进 `sms_tool ↔ providers` 的 19/19 平局（扫描 §10 序 5，需先拍板「共同词汇层」）。
- 不拆扫描 §4 记录的 46 条有意设计边。
- 不把 spec graph 全量迁移（会与 4 套文档门禁打架）。
- 不把根目录清零（剩余 glue 模块保留，冻结数只降到约 80）。

## 3. 已确认决策

| 决策 | 结论 |
|---|---|
| 落地范围 | P0 → P1 → P2 全量（含根目录逐簇迁移），P3 走最小版 |
| 分支 | 直接在 `main` 提交 |
| 每步验收 | 相关域 `pytest` + 全部相关 ratchet/scan + `ruff check` + `format_guard` |
| spec 图 | 只给边界/契约建 ~10 个，`implements: rule-NN`、`depends-on: adr-NNNN` |

## 4. 阶段任务

### P0 — 工具与门禁（行为中立，除 P0-3 新增一个开关）

| 序 | 任务 | 做法 | 验证 |
|---|---|---|---|
| P0-1 | `services/protocol-payment` 的 `common.*` 导入根可解析 | **不重命名目录**（`pyrightconfig.json` 已用 `executionEnvironments.extraPaths` 声明了这个导入根，改名要动 300 处引用且不改运行时行为）。改为新增 `scripts/services_import_resolution.py`：用运行时同一条规则解析 `common.*`，并断言 ① 每条导入都落到 `common/` 下真实模块 ② 每个 `common/*.py` 都能从某个提取器到达；配 8 个 fixture 测试钉住解析器 | `python scripts/services_import_resolution.py`；`pytest tests/governance/test_services_import_resolution.py` |
| P0-2 | 生成式依赖图 | 新增 `scripts/dependency_graph.py`（AST，含相对导入还原、SCC、互指对、根节点入度），校验 `docs/current/dependency-graph.md` 是否与源码一致，`--write` 重生成；CI 加一步 | 手改一行 import → `python scripts/dependency_graph.py` 失败并指出文件行号。**注意**：落地时没有做 `--check` 开关 —— 无参数即校验、`--write` 才重生成，少一个「忘了加 --check 就静默通过」的口子 |
| P0-3 | `--init-config` | 新增 CLI 开关：按 `SHARD_FILES` 写 `proxy/runtime/payment` 三个分片，已存在则跳过；`--doctor` 在无配置时打印该命令 | 空目录跑一次 → 三个分片；再跑一次 → 全跳过 |
| P0-4 | provider 端口化（有界切片） | 新增 `sms_tool/providers/ports.py`：`PoolWriterPort` / `ProgressPort`；`mailbox_gmail` / `mailbox_graph` 的 `apply_rotation` 与 `mailbox_remail` 的 `progress_dots_enabled` 改为端口注入；Rule 03 的 grep 扩到这两个模块 | `grep -rn "from \.\.\(mailbox_pool_writer\|desktop_ipc\)" sms_tool/providers/` 为空；`import_layer_ratchet` 收紧 |

### P1 — 分层与枢纽

| 序 | 任务 | 做法 | 验证 |
|---|---|---|---|
| P1-1 | 模块级层清单 + 方向门禁 | 新增 `scripts/layer_manifest.json`：153 个根模块 + 13 子包 + 5 个包内覆盖，层号沿用 `architecture.md` 的顺序（1 表层 → 5 适配器）；`import_layer_ratchet` 增加方向判定（`source > target` 即违规，**含同目录边**，否则包门面环看不见）与「每个模块必须被认领」断言；allow 只有 2 条，各带 reason + owner | `import_layer_ratchet.py` → `2 allow-listed inward edge(s), 0 unclassified`；10 个新用例覆盖违规/合法/allow/未认领/同目录 |
| P1-2 | `config` 拆包 | `sms_tool/config/` = `loader.py`（分片/迁移）、`schema.py`（`validate_config` + warnings）、`runtime.py`（`RuntimeConfig`/scope）、`legacy.py`（`LegacyConfigView`），`__init__.py` 只 re-export；73 个 import 一行不改 | `pytest tests/config/`；`config_schema_check.py`；`config_key_ratchet.py` |

### P2 — 环与根目录

| 序 | 任务 | 做法 | 验证 |
|---|---|---|---|
| P2-1 | `TYPE_CHECKING` 导入不计入运行时环 | 两个解析器都把 `if TYPE_CHECKING:` 里的导入单独计为 `type`；它们不执行，不可能成环。SCC 5 → 2，**没有改一行产品代码** | `dependency_graph.py` → 2 cycles, 11 type-only；`pytest tests/governance` |
| P2-2 | 包门面环惰性导出（`auth_flow` 7 + `upi_link` 4） | 两个包的 `__init__.py` 改 PEP 562 `__getattr__`（名字表由原 import 块机械生成），并保留一个 `if TYPE_CHECKING:` 块给 pyright；子模块 `from . import x` 的回边保留（它是真实依赖） | SCC 2 → **0**；全量 `pytest` 6692 passed；`tests/governance/test_lazy_facades.py` 钉住「导入包不导入子模块」 |
| P2-3 | ~~`phone_pool` ↔ `phone_provider_lifecycle`~~ | **并入 P2-1**：那条互指是 `phone_provider_lifecycle` 的注解导入 | — |
| P2-4 | `proxy_*` → `sms_tool/proxy/`（7） | `git mv` + codemod 改 dotted 引用；`SUBPACKAGES` 加 `proxy`；`directory_map_groups.json` 加组 | 全量 `pytest`；`root_module_ratchet --update-baseline` 降 7 |
| P2-5 | `mailbox_*` → `sms_tool/mailbox/`（15） | 同上；`mailbox.py` → `mailbox/router.py` + `__init__` 门面（`sms_tool.mailbox` 路径不变） | 同上，降 15 |
| P2-6 | `paypal_*` → `sms_tool/paypal/`（9） | 同上，并入既有 `paypal/` 包 | 同上，降 9 |
| P2-7 | `payment_*` → `sms_tool/payment/`（15） | 同上 | 同上，降 15 |
| P2-8 | `registration_*` → `sms_tool/registration/`（29） | `registration.py` → `registration/orchestrator.py` + `__init__` 门面 | 同上，降 29 |

### P3 — spec 图（最小版）

| 序 | 任务 | 做法 | 验证 |
|---|---|---|---|
| P3-1 | 边界 spec 图 | 为 10 个边界/契约建 `architecture-design` spec，`implements: rule-NN`、`depends-on: adr-NNNN`；CI 加 `spec_validate` | `spec_validate` 无 dangling / cycle |

## 5. 每步的公共验收

```text
python -m pytest -q <相关域>
python scripts/import_layer_ratchet.py
python scripts/root_module_ratchet.py
python scripts/module_coverage_check.py
python scripts/architecture_scan.py
python -m ruff check sms_tool services scripts
python scripts/format_guard.py
```

搬模块的步骤额外跑：`python -m pytest -q`（全量，约 220 s 并行）。

## 6. 明确延后

- **providers 19/19 平局的整体处理**：需要先拍板「共同词汇层」（`mail_otp` / `mailbox_types` /
  `config` / `storage` 下沉到哪一层），属架构决策，不在本轮的清理动作里。本轮只切
  `mailbox_pool_writer` 与 `desktop_ipc` 两条。
- **`sms_tool/registration.py` ↔ `registration_drivers` 的 4 条反向边**：扫描 §10 序 4 已记录为
  「同层横跳」，结论是记录、不动。
- **`unused_import` 330 条**：门面残留，按文件逐个清理，不在本方案内一次性处理。
- **`services/mail-otp-web`**：零耦合运维工具，不是死代码；是否加启动脚本另议。

## 7. 回滚

每一步是独立提交，且只依赖前一步的产物：

- P0-1 / P0-3 独立可回滚。
- P0-2 / P1-1 是纯新增脚本与 CI 步骤，回滚不影响运行时代码。
- P1-2 拆包保留 `sms_tool.config` 的 re-export 面，回滚只需恢复单文件。
- P2-1..3 解环都保留原有调用面；P2-4..8 每簇一个提交，`root_module_ratchet --update-baseline`
  与 codemod 同提交，`git revert` 单簇即可。

## 8. 落地记录

### P1-1（层清单 + 方向门禁）

153 个根模块 + 13 子包 + 5 个包内覆盖，层号沿用 `architecture.md` 的顺序（1 表层 → 5 适配器）。
门禁判据是 `source > target`，**含同目录边** —— 只看跨目录边会漏掉包门面环（`__init__` 引用自己的高层子模块）。
allow 只有 2 条，各带 reason + owner。落地时先按「1 = 最内层」编号，发现与 `architecture.md`
的「outermost first」相反，整体翻转为 1 = 最外层，判据随之从 `source >= target` 改为 `source <= target`。

### P1-2（config 拆包）— 已落地

`config.py`（1104 行）拆成 `sms_tool/config/`：`_state.py`（30）· `loader.py`（286）· `schema.py`（637）·
`runtime.py`（174）· `legacy.py`（58）· `__init__.py`（71，只做 re-export）。**73 个 import 方一行未改**。

方案里的实施顺序（「先把模块级状态收进 `config/_state.py`，再搬函数」）**做不到**：
`sms_tool/config.py` 与 `sms_tool/config/` 在文件系统上不能共存，中间态无法提交。实际是一笔提交，
分界线换成了「先定接口、再搬代码」。

落地时真正花时间的不是搬函数，而是**四处 seam 会随拆分静默失效**——它们都不会报错，只会让测试
测到真配置：

| seam | 拆前 | 拆后必须改成 | 为什么 |
|---|---|---|---|
| 配置目录 | `patch.object(config, "_CONFIG_DIR", …)` | `patch.object(config_state, "CONFIG_DIR", …)` | 子模块若 `from ._state import CONFIG_DIR` 会把值在导入时定死 |
| 包目录 | `config.__file__.parent` | `_state.PACKAGE_DIR` | `runtime.py` 的 `.parent` 是 `sms_tool/config/`，内置 fallback 会指错地方 |
| 分片模板 | `patch.object(config, "example_config_candidates")` | `patch.object(config.loader, "example_config_candidates")` | 被调函数在 `loader` 的命名空间里解析 |
| 冻结/视图 | `config._freeze` / `config.CFG` | 无需改（`__init__` re-export） | 但 re-export 必须是真身而非副本 |

**第一个环是新拆出来的**：`loader`/`runtime` 里写 `from . import _state`，解析器把这条边算到**包**上
（这是对的——它确实先 `import` 包），而 `__init__` 又 import 每个子模块，于是 4 模块环。改成
`from ._state import config_dir`（读函数，函数读模块全局）后 **SCC 1 → 0**，seam 仍然有效。

门禁同步六处：`config_key_ratchet`（读 `config/schema.py`）· `bare_print_baseline`（print 随
`load_runtime_config` 进 `runtime.py`）· `delayed_import_baseline` + `delayed_import_reasons`（三条
函数内导入换了文件名，理由要重写）· `lane_coupling_baseline`（payment 读取方改成
`sms_tool.config.schema`，但 registration 扇出仍记包名）· `layer_manifest`（包层号 5 + 删根条目）·
`directory_map_groups` + `docs/directory-map.md` + 一处 `docs/current` 行号锚点。

实测：`module coverage 307/307` · `dependency graph 0 cycles` · `root_module_ratchet` 147 ·
全量 **6700 passed**。

### P2-4（`proxy_*` 迁移）— 已落地（`1b34697`）

7 个模块迁入 `sms_tool/proxy/`（`proxy_bridge`→`bridge`、`proxy_routing`→`routing` 等），
~120 处引用由 `scripts/module_move.py` 改写，`root_module_ratchet` 冻结数 155 → 148，
`dependency_graph` 仍为 **0 个 SCC**，`module coverage 302/302`。

第一次尝试（2026-10-21）**没有一次跑绿**，整簇回滚到 `763e185`；回滚不是终点，它把五个
门禁的耦合点暴露了出来，`module_move.py` 就是把这五点做成工具的产物。下表保留为工具的设计依据：

| 门禁 | 现象 | 需要做的 |
|---|---|---|
| `tests/governance/test_dependency_guard.py` | `proxy-edge-probe` 被 `upi_link/india_exit.py`、`pipeline.py` 报成未声明的第三方依赖 | 该测试从 import 语句推导发行包名，`sms_tool.proxy.*` 需要被识别为仓库内包（它已有 `LOCAL_PACKAGE_PREFIXES` / `_repo_module_stems` 两个入口，改动点在这两处之一） |
| `docs_consistency_scan.py` | `registration-and-proxy-architecture.md` 的 `proxy_routing.py:85` 等行号锚点失效 | 锚点改成 `proxy/routing.py:NN` **并核对行号**（迁移只改文件头 import，行号可能位移） |
| `test_import_layer_ratchet.py::test_summary_facts_are_pinned` | 固定总数变化：278 边 / 35 对 / 11 mutual / 45 minority / 205 delayed | 按该文件惯例补一段变更记录并更新断言 |
| `delayed_import_ratchet.py` | 文档化环表的键是「最后一段」，`proxy_routing`→`routing` 后分类从 `cycle` 掉成 `deferred` | 已定位：`DOCUMENTED_CYCLES` 需加 `("routing", "account_identity")`（本轮已改过，回滚时一并丢弃） |
| `format_guard.py` | 改写后需 `ruff format` | 迁移脚本最后一步应包含它 |

另外两个**不是**迁移引入、但会被 per-edit 检查报出来的既有债务（`pyright_baseline.json` 已冻结）：
`sms_tool/accounts/account_liveness.py`（baseline 3）与 `sms_tool/proxy/bridge.py`（baseline 3）。
第一次尝试时顺手修掉了 `bridge.py` 的 3 条（`_close_server` 缺 `@staticmethod`、`upstream` 未收窄）
与 `account_liveness` 的 `proxies: Any`，但随回滚一并丢弃；它们仍是**未处理的既有债务**，
不在本轮范围内。

**教训**：迁移脚本必须把「门禁同步」当作迁移的一部分，而不是事后补 —— 五个门禁里有三个是
**按模块名或行号硬编码**的，只改代码不改它们必然红。

### 迁移工具（已落地）

`scripts/module_move.py` + `scripts/move_specs/*.json`：一条命令完成「移动 + 改写 + 门禁同步 +
基线重生成 + 门禁验证」。它按顺序做：

1. `git mv` 七个/任意个模块进目标包（必要时建 `__init__.py`）；
2. 重写相对导入 —— 用**反向映射**取旧模块名（第一次尝试的 bug 就在这：文件已在新区，
   用新名解析兄弟模块会得到 `sms_tool.proxy.proxy_entry`），并正确处理
   `from . import a, b as c` 里「部分名字被移动」的情况；
3. 重写绝对点号名、`from sms_tool import <mod>` 形式、裸回退导入
   （`upi_link` 为直接脚本执行保留的 `from proxy_edge_probe import …`）、
   文件路径字符串与文档里的 `xxx.py:NN` 锚点；
4. 同步门禁元数据：`SUBPACKAGES`、`layer_manifest.json`（包层号 + 删根条目）、
   `directory_map_groups.json`（折叠成 `sms_tool/<pkg>/*.py`）、
   `delayed_import_ratchet.DOCUMENTED_CYCLES`（按 spec 提供的新键）；
5. `ruff format` → `dependency_graph.py --write` → 三个 ratchet `--update-baseline`
   → 回写 `test_import_layer_ratchet` 的固定总数；
6. 跑 8 个门禁，任一失败即非零退出（`git checkout .` 可整簇回滚）。

`--check` 只校验 spec 并打印计划，不动树。

**收尾过程**：`proxy` 簇第一次跑完时，8 个门禁全绿、`pinned totals` 自动回写，全量测试从 11 红
收敛到 **1 红**：`tests/workbench/test_cli_one_click_sms.py::test_one_click_sms_forces_one_phone_per_email`
—— 该用例直接调用 `cli._one_click_sms_max_reuse`，而这个全局由 `_load_helper_deps()` 填充；
迁移后它在「先跑该用例」的顺序下拿到 `None`。根因不在迁移本身，而是**用例依赖执行顺序**：
`24ec800` 让用例自己解析 helper deps，不再靠「别的用例先跑过」；`04c3c19` 补齐工具的三个缺口
（`architecture_scan` 的包名、`delayed_import_ratchet` 的键、`mailbox` spec）；`600ae5b` 让
`delayed_import_ratchet` 按被分析树的实际路径解析目标。三笔修完，`1b34697` 把 `proxy` 簇落地。

### 剩余未完成（2026-10-22 复核）

`P2-5`…`P2-8`（`mailbox_*` / `paypal_*` / `payment_*` /
`registration_*` 迁入子包）与 `P3-1`（边界 spec 图）未开始。它们的共同前置已经就绪：层清单（P1-1）
给出每个模块该去哪一层，生成式依赖图（P0-2）给出搬迁前后的边数对照，`module_move.py` 把
「移动 + 改写 + 门禁同步 + 基线重生成 + 门禁验证」做成一条命令，`scripts/move_specs/mailbox.json`
已备好。每簇仍应单独一个提交，并在同一提交里更新 `import_layer_baseline.json`、
`directory_map_groups.json` 与 `docs/directory-map.md` 的 claim。

### P2-2（包门面惰性导出）

`auth_flow`（64 个名字）与 `upi_link`（160 个名字）的 `__init__` 改为 PEP 562 `__getattr__`，
名字表从原 import 块机械生成。**SCC 2 → 0**，全量 6692 passed。

两个细节必须同时做，否则会换一种坏法：

1. **必须保留 `if TYPE_CHECKING:` 块**。只做 `__getattr__` 时 pyright 在每个消费者里报
   `"X" is unknown import symbol`（实测 9 个错误落在 `test_registration_concurrency.py`、
   1 个落在 `account_recovery.py`），`pyright_ratchet` 会因为这两个文件基线为 0 而失败。
   加上 TYPE_CHECKING 块后 pyright 恢复可见，而图里这些边是 type-only，不构成运行时环。
2. **`upi_link` 原本没有 `__all__`**，生成器必须回退成 `sorted(_LAZY)`，否则 `import *` 会静默变成空。

`tests/governance/test_lazy_facades.py` 用**子进程**验证「导入包不导入任何子模块」—— 这是破环的
实质属性，在同一个进程里测不出来（`sys.modules` 已被别的测试污染）。

### P2-1（`TYPE_CHECKING` 不是运行时边）

两个解析器把 `if TYPE_CHECKING:` 内的导入单独计为 `type`。SCC 5 → 2，产品代码零改动。
这一条同时解释了初次测量里最大的三个「环」：registration stage 簇 9、`browser_flow` 6、
`phone_pool` 2 —— 每个 stage 只是为注解 import 了 `RegistrationEmailWorkflow`，运行时不成立。


### P0-1（`services_import_resolution.py`）

原计划是「包化 + 删 `sys.path` + 改 `from ..common`」。落地时发现 `pyrightconfig.json` 已有
`executionEnvironments.extraPaths` 声明了这个导入根，且目录名带连字符本来就不可包化 ——
改名要动 300 处引用却不改变运行时行为。改为「让工具认识这个导入根」，并新增门禁两向都测：
解析不到的导入报行号，没有任何提取器能到达的 `common/*.py` 报孤儿。实测 11 个提取器 →
21 个模块、6 条 `common` 内部边、0 孤儿。

### P0-2（`dependency_graph.py` + `docs/current/dependency-graph.md`）

生成式视图落地，实测 **298 模块 / 981 条模块级边 / 262 条函数内边 / 5 个 SCC / 10 对互指**。
落地过程修正了方案的两处判断：

1. **SCC 不是 10 个而是 5 个**。先前脚本把函数内导入也算进环，于是
   `account_liveness` ↔ `browser_flow` 这类「已经用惰性导入破掉的环」被当成真环。
   现在惰性边单独计（本仓的破环手段），5 个真环里 **3 个是包门面环**，形状一致。
2. **P2-2 的对象换了**。原计划要切 `account_liveness` ↔ `browser_flow`，实测那条边已经是
   函数内导入，无需改动；真正需要处理的是 `browser_flow` 包内 6 模块环，它与 `auth_flow`、
   `upi_link` 是同一个「包 `__init__` re-export + 子模块回边」形状，所以合并成一步。

`import_layer_ratchet` 仍是**门禁**（逐对、只许下降），新文件是**视图**；两者计数单位不同
（语句 vs 模块对），因此 `tests/governance/test_dependency_graph.py` 钉的是**有序目录对集合**
与**互指对集合**相等，而不是总数相等。


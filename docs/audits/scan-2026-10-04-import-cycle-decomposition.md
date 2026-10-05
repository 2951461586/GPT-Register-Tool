# 扫描 2026-10-04：470 边「循环依赖」的分解 · provider 解耦测量 · 死代码清理

> **性质**：扫描 + 落地（同日）。落地面 = 2 个新 ratchet + 2 个新测试 + 4 个死脚本删除 + 1 个阻碍项修复。
> **基线**：HEAD `c9386db`（`feat(registration): add the P0-1/P0-2/P1-3 live A/B harness`）。
> 扫描开始时工作树干净，仅有 1 个未提交文件（`services/protocol-payment/twint/twint_extract.py`，纯格式化）。
> **与既往审计的关系**：`scan-2026-09-22-architecture-coupling-dirs-docs.md` §3.1 已把耦合
> 测到**模块级 SCC** 并得出「8 组静态互指、0 个 import-time 环」。本轮**换了一个粒度**
> （pi-lens 的**目录级** 14 节点图），因此测出的是**不同的一层**，不重复也不推翻 09-22 的结论。
> 探针在 `runtime/tmp/`（`_scc_scan.py`、`_dircut2.py`、`_edges3.py`、`_shim2.py`、`_astdup.py`），未入库。

---

## 0. 门禁基线（全绿，含本轮新增 2 项）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 架构守卫 | `scripts/architecture_scan.py` | `Architecture scan passed` |
| 文档一致性 | `scripts/docs_consistency_scan.py` | `passed (release-v2026.09.23.md)` |
| 模块覆盖率 | `scripts/module_coverage_check.py` | `290 tracked, 290 claimed, 0 unclaimed` |
| 提取器同构 ratchet | `scripts/extractor_parity_report.py` | `OK (ideal:blik=28, ideal:twint=47, momo:kakao=0, twint:blik=25)` |
| 配置键 ratchet | `scripts/config_key_ratchet.py` | `OK (registration=31, email_registration=33)` |
| bare-print ratchet | `scripts/bare_print_ratchet.py` | `OK (511 bare prints)` |
| 延迟导入 ratchet | `scripts/delayed_import_ratchet.py` | `OK: 417 <= baseline 417` |
| 未使用导入 ratchet | `scripts/unused_import_ratchet.py` | `OK (355 <= baseline 355, 81 files)`（**本轮 357 → 355**） |
| endpoints 字面量 ratchet | `scripts/endpoints_literal_ratchet.py` | `OK (235 inline literal(s) across 40 file(s))` |
| mailbox 私有导入 ratchet | `scripts/mailbox_private_import_ratchet.py` | `OK`（6 个 hub 计数与基线一致） |
| **导入分层 ratchet（新）** | `scripts/import_layer_ratchet.py` | `OK (288 module-level edge(s) over 31 pair(s); 11 mutual, 60 minority; 182 delayed)` |
| **provider 解耦 ratchet（新）** | `scripts/provider_decoupling_ratchet.py` | `OK (238 same-named def(s): 182 delegating, 8 identical, 48 divergent)` |

全量验收：`pytest -q` → **5779 passed, 8 skipped, 1097 subtests passed, 0 failed**（6m04s）。
`dotnet build -c Release` → **0 errors**，118 warnings（nullable / xUnit 分析器提示）。

---

## 1. 结论摘要

| # | 轴 | 结论 | 证据 | 状态 |
|---|---|---|---|---|
| B1 | 耦合 | pi-lens 报的 **470 边循环** 分解为 **288 条模块级 + 182 条函数内（惰性）**；真正互相指认的只有 **11 对 / 60 条反向边** | §2–§3 | ✅ 已测量并冻结 |
| B2 | 耦合 | 470 里的 **46 条属有意设计**（5 wildcard 门面 + 17 `mailbox_*` 门面 + 29 惰性 + `auth_flow/deps.py` 的 DI seam），**拆掉会打破 ADR-0009 与测试锚点** | §4 | ⛔ 明确不动 |
| B3 | 耦合 | 12 条「`sms_tool/upi_link/__init__.py` 属于 `sms_tool`」的**幻影边**是我第一版探针的 `dir_of` 缺陷；修正后 `sms_tool -> sms_tool/upi_link` 是 **1** 而不是 13 | §5.1 | ✅ 已修正（被新 ratchet 的 fixture 钉住） |
| B4 | 耦合 | 60 条反向边的**头部集中**在 `cli.py`(10) + `providers/mailbox_remail`(8) + `registration`(4) + `sentinel/client`(4) | §3.3 | 供后续增量收敛 |
| B5 | 解耦 | provider 到 `common/` 的迁移**已完成 182/238（76%）**；剩 56 个非转发实现（blik 占 40） | §6 | ✅ 已测量并冻结 |
| B6 | 解耦 | 分类器第一版**只比函数体、不比签名**，于是把 `def f(a,b,c=0): return a+b` 判成与 `def f(a,b): return a+b` **identical** —— 收敛它会改变全部调用方的实参绑定，正是 Rule 19 禁止的事 | §6.3 | ✅ 被自己的 fixture 拦下 |
| B7 | 死代码 | pi-lens 的 10 项「DEAD WEIGHT」里 **8 项是假阳性**（CI / 测试按**文件名**调用，不做 import）；真死 4 项已删 | §7 | ✅ 已删 4 项 |
| B8 | 阻碍 | 刷新 review graph 后 `scripts/registration_ab.py:449` 报 pyright 阻断（`__doc__` 可能是 `None`）—— **是新提交引入的真回归** | §8 | ✅ 已修 |

---

## 2. 方法与粒度的选择

### 2.1 pi-lens 的粒度是**目录级**，不是模块级

`pilens_project_report` 的 CYCLES 段落把整个 `sms_tool/` 压成**一个节点**，
13 个子包各一个节点，共 14 个节点：

```text
sms_tool <-> sms_tool/accounts <-> ... <-> sms_tool/upi_link   (470 edges)
```

`470 edges` 是这 14 个节点之间**import 语句**的条数（我的独立实现测得 **470**，逐字吻合）。

**它与 09-22 的模块级结论不矛盾**：模块级 SCC 只有 9 个模块，
但目录级会把「A 包里的 a1 → B 包的 b1」与「B 包的 b2 → A 包的 a2」并成一对互指，
从而报出大环。**两个粒度都对，回答的是不同问题**：
模块级答「import 期会不会真成环」，目录级答「分层有没有被违反」。

### 2.2 测量脚本的三次自我纠错

| 版本 | 缺陷 | 后果 |
|---|---|---|
| v1 `_scc_scan.py` | 把 `sms_tool/upi_link/__init__.py` 判为 `sms_tool` | `sms_tool -> sms_tool/upi_link` 报 **13**，真实是 **1**（12 条幻影边） |
| v2 `_dircut2.py` | 修了 `dir_of`，但「模块级 vs 函数内」的分类器**逻辑恒真**（循环体里无条件 `return True`） | 92 条反向边**全部**被报成「模块级」，把 29 条惰性边算进了「可减少」面上 |
| v3 `_edges3.py` | 两者都修（`is_pkg` 感知 + 正确的 `id()` 归属判定） | 得到本报告采用的数字 |

**教训**（与 09-22 §9.16 同源）：**「总数对」不代表「分组对」**。
v1 的总边数 470 一开始就是对的，错的只有分组 —— 而那正是全部结论的来源。
本轮的补救不是「下次小心」，而是**把这两处判据写进新门禁的 fixture**
（见 §9.1：`test_package_relative_self_import_is_not_an_edge`）。

---

## 3. 470 边的分解

### 3.1 总量

| 量 | 值 |
|---|---|
| 目录级 import 语句总数 | **470** |
| 其中**模块级**（文件顶层 / 顶层 `if` / 顶层 `try`） | **288** |
| 其中**函数内**（惰性） | **182** |
| 模块级涉及的有向目录对 | **31** |
| 互相指认的目录对（两个方向都有模块级边） | **11** |
| 「少数方向」的模块级边（= 可减少面） | **60** |

> 182 条惰性边**不在**新 ratchet 的冻结范围内：把一条成环边改成函数内导入是本仓
> **既定且被记录的**破环手段，冻结它等于禁止修复。它们由
> `delayed_import_ratchet.py` 按**总量**（417）单独守住。

### 3.2 11 对互指（模块级），按反向边数排序

| 目录对 | 模块级 (A→B / B→A) | 少数方向 | 反向边数 |
|---|---|---|---|
| `sms_tool` <-> `providers` | 19 / 19 | `providers` → `sms_tool` | **19** |
| `sms_tool` <-> `accounts` | 9 / 63 | `sms_tool` → `accounts` | **9** |
| `sms_tool` <-> `commands` | 9 / 14 | `sms_tool` → `commands` | **9** |
| `sms_tool` <-> `auth_flow` | 4 / 9 | `sms_tool` → `auth_flow` | 4 |
| `sms_tool` <-> `paypal_link` | 4 / 7 | `sms_tool` → `paypal_link` | 4 |
| `sms_tool` <-> `registration_drivers` | 4 / 19 | `sms_tool` → `registration_drivers` | 4 |
| `sms_tool` <-> `sentinel` | 4 / 4 | `sentinel` → `sms_tool` | 4 |
| `sms_tool` <-> `pay_link` | 2 / 34 | `sms_tool` → `pay_link` | 2 |
| `sms_tool` <-> `paypal` | 2 / 9 | `sms_tool` → `paypal` | 2 |
| `sms_tool` <-> `store` | 2 / 10 | `sms_tool` → `store` | 2 |
| `sms_tool` <-> `upi_link` | 1 / 23 | `sms_tool` → `upi_link` | 1 |

**注意 `providers` 与 `sentinel` 是 19/19 与 4/4 的平局** —— 这两对的「少数方向」不是由
数量决定的，而是由**语义**决定的（见 §4）。平局说明该对的两半体量相当，
更可能是「同一层的两半被拆到了两个目录」，而不是清晰的上/下层关系。

### 3.3 60 条反向边的来源模块（Top 10）

| 条数 | 来源模块 |
|---|---|
| 10 | `sms_tool/cli.py` → `commands`(9) + `accounts`(1) |
| 8 | `providers/mailbox_remail.py` → `sms_tool` |
| 4 | `sms_tool/registration.py` → `accounts`(2) + `registration_drivers`(2) |
| 4 | `sentinel/client.py` → `sms_tool` |
| 3 | `sms_tool/gen_pp_link.py` → `paypal_link`(2) + `upi_link`(1) |
| 3 | `providers/mailbox_icloud_url.py` → `sms_tool` |
| 2 | `payment_link_manager.py` → `pay_link`（`from .pay_link import *`） |
| 2 | `paypal_auto.py` → `paypal`（`from .paypal import *`） |
| 2 | `paypal_reconciliation.py` → `paypal_link`（`from .paypal_link import *`） |
| 2 | `storage.py` → `store`（`from .store import *`） |

---

## 4. 拆不得的部分：46 条属有意设计

这是本轮**最重要的结论**。pi-lens 只报「有环」，报不出**哪些环是设计**。
逐条读过之后，470 里有 46 条一旦按「消除反向边」的直觉去改，就会破坏已记录的决策：

### 4.1 5 条 wildcard 门面（ADR-0009）

```python
sms_tool/storage.py:7              from .store import *          # noqa: F401,F403
sms_tool/paypal_auto.py:23         from .paypal import *         # back-compat re-export surface
sms_tool/gen_pp_link.py:6          from .paypal_link import *
sms_tool/payment_link_manager.py:49 from .pay_link import *
sms_tool/paypal_reconciliation.py:6 from .paypal_link import *
```

`storage.py` 被 **30 个模块**导入；删门面等于让 30 处改导入路径，
而收益只是让一张图好看。`architecture_scan.py` 已有专门规则
（`sms_tool/store/accounts.py` 不得导入具体邮箱 provider）保护这一层，**门面是它的一部分**。

### 4.2 17 条 `mailbox_*` / `outlook_imap` 门面

`sms_tool/mailbox.py`(9) + `mailbox_quarantine`(2) + `mailbox_strategies`(2) +
`mailbox_cfworker` + `mailbox_gmail` + `mailbox_graph` + `mailbox_icloud_url` +
`mailbox_parsers` + `mailbox_remail` + `mailbox_smailr` + `outlook_imap`(各 1)。

`docs/directory-map.md` 明写这些顶层模块是**兼容门面**，
`architecture_scan.py` 有一条硬规则：**门面文件不得定义实现符号**：

```python
if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in tree.body):
    failures.append(f"{facade.relative_to(ROOT)} is a compatibility facade but defines implementation symbols")
```

⇒ 门面 → provider 的这 17 条边是**被门禁要求存在的**。

### 4.3 29 条惰性边

破环的既定手段。`delayed_import_ratchet.py` 的 docstring 记录了这个模式的来源：
「延迟 import 总数超基线」是历史上反复踩的坑，修复方式就是**上提到模块顶部**——
所以惰性 import 在这个仓里是**受控的例外**，不是随手可加的。本 ratchet 因此
**显式不冻结**它们，只在 `--detail` 里如实列出。

### 4.4 `auth_flow/deps.py` 是 DI 注入面，不是耦合

```python
from ..accounts.account_creation import _validate_email_otp
from ..mailbox import _poll_email_otp
...
```

它的 docstring 写明了理由，且与 `docs/CONTEXT.md` 记录的「payment-capability seam 同类缺陷」互为印证：

> A test then patches exactly one attribute --
> ``sms_tool.auth_flow.deps.request_with_retry`` -- and every caller sees it.
> A direct ``from ..http_client import request_with_retry`` in a submodule would
> bind the function at import time and make the patch silently ineffective.

⇒ 这 1 条反向边是**为了可测试性**存在的。09-22 §3.3 已经踩过一次同类坑
（把函数体内导入提到模块级 ⇒ `patch("account_recovery.CFG")` 静默失效）。

### 4.5 已在原地写理由的反向依赖：`store/connection.py`

09-22 §7.1 已驳回一次，本轮**独立复现同一结论**，保留驳回：

> DELIBERATE REVERSE DEPENDENCY - do not "clean this up".
> The shell is the test suite's patch injection point: 7 test files do
> `patch.object(storage, "database_path", ...)`.

（该反边是**函数内**的，因此它不出现在本轮的 60 条里 —— 这也是一个交叉验证：
两个独立实现都把它归为「惰性」。）

---

## 5. 12 条幻影边的具体形态（供后人识别同类陷阱）

### 5.1 症状

第一版探针把 `sms_tool/upi_link/__init__.py` 读成模块 `sms_tool.upi_link`，
再因为 `len(parts) > 2` 不成立而把它归给目录 `sms_tool` ——
于是 `upi_link/__init__.py` 里 **12 条 `from .browser import ...` / `from .flows import ...`
被算成了 `sms_tool -> sms_tool/upi_link`**，而它们**根本没跨目录**。

### 5.2 判据

一个 `__init__.py` 属于**它自己那个包**，永远不属于它的父目录。
正确写法要显式区分「包」与「模块」：

```python
def _directory_of(module: str, is_package: bool) -> str:
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in SUBPACKAGES:
        return f"{PKG}/{parts[1]}"
    return PKG
# is_package 必须由 path.name == "__init__.py" 提供，不能靠段数推断
```

同类的相对导入解析也必须带 `is_package`：
`from . import x` 在包的 `__init__.py` 里指的是**该包自身**，在普通模块里指的是**其所在包**。
两者差一层，正是这 12 条幻影边的来源。

**这条现已写进 fixture**：`test_package_relative_self_import_is_not_an_edge`
用 `sms_tool/accounts/__init__.py` 的 `from . import helper` 钉住它。

---

## 6. provider 解耦测量

### 6.1 现状：已完成 182/238

对 `services/protocol-payment/<provider>/*.py` 的**顶层函数**按名匹配
`services/protocol-payment/common/**` 的顶层函数，每个匹配落进**互斥**的一桶：

| 桶 | 定义 | 成本 |
|---|---|---|
| `delegating` | 去 docstring 后函数体是**单条 `return <call>(...)`**（或 `pass`） | 零 —— 已迁移 |
| `identical` | 体 + 签名**AST 逐字相同** | 纯重复，最便宜的收敛候选 |
| `divergent` | 同名、独立实现 | 收敛会改变生产行为 ⇒ 需取证 |

| provider 文件 | delegating | identical | divergent |
|---|---|---|---|
| `ideal/ideal_qr_extract.py` | **67** | 0 | 2 |
| `twint/twint_extract.py` | **67** | 0 | 2 |
| `blik/blik_qr_extract.py` | 39 | **8** | **32** |
| `kakao/kakao_extract.py` | 6 | 0 | 6 |
| `momo/momo_qr_extract.py` | 0 | 0 | 4 |
| `momo/ac_paylink_core.py` | 2 | 0 | 1 |
| `direct_card/direct_card_extract.py` | 1 | 0 | 1 |
| `pix/*`（3 文件） | 0 | 0 | 0 |
| **合计** | **182** | **8** | **48** |

### 6.2 `pix` 的「0」不是「已迁移」

`pix/` 三个文件与 `common/` **零同名函数** —— 它**不是**从同一上游 fork 出来的，
而是独立实现。⇒ 「divergent = 0」在这里含义是「**无可比对象**」，
不能读成「完全解耦」。这条已作为显式测试钉住
（`test_pix_has_no_same_named_definitions`），否则后人会把 0 当成绩。

### 6.3 分类器自身的缺陷：先比签名，还是只比体

第一版 `_normalised_ast()` **只序列化函数体**（并剥掉 docstring），理由是
「blik 的 `build_email(first, last)` 与 `common/` 的 `build_email(profile, first, last)` 签名不同」——
但我把结论写反了：**正因为签名不同才不能叫 identical**。

fixture 立刻抓到了它：

```text
SELF-TEST FAILED: alpha/resignatured.py: expected {'identical': 0, ..., 'divergent': 1},
                  got {'delegating': 0, 'identical': 1, 'divergent': 0}
```

修正后指纹 = **行为签名（实参名/个数/顺序/默认值/`*args`/`**kwargs`）+ 函数体**，
类型注解仍剥离（不影响行为）。修正后 `blik build_email` 正确落到 `divergent`，
`identical` 仍是 **8**（这 8 个的签名本来就一致）。

**这与 `docs/architecture.md` Rule 19 是同一件事的两种表述**：
Rule 19 说「同名不构成重复，直到 AST 证明」；本轮补了一句 ——
**AST 证明必须覆盖签名，否则「证明」本身是错的**。

### 6.4 与既有 `extractor_parity_report.py` 的分工

`extractor_parity_report.py`（CI 门禁）ratchet 的是**提取器之间的配对同构度**
（`ideal:blik` / `ideal:twint` / `twint:blik` / `momo:kakao`）。
新 ratchet 量的是**provider 对 `common/` 的收敛度**。两者不重叠：
前者防「两个 provider 互相分叉」，后者防「provider 又长出一份 `common/` 已拥有的实现」。

---

## 7. 死代码：pi-lens 的 10 项里 8 项是假阳性

`pilens_project_report` 的 DEAD WEIGHT 段落列了 10 个 `scripts/*.py`，
判据是「没有静态 importer」。但本仓的门禁是**按文件名**调用的：

| pi-lens 判为「死」 | 真实状态 | 证据 |
|---|---|---|
| `architecture_scan.py` | **CI 门禁** | `ci.yml:69` |
| `docs_consistency_scan.py` | **CI 门禁** | `ci.yml:73` |
| `extractor_parity_report.py` | **CI 门禁** | `ci.yml:70` |
| `bare_print_ratchet.py` | 测试 import | `tests/test_operator_output.py:25` |
| `config_key_ratchet.py` | 测试 import | `tests/test_config_key_ratchet.py` |
| `delayed_import_ratchet.py` | 测试 import + 文档引用 | `tests/test_delayed_import_ratchet.py`、`docs/architecture.md:66` |
| `endpoints_literal_ratchet.py` | 测试 import | `tests/test_endpoints_literal_ratchet.py:30` |
| `build_installer_py.py` | 发布记录引用 | `docs/releases/release-v2026.09.13.md:57` |

⇒ **8/10 假阳性**。这正是该段落自己写的免责声明
（「verify with a real usage search before deleting anything listed」）所要求的动作。

### 7.1 真死：4 项，已删

`docs/audits/audit-2026-08-31-cleanup-backlog.md:284-289` 早已立项：

> 7 个 filter-repo 事故脚本（`cmp_index_wt.py`、`extract_history_secrets.py`、
> `filter_replacements.py`、`fix_hardcoded_token.py`、`pick_final_replacements.py`、
> `scan_hardcoded_secrets.py`、`scan_sensitive_history.py`）随 `ee02fab` 入库，
> +395 行，**无任何代码/文档/CI 引用**，输入目录 `runtime/_filter_repo_work/` 已消失，不可再运行。

**但该清单已过时**：其中 `scan_hardcoded_secrets.py` 与 `scan_sensitive_history.py`
**后来被接进 CI**（`ci.yml:64-66`），`pick_final_replacements.py` **已被删除**。
逐个复核后真正可删的是 4 个：

| 文件 | 行的执行证据 | 判据 |
|---|---|---|
| `scripts/cmp_index_wt.py` | 26 行，`hashlib.md5` 比对 index vs 工作树，`FILES` 是硬编码的 5 个文件 | 已被 `git diff` 完全取代 |
| `scripts/extract_history_secrets.py` | `OUT = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.txt'` | 硬编码本机绝对路径 + 输入目录不存在 |
| `scripts/filter_replacements.py` | `SRC` / `DST` 同样是硬编码绝对路径 | 同上，且被删的前者是其上游 |
| `scripts/fix_hardcoded_token.py` | `TARGET = r'F:\epsoft\...\scripts\_diag_roxy_egress.py'` | **一次性修复已生效**：目标文件现在已是 `os.environ.get("ROXY_API_TOKEN", "")` |

**零引用复核**（`git grep` 全跟踪面，排除自身）：4 个文件名的命中只来自
**审计文档**与 `unused_import_baseline.json`；`docs/directory-map.md`、
`docs/architecture.md`、`docs/CONTEXT.md` **零命中** ⇒ 不在任何「活文档」的清单里。

**门禁连带**：`scripts/unused_import_baseline.json` 里 `cmp_index_wt.py` 与
`filter_replacements.py` 各 1 条记录，已同步移除并把 `total` 由 **357 → 355**
（ratchet 的既定语义是「只禁增长」，但删文件后顺手收紧是它的 `--update-baseline` 用法）。

### 7.2 三条「零文本引用」但**不删**的 operator 脚本

严格意义上，全跟踪面无任何提及的脚本有 3 个：

```text
scripts/probe_paypal_regions.py
scripts/rebuild_agent_identity_batch.py
scripts/recover_remail_tokens.py
```

**不删**，理由：`docs/directory-map.md` 定义 `scripts/` 的职责是
「Operator scripts；Small launch/setup helpers」—— **手工运行的脚本本来就不该有引用**。
三者都仍可运行、且 import 的是活模块：

```text
$ python -m scripts.probe_paypal_regions --help      → usage: ... [--emails] [--regions] ...
$ python scripts/rebuild_agent_identity_batch.py -h  → usage: ... [--input-report] [--all-local] ...
$ python -m scripts.recover_remail_tokens --help     → usage: ... emails [emails ...]
```

⚠️ 其中两个**必须以 `-m` 运行**：`probe_paypal_regions.py` 与 `recover_remail_tokens.py`
没有把仓库根加进 `sys.path`，直接 `python scripts/X.py` 会 `ModuleNotFoundError: No module named 'sms_tool'`。
`rebuild_agent_identity_batch.py` 自己 `sys.path.insert(0, ROOT)`，两种方式都行。
**记录在此以免后人把它当成 bug 顺手「修」掉或误判为死代码。**

---

## 8. 刷新 review graph 后暴露的真回归

`pilens_session_start` + 对 5 个新提交文件跑 `pilens_analyze` 后，
`scripts/registration_ab.py:449` 报出 **pyright 阻断**：

```text
reportOptionalMemberAccess: "splitlines" is not a known attribute of "None"
```

该文件是**最新提交 `c9386db` 新增的**，属真回归（不是历史存量）。
修复：`(__doc__ or "").splitlines()[0]`。

**同类写法还有 7 处**（`delayed_import_ratchet.py:91`、`extractor_parity_report.py:563`、
`mailbox_private_import_ratchet.py:259`、`module_coverage_check.py:118`、
`refresh_doc_symbol_lines.py:161`、`unused_import_ratchet.py:118`、
`runtime_retention.py:336`）。**本轮只修被门禁实际报出的那 1 处**，
其余保留 —— 它们当前没有门禁会看，一次性改 7 个文件的收益低于改动面。

**这条本身是「刷新图」这个动作的价值证明**：图停在 `6e4d890` 时，
最新提交引入的阻断项**任何门禁都没看到**（CI 只跑 `ruff check`，不含 pyright）。

---

## 9. 本轮新增的两个 ratchet

两者都遵循本仓既有形态：**stdlib-only**、docstring 说明「为什么存在」、
**每次调用先跑 fixture 自检**、`--detail` / `--update-baseline`、基线 JSON 带 `newline="\n"`、
退出码 `0` 通过 / `1` 超标 / `2` 缺基线 / `1` 自检失败。

### 9.1 `scripts/import_layer_ratchet.py`

- **量**：`sms_tool/` 下每条**模块级**跨目录 import 语句 = 1 条边，键为 `源目录 -> 目标目录`。
- **只禁增长**；函数内（惰性）导入**单独报告、不冻结**（理由见 §4.3）。
- **逐对基线**（31 对），不是单个总数 —— 否则「收敛 A 对」会掩盖「B 对新增一条反边」。
- **显式写明取舍**：同一对内部挪动导入（如 `cli.py` → `registration.py`，两者都是 `sms_tool`）**不可见**。
  这是有意的：架构单元是**目录关系**，同对内部重分配不改变它。`--detail` 会打印逐模块明细。
- **fixture 钉住**（`FIXTURE_TREE` + `FIXTURE_EXPECTED` + `FIXTURE_FORBIDDEN`）：
  绝对 / 相对 2 层 / 包内相对 / 同目录 / 自身 / 惰性 / wildcard / stdlib / 注释提及 / `_vendor` 排除。
  其中「包内相对不得成边」正是 §5 那 12 条幻影边的回归测试。

### 9.2 `scripts/provider_decoupling_ratchet.py`

- **量**：provider 顶层函数与 `common/` 同名者，落进 `delegating` / `identical` / `divergent` 三桶。
- **冻结 `identical` + `divergent`**，**不冻结 `delegating`**：`delegating` 是**迁移状态**，
  取消迁移会让 `delegating` 下降、另两桶上升，**已被捕获**；反过来冻结它等于禁止继续迁移。
- **逐文件基线**（11 个文件）。
- **fixture 钉住**：转发壳（含带 docstring 的）/ 逐字复制 / 独立实现 / **改签名** / 单个非调用 return /
  不同名 / `logs/` 排除，以及「三桶必须都出现」（防止分类器把一切倒进一桶而通过总数检查）。

### 9.3 接线方式

两者都由 `tests/test_*_ratchet.py` 随 **pytest** 执行（`ci.yml` 的
`python -m pytest -q --cov=...` 步骤），与 `config_key` / `delayed_import` /
`endpoints_literal` / `unused_import` 四个既有 ratchet 同构 —— **不新增 CI 步骤**。
测试分层与既有 ratchet 测试一致：
① 判据双向 fixture → ② 自检能变红 → ③ 真实树通过 + 关键事实钉住。

---

## 10. 建议的增量收敛顺序（未落地，供后续立项）

按「收益 / 风险」排序。**每一步都必须**：改完跑
`import_layer_ratchet.py --update-baseline`（收紧）+ 全量 `pytest`。

| 序 | 目标 | 反向边 | 风险 | 说明 |
|---|---|---|---|---|
| 1 | `sentinel/client.py` 的 4 条 → 把 `sentinel` 真正做成叶子 | 4 | 低 | sentinel 在概念上是叶子；4 条边集中在 1 个文件 |
| 2 | `providers/mailbox_remail.py` 的 8 条 | 8 | 中 | 与 §4.2 的门面边界相邻，需先确认哪几类名属于共享词汇层 |
| 3 | `cli.py` 的 9 条 → 把 `cli.py` / `cli_parsers/` 迁到独立入口层 | 9 | 中 | 属**结构搬迁**而非改导入：`cli` 是入口、`commands` 是适配层，两者本就不是上下层 |
| 4 | `registration.py` / `registration_drivers` 间的 4 条 | 4 | 中 | 09-22 §3.2 已记录「同层横跳」的同类问题，结论是「记录，不建议本轮动」 |
| 5 | `sms_tool` ↔ `providers` 的 19/19 **平局** | 19 | 高 | 平局说明这一对很可能是「同层两半」，需要的是**共同词汇层**（把 `mail_otp` / `mailbox_types` / `config` / `storage` 下沉），属架构决策 |

**顺序 1–4 合计 25 条**，可把 11 对降到 5 对左右。**顺序 5 需要先拍板**，
不应作为清理动作推进。

### 10.1 provider 侧的后续

| 序 | 目标 | 数量 | 风险 |
|---|---|---|---|
| 1 | blik 的 **8 个 `identical`** | 8 | 低 —— 签名与体都已逐字相同，收敛不动行为 |
| 2 | blik 的 **32 个 `divergent`** | 32 | 高 —— 需逐个取证；`build_email` 是已知的「签名就不同」 |
| 3 | kakao(6) / momo(4) / ac_paylink_core(1) / direct_card(1) | 12 | 中 |
| — | `pix` | 0 | **无可比对象**，不是「已迁移」（§6.2） |

---

## 11. 本轮自查出的 4 个自身缺陷（留档，避免重犯）

| # | 缺陷 | 被发现的方式 | 现已由什么钉住 |
|---|---|---|---|
| 1 | `dir_of` 不区分包与模块 ⇒ 12 条幻影边（§5） | 与 pi-lens 的 `sms_tool -> upi_link (2)` 对不上，逐条读行 | `test_package_relative_self_import_is_not_an_edge` |
| 2 | 「模块级 vs 函数内」分类器恒真 ⇒ 29 条惰性边被算进可减少面（§2.2） | 换成 `id()` 归属判定后总数从 92 变 60 | `test_function_local_import_is_delayed_not_module_scope` |
| 3 | provider 指纹只比函数体、不比签名 ⇒ 改签名的函数被误判 `identical`（§6.3） | **新 ratchet 的 fixture 在第一次运行时直接报错** | `test_resignatured_body_is_divergent_not_identical` |
| 4 | pi-lens 的 10 项「死代码」未先做「按文件名调用」的检索 | 逐项 `git grep` + 读 `ci.yml` / `.githooks/pre-commit` | 无自动化（这类判断依赖门禁清单，见 §7） |

**共性**：4 个里有 3 个是**判定器实现缺陷**，而不是观察错误 ——
而且第 3 个是**被自己的 fixture 在运行的第一秒抓住的**，不是被人工复核抓住的。
这与 09-22 §5.3「消费通道无法穷举、必须由失败测试反推漏检面」是同一条教训的延伸：
**能自动变红的 fixture，比更仔细的作者更可靠。**

---

## 12. 交付物清单

| 类型 | 路径 |
|---|---|
| 新门禁 | `scripts/import_layer_ratchet.py` |
| 新基线 | `scripts/import_layer_baseline.json`（288 边 / 31 对 / 11 互指 / 60 反向 / 182 惰性） |
| 新测试 | `tests/test_import_layer_ratchet.py`（19 项） |
| 新门禁 | `scripts/provider_decoupling_ratchet.py` |
| 新基线 | `scripts/provider_decoupling_baseline.json`（182 转发 / 8 逐字 / 48 独立） |
| 新测试 | `tests/test_provider_decoupling_ratchet.py`（20 项） |
| 收紧 | `scripts/unused_import_baseline.json`（357 → 355，`total` + 2 条 `per_file`） |
| 删除 | `scripts/cmp_index_wt.py`、`scripts/extract_history_secrets.py`、`scripts/filter_replacements.py`、`scripts/fix_hardcoded_token.py` |
| 修复 | `scripts/registration_ab.py:449`（`__doc__` 可能为 `None`） |
| 本报告 | `docs/audits/scan-2026-10-04-import-cycle-decomposition.md` |
| 索引 | `docs/audits/README.md`（计数 48 → 49） |

# 落地记录：scan-2026-09-07 审计清单 1→9

- 来源：`docs/audits/scan-2026-09-07-module-optimization-review.md` §6
- 日期：2026-09-07
- 回归：**pytest 2937 passed / 6 skipped / 547 subtests，RC=0**（基线 2891，+46 为本轮新增用例）
- 架构门禁：`scripts/architecture_scan.py` 通过；延迟 import 棘轮 402 → 402（+0）

---

## 结果总表

| # | 落地内容 | 主要改动 | 验证 |
|---|---|---|---|
| 1 | 断 `http_client → registration_policy` 顶层反向依赖 | 新建 `sms_tool/backoff.py`；`TERMINAL_ERROR_MARKERS` 与 `is_terminal_registration_error()` 下沉到 `error_classification.py` | 131 passed / 30 subtests |
| 2 | `RegistrationOperations` 59 字段语义分组 | 新增 `OPERATION_GROUPS`（8 组）；`bind()` 缺键一次性列全 | 42 passed |
| 3 | 统一查优惠三入口的 `proxy_pool` 与 `trial_eligible` 口径 | `check_registered_promotions(proxy_pool=...)` 透传；`trial_eligible` 由 `refresh_promotion_statuses` 统一产出 | 43 passed |
| 4 | `run_browser_registration()` 抽可测决策 | 新建 `browser_flow/decisions.py`（7 个纯函数） | 136 passed / 36 subtests |
| 5 | 架构快照 Boundary Rules 提炼回 `docs/architecture.md` | 新增 `## Boundary Rule Checks`，15 条规则各带 grep 判据 | 文档 5,606 → 11,511 字节 |
| 6 | 补扇入 ≥10 模块 docstring | `mailbox.py`、`desktop_ipc.py` | 114 passed / 16 subtests |
| 7 | `providers/` 命名统一 | 3 个低层模块改名 `*_client` | 171 passed + 门禁 |
| 8 | `account_*` 收进 `accounts/` 子包 | 15 个文件硬移动，**不留转发壳** | 全量回归 |
| 9 | 延迟 import 纳入棘轮 | `scripts/delayed_import_ratchet.py` + 基线 | 6 passed |

---

## 第 1 项：断反向依赖的正确姿势

不是把 import 挪进函数（那是掩盖症状），而是把**纯函数下沉**：

- 纯数学 → 新建 `sms_tool/backoff.py`（`transport_backoff` / `bounded_cooldown`，stdlib only）
- 纯分类 → `error_classification.py` 新增 `TERMINAL_ERROR_MARKERS` 与 `is_terminal_registration_error()`

等价性说明（已写进代码注释，**禁止"还原"回 policy 调用**）：
`registration_retry_decision(error, failure_class="network").retryable` 与
`is_terminal_registration_error(error)` 等价 —— 因为 `"network"` 在 `RETRYABLE_CLASSES` 中，
`.retryable` 退化为 `not terminal`。

`registration_policy.py` 从 58 行缩到 46 行，改为 re-export。

## 第 2 项：为什么 59 字段不能改成嵌套 dataclass

`tests/test_registration_operations.py:22-31` 用 AST 扫 `registration_handlers.py` 里的
`r.<name>` 读取，断言与 `fields(RegistrationOperations)` **集合相等**。
改成嵌套结构会让平铺属性消失 → 测试红。

因此只做**排序 + 注释 + 机器可读分区**：

```
OPERATION_GROUPS = {
    "config": (4 字段), "http_transport": (9), "sentinel_fingerprint": (9),
    "auth_session": (6), "signup_navigation": (8), "generated_identity": (5),
    "email_otp_mailbox": (8), "checkpoint_outcome_telemetry": (10),
}
```

新增用例钉住「精确分区 + 每组在 dataclass 中必须连续」。
`bind()` 的缺键报错从裸 `KeyError`（只报第一个）改为一次列出全部缺失。

## 第 3 项：查优惠口径

两个真问题：

1. `commands/registration.py:check_registered_promotions()` **静默丢弃**调用方传入的代理池
   （`refresh_promotion_statuses` 根本没这个形参）→ 新增 `proxy_pool` 并透传，`cli.py:791` 同步。
2. `refresh_promotion_statuses()` **根本不返回** `trial_eligible` 键 —— 只有
   `check_registered_promotions` 自己本地重算一次 → 改为由前者统一产出，后者直接读。

## 第 4 项：不切分 571 行，只抽决策

`run_browser_registration()` 是**"顺序即语义"的编排脚本**（首屏预热故意放 2FA 之后、
注册国家镜像协议路径补漏、OTP 后跑两轮 reload），机械切分会破坏时序约束。

新建 `browser_flow/decisions.py`，7 个纯函数：
`attempt_number` / `browser_profile_key` / `playwright_viewport` /
`aligned_locale_timezone` / `geo_affinity_country` /
`registration_state_and_basis` / `needs_chat_base_navigation`。

其中 `registration_state_and_basis()` 把原来两个独立表达式（易漂移）改成**成对产出**，
关键断言 `(True, True) → ("at_probe_pending", "at_http_200")`。
端到端 fake 浏览器 9 个用例全绿，证明行为未变。

## 第 7 项：providers 不是"命名混乱"，是分层没体现在后缀上

改前/改后：

| 层 | 改前 | 改后 |
|---|---|---|
| 低层客户端 | `providers/cfworker_mailbox.py` (591) | `providers/cfworker_client.py` |
| 上层流程 | `providers/mailbox_cfworker.py` (151) | 不变 |
| 顶层兼容壳 | `sms_tool/mailbox_cfworker.py` | 不变 |
| 低层客户端 | `providers/smailr_mailbox.py` (417) | `providers/smailr_client.py` |
| 上层流程 | `providers/mailbox_smailr.py` (322) | 不变 |
| 低层客户端 | `providers/outlook_imap.py` | `providers/outlook_imap_client.py` |

**命名契约**（写进 `cfworker_client.py` docstring 与 `docs/directory-map.md`）：
低层 `*_client`，上层 `mailbox_*`，`*_client` 不得 import `providers.mailbox_*`。

两个必须注意的点：

1. `outlook_imap` 内部把 `"outlook_imap"` 当**字符串值**用（`_source` 字段、
   配置键 `outlook_imap_enabled` / `outlook_imap_folders`、函数 `_outlook_imap_enabled`）。
   全局 sed 会误伤 → 只改 import 行与 `outlook_imap.` 属性访问，**文件内字符串一个不动**。
2. `sms_tool/mailbox_gmail.py:18` 从 `providers/outlook_imap_client` 取
   `discover_imap_folders` —— 低层之间互相复用合法，**别误判成反向依赖**。

## 第 8 项：为什么没按审计报告说的"留壳"

审计报告原文：`account_*` 15 个文件收进 `accounts/` 子包（**高风险**，需壳+渐进）。

**这个药方是反的。** 58 处测试 patch `sms_tool.account_X.<name>`，
patch 生效靠**在"调用方读取的那个模块对象"上改属性**。若 `sms_tool/account_X.py` 留作转发壳：

- 壳的 globals ≠ 真正执行代码的模块的 globals
- 58 处 patch 全部**静默失效**：测试全绿，一个都没测到
- 这种失败不会报红 —— 比红更难发现

反过来，**硬移动 + 删掉旧文件是安全的**：旧模块不存在，
`patch("sms_tool.account_scan.xxx")` 直接抛 `ModuleNotFoundError`，漏改一处立刻炸。

实测替换量：相对 import 29 / 绝对 19 / 字符串 74 / `from sms_tool import` 9 / 包内层级调整 78。

### 移动后的 import 改写规则

`accounts/` 外：

```
from .account_X import          -> from .accounts.account_X import
from ..account_X import         -> from ..accounts.account_X import
from sms_tool.account_X import  -> from sms_tool.accounts.account_X import
from sms_tool import account_X  -> from sms_tool.accounts import account_X
patch("sms_tool.account_X...")  -> patch("sms_tool.accounts.account_X...")
```

`accounts/` 内（包层级下移一级）：

```
from .account_X import   -> 不变（一起搬的，同级）
from .<其它模块> import  -> from ..<其它模块> import
```

---

## 本轮踩到并修掉的三个坑

### 1. AST `ImportFrom.module` 不含前导点

```python
# ast.ImportFrom(module='mailbox_cfworker', level=1)   ← 点数在 level，不在 module
node.module.startswith(".mailbox_")                    # 永远 False
node.level == 1 and node.module.startswith("mailbox_") # 正解
```

新架构门禁第一版因此**静默失效** —— 是**负向测试**发现的（临时插一行违规 import，
确认 rc=1 再还原）。门禁上线必须配负向测试。

另：规则不能只按 `mailbox_` 前缀。`sms_tool/mailbox_poll.py` 是三家供应商**共用的轮询模板**
（`mailbox_cfworker:143` / `mailbox_strategies:263` / `providers/smailr_mailbox:409`），
属共享基础设施，低层 client 复用它合法。判据必须落在**解析后的目标**。

### 2. `tests/test_unbound_name_guard.py` 的潜伏 bug

相对 import 解析里 `candidate_pkg = base / module / "__init__.py"` 漏了
`replace(".", "/")` —— 带点的嵌套包（`registration_drivers.external_sessions`）
被当成单级目录名，永远判"不存在"。

以前因为这些文件直接在 `sms_tool/` 根下（守卫显式跳过该目录）没暴露；
第 8 项把它们移进 `accounts/` 子包后立刻炸出 2 条**误报**。已修，
并负向验证守卫仍能抓真不存在的目标。

### 3. grep 残留检查不够，必须有 import 冒烟

改写脚本只处理了 `from X import name` 形态，漏了两种**模块导入**形态：

- `sms_tool/registration.py:185` `from . import account_liveness`
- `sms_tool/registration_drivers/browser_flow/orchestrator.py:20`
  `from ... import ( account_identity, ... )` 括号多行里的单个名字

残留 grep 当时报告"全空"，是 `importlib.import_module` 冒烟把它们逮出来的。

### 4. 机械改写脚本把行尾改成了 LF（已知陷阱，仍然踩了）

`open(r).read()` 通用换行把 `\r\n` 变 `\n`、`open(w, newline="")` 不转回去，
导致 85 个被改写文件整片变 LF。

两个当时没想到的点：

1. **本仓行尾是混合的** —— 153 个文本文件本来就是 LF，其余 CRLF
   （`core.autocrlf=true`，无 `.gitattributes`）。所以**不能一刀切**转 CRLF。
2. **不能用 `git show HEAD:<path>` 判断原行尾** —— 它返回的是 autocrlf **归一化后的 blob，
   恒为 LF**。第一版修复脚本照它"匹配 HEAD 风格"，结果退化成"全转 LF"，
   反而把 9 个原本 CRLF 的文件改坏了。

正解：只按"工作副本当前是不是纯 LF"判断，把**自己弄脏的那批**统一转回 CRLF，
不碰没动过的文件。已按此修复 85 个文件，脏文件里纯 LF 归零。

**验证**：修复前后 `git diff HEAD --numstat` 均为 `+1565 / -391`，**完全一致** ——
证明零内容影响（autocrlf=true 下 LF/CRLF 产生的 diff 相同）。
修复后全量回归仍是 2937 passed。

---

## 新增门禁（防回归）

`scripts/architecture_scan.py`：

1. `sms_tool/` 根下不得再出现 `account_*.py`
2. 必须存在 `sms_tool/accounts/__init__.py`
3. `providers/*_client.py` 不得 import `providers.mailbox_*`

三条均做过负向测试（插违规 → rc=1 → 还原 → rc=0）。

---

## 清理

| 项 | 状态 |
|---|---|
| `runtime/browser_profiles`（20GB，216 个 camoufox profile） | 已删除（老板拍板"直接删，不放回收站"） |
| `%TEMP%\pytest-of-29514.bak-20260903b` | 已删 |
| `runtime/*20260823*`（19 个过期产物） | 已删（先 grep 确认 0 引用） |
| `__pycache__ / *.pyc` | 未动（老板未选） |
| `dist/` 260MB | 未动（老板未选） |

删除前核实：无 camoufox/firefox/playwright/cloak 活跃进程；`profiles.py:11` 只拼路径、
profile 由驱动按需自建；目录 0 入库。
**已知代价**：216 个 profile 里 214 个是 2026-09 的近期注册产物，
删后同账号重试会**重新走登录**（cookie 丢失），不损坏账号但多花时间。

### 只读扫描推翻的初始假设

需求原文提到"清理测试、备份、临时等多余文件"，实测：

- 仓库里**没有任何** `.bak` / `.orig` / `.old` / `.tmp` 备份文件（git 已跟踪 + 未跟踪全搜）
- `tests/` **没有**孤儿或重复测试文件（218 个入库文件里仅 2 个是测试依赖的辅助 fakes：
  `paypal_dom_fakes.py` / `paypal_reverse_fakes.py`）
- 真正的"临时"大头就是 `runtime/browser_profiles`

---

## 遗留

- C# 侧本轮**零改动**（未改任何 `.cs`），dotnet 253 passed 基线未复跑。
- 历史审计快照 `docs/audits/*` 中的旧模块路径**故意不改**（记录当时的真实状态）。
- `docs/releases/*` 同理。
- `dist/` 里有一份 `sms_tool` 的构建副本（未入库），下次构建会刷新。

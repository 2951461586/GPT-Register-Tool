# 协议注册 / 支付链接提取 / 代理池·指纹库 —— 修复与优化方案

- **日期**：2026-09-30
- **状态**：**§2 已落地**（工作区改动，未提交）；**§3 待拍板**（未动代码）；§4 为明确不做项
- **范围**：协议注册（`auth_flow/`、`registration_handlers.py`）、支付链接提取（`pay_link/`、`upi_link/`、`paypal_link/`、`payment_egress.py`）、代理池与指纹库（`proxy_*.py`、`*fingerprint*.py`、`geo/`），以及三处全仓门禁（`sensitive_field_scan` / `delayed_import_ratchet` / `ruff format`）
- **相关**：`docs/architecture.md` §Known function-level lazy cycles、`PROXY_GUIDE.md` §Unified Proxy Registry、`docs/current/protocol-registration.md`、`plan-2026-09-17-protocol-payment-extractor-consolidation.md`

---

## 1. 结论先行

1. **三个模块本身没有结构性缺陷。** 精确导入图（每条真实 `import` 语句，含函数体内，按包布局解析）显示 `sms_tool` 在 **import time 是一个 DAG**：278 模块 / 1103 边 / 268 个 SCC，非平凡环只有 **6 个、共 16 模块（5.8%）**，全部由函数内延迟导入或 `if TYPE_CHECKING` 打断。pi-lens review graph 报的「483 边、跨 14 子包的巨型环」是 **name-only 解析（82%）的假象**（该图 exact 仅 18%）。
2. **真正的缺陷是三处门禁失效 + 一处出口门禁缺口**，全部已在本轮定位；其中 §2.1 与 §2.6 是**会让真实链路出错**的两条，其余是死代码/文档/基线卫生。
3. **最大的优化杠杆是 `pay_link/` 的机械拆分残留**：8 个文件占全仓未用 import 基线的 **55%（236/427）**，其中两个分片声明 24 个模块依赖却只用 4 个。这不是观感问题——它让 `pay_link/` 在图里看起来是 7 个紧耦合模块，实际是 `__init__` + `core` + `registry` + 2 个近乎叶子 + `base`。
4. **AST 证实的真重复只有 4 组**（`_as_bool` / `_load_json` / `_emit` 各 2 处，`_as_int` 11 定义/6 体）；同名的 `_request`（7/7 不同）与 `create_checkout`（8/8 不同）**不是**重复，与 `docs/architecture.md` 的既有规则一致。
5. **vulture 的 21 条 80% 命中基本是假阳性**（`HTMLParser` 回调、仅测试引用、对外 API），不应按它清理。

---

## 2. 已落地

> 复验基线：`pytest`（全量）**5591 passed / 7 skipped**；6 个 ratchet 全绿；`ruff check`（CI 门禁）全绿；`architecture_scan` / `docs_consistency_scan` / `module_coverage_check` / `config_schema_check` / `ipc_schema_check` / `extractor_parity_report` / `precommit_guard` / `scan_hardcoded_secrets` 全绿。

### 2.1 P0 — `native_upi` 不过支付出口门禁

**缺陷**：`payment_egress.assert_egress_countries` 只在**子进程**适配器路径调用（`pay_link/adapters.py:139` 的 `_prepare_extractor`）。`native_upi` 是**函数适配器**，完全绕过它，于是 UPI 链路用池里给的任意出口创建了真实 Checkout session。`upi_link/pipeline.py` 自己的注释写着「真实出口门禁由上层负责」——而上层从来没做。`PROXY_GUIDE.md` 也把这条列为**只能手工探测**的已知缺口。

**修法**（`sms_tool/upi_link/pipeline.py`，+61 行）：门禁放在 **retarget 之后、Stage 1 创建 Checkout 之前**，而不是 adapter 入口。

- **必须 post-retarget**：`_upi_retarget_region` 会把 region-tag 凭据改写成账单国。在 retarget 前探测，会把「retarget 马上就要改对」的池误判为违规——那是回归。
- **一处覆盖两条路径**：放在 `generate_upi_qr_link` 内，`native_upi` 适配器与 CLI `--generate-upi-qr` 同时获得门禁。
- `stages=("checkout", "stripe_init", "approve")` 显式 opt-in：门禁默认 stage 集 `{checkout, approve, promotion}` **不含 `stripe_init`**，不写会漏掉 Stripe init 那一跳。
- 失败返回 `EgressCheckError.to_result("upi")`，与 `_prepare_extractor` 同形（`error_stage="preparing_proxy"`）。

**副作用修复**：`tests/test_gen_pp_link.py` 两个 full-pipeline UPI 测试开始触发 `conftest` 的真实网络守卫（`real network probe reached in tests: … 'socks5h://jp-checkout:1080', expected='JP'`）。按 conftest 自己的指示（"fake the probe explicitly"）在既有的 seam 助手 `_patch_upi` 内 stub 探针，并保留 `egress_probe=None` 的 opt-out。

**新增测试**：`tests/test_upi_egress_gate.py`（5 例，含「探的是 retarget 后的代理」这条关键回归）。

### 2.2 P1 — 两处死代码

| 位置 | 内容 | 证据 |
| --- | --- | --- |
| `sms_tool/session_refresh.py` | `return {}` 之后 7 行**逐字重复**的前一块 | vulture 100% `unreachable code after 'return'` |
| `sms_tool/auth_flow/steps.py` | `logger = logging.getLogger(__name__)` 写了两遍 | AST |

### 2.3 P1 — mandate 状态机的只写死键

**缺陷**：`mandate_state: dict[str, bool] = {"attempted": False, "ok": False}` 中 `attempted` 只写不读（写入 `pipeline.py:1158`，唯一 guard 读 `["ok"]`）。它看起来像「已尝试过就别再试」的闸门，实际不是——`ok=False` 时后续 rescue 仍会重试（**这个行为是有意的**：批准前 `checkout.session.setup_intent` 是 `null`，真正能补交的时机在 Stage 7 的 rescue）。

**修法**：换成显式布尔 `mandate_ok`（`nonlocal approval_data, mandate_ok`），并把「失败的尝试刻意允许重试」写进注释，消除误导。行为逐位不变。

### 2.4 P2 — `docs/architecture.md` 漏登记一个延迟环

文档的「Known function-level lazy cycles」表登记了 4 组 pair 并写「第五组必须补上同样证据」。精确分析发现**还漏了一个用单条延迟边闭合的五模块环**：

```text
accounts.account_identity → fingerprint_pool → paypal_proxy → payment_routing → proxy_routing → accounts.account_identity
        [top]                  [lazy]          [lazy]           [top]           [lazy]
```

漏掉的那条边是 `proxy_routing._saved_registration_proxy` 里的 `from .accounts.account_identity import resolve_account_proxy`（只在 opt-in 的 `account_health.use_registration_affinity` 路径上读）。`delayed_import_ratchet` 只 gate **数量**，不 gate **文档清单**。

**修法**：补表格一行（逐边标注 top/lazy 与不抬到模块级的理由）；表头 `Pair` → `Pair / cycle`；页脚「第五个 pair」改为「任何进一步闭合环的延迟边」；并加一段说明**权威来源是精确导入图，不是 review graph**（附 483 边 vs 6 环的对照）。

### 2.5 P2 — 基线与格式

- **`scripts/delayed_import_baseline.json`**：`fingerprint_pool 5→4`、`proxy_edge_probe 0→1`、`total 411→411`。此前逐文件失真（总数靠 −1 抵消 +1），再动一处同类导入就会红 CI。现在逐文件与工作树**完全一致**（`per-file mismatches: none`）。`proxy_edge_probe` 那条不是「用基线盖住新增」：§2.6 已在代码里写下理由，而脚本原文就是 *"Delete an import **or justify a baseline bump**"*。
- **`ruff format`**：`session_refresh.py` / `auth_flow/steps.py` 的既有风格漂移已清（由 pi-lens 对改动文件自动执行）。**未做全仓清扫**——见 §3.7。

### 2.6 P0 — 安全门禁 `sensitive_field_scan.py` 在正确的树上报红

**两个不同性质的根因**：

**(a) 项目路径写死。** 断言写死 `SmsWorkbench/SmsWorkbench.csproj`，但提交 **`aeaea75`**（协议支付规划器下沉 `SmsWorkbench.Contracts`）把 `SensitiveDataSanitizer.cs` **和**嵌入一起搬走了：

```xml
SmsWorkbench.Contracts/SmsWorkbench.Contracts.csproj:20:
  <EmbeddedResource Include="..\sensitive_policy.json" LogicalName="SmsWorkbench.sensitive_policy.json" />
```

于是闸门长期在正确的树上失败——**红灯久了没人看，真泄漏跟着过闸**。改为解析「编译 sanitizer 的那个工程」，把嵌入与其**消费者**绑在一起（`SensitiveDataSanitizer` 读的是**自己**程序集的清单项），比原来更强，并顺带修掉原来对文件缺失会抛未捕获 `FileNotFoundError` 的隐患。

**(b) 对产物误报。** `ARTIFACT_SECRET` 把两类**不是值的东西**当成值：

| 文件 | 匹配到的「密钥」 | 实际是什么 |
| --- | --- | --- |
| `runtime/account_relogin_guard.json` | `refresh_token=missing_refresh_token\|web_session=…` | `last_shape` 字段的**失败词表**（"这次没拿到"的标签） |
| `runtime/_retired_*/_phases*.json` | `totp_secret=totp_secret` | 被引号包住的**源码片段**里的关键字参数自回显 |

修法是在既有占位符负向断言上补 `missing_` 与「值等于键本身」（`(?P<artifact_key>…)` + `(?!(?P=artifact_key)(?![A-Za-z0-9_]))`）。**新旧正则逐例对照：10 个真凭据形态 regressions = 0，6 个占位符形态 2 → 0。**

**(c) 补上零测试覆盖。** 该闸门此前**零测试**（`tests/test_sensitive_policy_coverage.py` 测的是 sanitizer，不是这个扫描器），这正是它能烂掉而无人察觉的原因。新增 `tests/test_sensitive_field_scan.py`（**24 例**）：嵌入检查（真树通过 / owner 必须是 `SmsWorkbench.Contracts.csproj` / 缺失要报 / 放错工程要报 / 找不到 sanitizer 要报，后三个用临时树，否则「抓到」这一侧永远验证不到）+ 10 个真凭据形态必须命中 + 10 个占位符形态必须不命中。

---

## 3. 执行结果（2026-09-30 收口）

| # | 项 | 结果 |
| --- | --- | --- |
| 3.1 | `ARTIFACT_SECRET` 漏检 JSON | ✅ 已落地 |
| 3.2 | `sensitive_policy.json` 工程归属 | ✅ 已落地（Contracts 独占，含证据） |
| 3.3 | `pay_link/` import 头修剪 | ❌ **尝试两次后回滚** —— 见 3.3 的新证据 |
| 3.4 | 重复 helper 合并 | ⏸ 未执行（与 3.3 同一风险类别） |
| 3.5 | 其余 function adapter 过门禁 | ✅ 已落地 |
| 3.6 | `UPI_LOCAL_MANDATE_ENABLED` 可配 | ✅ 已落地 |
| 3.7 | 全仓 `ruff format` | ⏸ 未执行（需独立提交，否则整个工作区不可评审） |

### 3.1 P1 — `ARTIFACT_SECRET` 漏检 JSON 形态（结构性）—— ✅ 已落地

**缺陷**：键备选式后紧跟 `\s*[=:]\s*`，而 JSON 里键与分隔符之间有一个 `"`，整类失配：

```text
miss  '"access_token": "eyJhbGciOiJIUzI1NiJ9.abc"'
miss  '"ba_token":"BA-9F2K7QX"'
HIT   'access_token=eyJhbGciOiJIUzI1NiJ9.abc'     ← 只认 key=value
```

而产物（`runtime/*.json`、发布包报告）几乎全是 JSON。

**量测**：改成 JSON-aware（键后允许一个可选引号）后，本机 `runtime/` 只多出 **15 条命中，且全部是假阳性**——`"refresh_token": s.oauth_refresh_token` 这类源码回显、`"ba_token": false` / `"refresh_token": true` 这类配置布尔值。

**为什么不能只调正则**：要区分「JSON 里的真 token」和「日志里的代码回显」，需要**按 `sensitive_policy.json` 的键名解析结构化产物**，而不是继续刮行正则。这是设计变更。

**影响面**：CI 里 `runtime/`、`logs/` 在全新检出时并不存在，所以这个闸门在 CI 中基本空转（这也是为什么 §2.6(a) 是唯一的 CI 阻塞点）；真正受影响的是发布流程 `--artifacts dist/installer/package`。

**方案**：新增 `_iter_structured_artifact_findings(path)`——`json.loads` / JSONL 逐行解析，递归取键，用 `sms_tool.sanitizer.SENSITIVE_POLICY` 的 `sensitive_keys` + `sensitive_key_fragments` 判定，命中即报 `path:key_path`。行正则保留为**兜底**（非结构化 `.log` / `.txt`）。

**验收**：① 上述 15 条假阳性仍为 0；② 人造样本 `{"access_token":"eyJ…"}` 必须报；③ `runtime/` 上仍为 0 命中。**工作量**：0.5–1 天。

### 3.2 P2 — `sensitive_policy.json` 的工程归属 —— ✅ 已落地（Contracts 独占）

现在的事实是 **`SmsWorkbench.Contracts` 独占**嵌入（§2.6a 把这个事实固化进了检查）。若本意是让主 app 工程也嵌入，那是在 `SmsWorkbench/SmsWorkbench.csproj` 加一行 `EmbeddedResource` 的另一处改动。**需拍板**：独占（现状，检查已固化）还是双份。

### 3.3 P1 — `pay_link/` 机械拆分的 import 头残留 —— ❌ 尝试后回滚

| 模块 | 声明的模块依赖 | 实际使用 | 未用 import 名 |
| --- | --- | --- | --- |
| `payment_link_manager.py`（兼容壳） | 24 | **0** | 43 |
| `pay_link/persistence.py` | 24 | **4** | 39 |
| `pay_link/normalize.py` | 24 | **4** | 39 |
| `pay_link/registry.py` | 27 | 9 | 31 |
| `pay_link/adapters.py` | 29 | 16 | 30 |
| `pay_link/core.py` | 29 | 13 | 28 |
| `pay_link/base.py` | 23 | 11 | 27 |

```text
仓库未用 import 基线总量：427      pay_link/ + 兼容壳：236（55%）      修剪后：191
```

**为什么值得做**：名字解析型依赖图只能看见「声明了什么」，所以这 8 个文件在图里看着像 7 个紧耦合模块，实际是 `__init__` + `core` + `registry` + 2 个近叶子 + `base`。修剪会**同时**降低未用 import 债、让真实依赖图可见、让 `pay_link/` 从「目录」变成真正的边界。

**⚠️ 不能盲删**：`pyproject.toml` 明文记录，`unused` 是对**每一条消费通道**的断言，历史上两次删 456 个都回滚（3→6→8 条通道，逐轮发现）。**必须按既有 ratchet 流程**：先逐条验证消费通道（含 `patch("sms_tool.pay_link.*")` 这类测试目标），再删、再 `--update-baseline`（删除之后的合规动作）。**工作量**：1–2 天。

#### 3.3.1 本次尝试失败记录（2026-09-30，已回滚）

两轮尝试，两次都产生真实破损，均已回滚（工作区已回到 pre-trim 状态，`ruff check` 绿）。这两次失败**不是同一原因**，而且第二次暴露了一条 `unused_import_ratchet.py` 的 8 条通道清单**没有列**的通道。

**尝试 1：`ruff check --select F401 --fix`（234 删）。** 立即可见的破损：

```text
sms_tool/payment_batch.py:22: from .payment_link_manager import (
E ImportError: cannot import name 'parse_proxy_pool' from 'sms_tool.payment_link_manager'
```

`parse_proxy_pool` **不在** `pay_link.__all__` 里。`payment_link_manager.py` 只是因为自己有一行 `from .payment_routing import parse_proxy_pool` 才**顺带**暴露了它，而 `payment_batch.py` / `payment_batch_setup.py` 正是从这个门面取它。删掉那行 ⇒ 三个测试模块在 collection 阶段就 ImportError。

> 🔴 **这是通道清单的缺口，建议补进去**：ratchet 列的 8 条通道都是针对**定义模块**的消费面；**「另一个模块从本模块 import 这个名字」**（含经由 re-export 门面）不在其中。判据：`consumed[module_path(target)] ∩ bound_names`，其中 `module_path` 需按 `from X import ...` 的相对层级解析。

**尝试 2：带通道建模的 AST 精确修剪（216 删）。** 已按上条补上门面消费通道（`parse_proxy_pool`、`subprocess`、`current_config_data`、`runtime_file` 都正确保留），并且用「ruff 的 column 在**有 `as` 时指向绑定名**、否则指向源名」的规则逐 `alias`（而非逐行）定位——这一点已单独验证过，映射 100% 命中。但结果仍在 CI 门禁上爆出两处：

```text
F821 Undefined name `PaymentRoutePlan`   --> sms_tool/pay_link/registry.py:314:29
F821 Undefined name `PaymentRoutePlanner` --> sms_tool/pay_link/registry.py:317:18
```

而单独对 pre-trim 文件跑 `ruff --select F401` 时，这两个名字**从未被标记**（只有 `coerce_approve_country` 与 `parse_proxy_pool` 被标记）。也就是说：删除集里出现了一个按我的模型不应存在的名字。**根因未完全定位**，而“无法解释自己删了什么”本身就是不继续下去的理由。

**结论与建议**：

1. **§3.3 维持 ratchet 状态**（`pyproject.toml` 当初的判断是对的：这不是可自动化的一次性清理）。
2. 若仍要做，只能**逐文件、逐提交**：每删一个文件就跑 `ruff check`（F821/E9 是有效的过度删除探测器，`ruff check` 本身不选 F401）+ 全量测试，绿灯才进下一个文件。
3. 把 **`pay_link/__init__.py` 的 `__all__` 当作地板**（其中任何名字都不得删），并把上面那条门面通道补进 `unused_import_ratchet.py` 的文档清单。
4. 已量化收益不变：修剪后基线 427 → **191**（-55%），对名字解析型依赖图的收益也成立——只是代价必须按文件承担。

### 3.4 P2 — AST 证实的重复 helper 合并 —— ⏸ 未执行

| 函数 | 位置 | 说明 |
| --- | --- | --- |
| `_as_bool` ×2 | `pay_link/base.py:60` = `payment_contracts.py:103` | body 逐字节相同 |
| `_load_json` ×2 | `upi_link/config.py:16` = `paypal_link/gen_link.py:197` | body 相同；各自还各有一份 `DEFAULT_CONFIG_PATH` |
| `_emit` ×2 | `paypal_link/gen_link.py:187` = `upi_link/env.py:8` | body 相同 |
| `_as_int` 11 定义 / **6** 体 | `codex_export`+`codex_oauth`+`store/normalize` ×3；`payment_auth`+`token_telemetry`+`accounts/account_liveness` ×3；`proxy_edge_probe`+`accounts/account_identity` ×2 | 5 组重复 |

**风险**：这些是**私有** helper，可能被测试当作 patch 目标（`patch("sms_tool.upi_link.env._emit")`）。合并前须逐条 grep patch 目标。按 `docs/architecture.md` 的规则，AST 已证明的重复才算重复——上表已证明。

### 3.5 P2 — 其余 function adapter 同样不过门禁 —— ✅ 已落地

§2.1 只修了 `native_upi`。同样不过 `assert_egress_countries` 的还有 **`native_paypal`**（`paypal_link/gen_link.py`）、**`wallet`**、**`gcash_custom`**、**`regional_wallet`**。PayPal 有自己的 `paypal.preflight_proxy_check`，但那是不同的判据（探活 vs 国家匹配）。**需拍板**：是逐个补，还是在 `pay_link/core.py` 的计划完成处统一补一道（后者会连带改变 PayPal 行为，需评估与其 preflight 的重复探测，探针缓存按 `(proxy_key, expected)` 去重可缓解）。

### 3.6 P3 — `UPI_LOCAL_MANDATE_ENABLED` 不可按次关闭 —— ✅ 已落地

`constants.py:73` 硬编码 `True` 且只被读一次；测试里唯一断言是 `assert … is True`。**两个「mandate」开关语义不同且命名易混**：

- `UPI_LOCAL_MANDATE_ENABLED` = 本地 mandate 阶段**总闸**（常量，不可配）
- `require_server_upi_mandate`（CLI / 调用选项）= 只在 Stage 2 **打一行日志**报告服务器是否给了 `mandate_options`，**不 gate 任何东西**

无法按次 A/B 或回滚，只能改源码。建议把总闸接进 `upi.*` 配置段（`browser_rail`、`approve_shape` 已有先例）。

### 3.7 P3 — 全仓 `ruff format` —— ⏸ 未执行（需独立提交）

`ruff format --check .` 报 **494 个文件**需要重排（288 已格式化）。CI 只跑 `ruff check`（不跑 `format --check`），所以无门禁影响。**需拍板**：是否值得一次独立提交清掉（会淹没其它 diff，须单独提交）、还是维持「只格式化被改动文件」的现状（pi-lens 已是这个行为）。

---

## 4. 明确不做

| 项 | 否决理由 |
| --- | --- |
| 按 vulture 的 21 条 80% 命中清理 | 基本是假阳性：`HTMLParser.handle_*` / `do_POST` / `log_message` 是框架回调；`payment_egress.clear_cache`、`proxy_entry.is_socks` 仅测试引用；`agent_identity.*`、`account_cleanup.select_removable_accounts`、`config.default_config_path` 是对外 API（脚本 / C# 侧调用）。与 pi-lens 自标的 Low confidence 一致 |
| 合并 `_request`（7/7 不同）与 `create_checkout`（8/8 不同） | AST 证明**不是**重复；`docs/architecture.md` 明文：「同名函数在 AST 证明前不算重复」 |
| 按 pi-lens 的 SCC / 分层违规告警重构 | 483 边/14 子包是 name-only 解析产物；精确图是 6 个环/16 模块，且 import time 为 DAG |
| 动 `phone_provider_lifecycle ↔ phone_pool` | 那条边在 `if TYPE_CHECKING:` 内（typing-only，`phone_provider_lifecycle.py:35` 有注释），**不是**运行期环 |
| 让 `sms_tool.providers` 不再被顶层导入 | `mailbox.py` / `account_recovery.py` / `registration_handlers.py` 正是 `docs/directory-map.md` 指定的 provider 消费者；精确图里 `mailbox → providers` 是 8 条**单向**边（reverse 0） |

---

## 5. 复现与验收

```powershell
# 全量测试（本轮基线：5591 passed / 7 skipped）
python -m pytest -q

# 门禁（全部应为绿）
python scripts/sensitive_field_scan.py                 # 本轮修好
python scripts/delayed_import_ratchet.py               # 本轮补基线；会重算逐文件
python scripts/unused_import_ratchet.py                # 425 <= 427
python scripts/bare_print_ratchet.py
python scripts/config_key_ratchet.py
python scripts/endpoints_literal_ratchet.py
python scripts/mailbox_private_import_ratchet.py
python scripts/architecture_scan.py
python -m ruff check sms_tool services scripts

# §1 的两条核心事实（可独立复核）
python -m pytest tests/test_sensitive_field_scan.py tests/test_upi_egress_gate.py -q
```

**精确导入图**（§1.1 / §2.4 的证据，无脚本，按需重算）：解析 `sms_tool/**/*.py` 的全部 `ast.Import` / `ast.ImportFrom`（含函数体内与 `if TYPE_CHECKING`，后者需单独标注），解析相对层级对包布局，Tarjan 求 SCC，并逐边标注 top / lazy。**不要**用 review graph 的边做此判断。

---

## 6. 遗留观察（无需动作，仅记录）

- **`proxy_edge_probe.py:196` 的延迟 `curl_cffi` 导入**：理由已写进代码（`promotion_batch` 顶层导入本模块、curl_cffi 是重可选依赖）。基线已登记（§2.5）。
- **`ruff format` 的两处历史漂移**（`session_refresh.py` / `auth_flow/steps.py`）已顺带清掉，diff 含无关格式改动。
- **pi-lens `pyright` deferred runner 报 `unknown` 失败**：环境问题——本机既无 `pyright` 也无 `basedpyright`（`node_modules/.bin/basedpyright-langserver.exe` ENOENT，npx 需联网），非代码问题。
- **`docs/audits/README.md` 的 `plan-*` 分组**标注为「待拍板，未落地生产默认行为」；本文档 §2 属已落地部分，落地记录亦见工作区改动本身。

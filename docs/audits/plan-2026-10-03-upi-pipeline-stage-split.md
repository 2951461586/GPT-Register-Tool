# UPI `pipeline.py` 阶段拆分方案（依赖注入 seam + 测试迁移清单）

> 状态：**设计稿，未动代码**。目标是把 `sms_tool/upi_link/pipeline.py`（1824 行，
> 其中 `_generate_upi_qr_link_once` 单函数 1138 行）按 `registration_handlers.py`
> 同款拆成「门面 + 依赖束 + 纯 helper + stage 模块」，**不改行为、不改测试 patch 目标**。
>
> 本文所有行号是写作时的快照（`pipeline.py` 1824 行）；`docs/audits/` 不在
> `docs_consistency_scan.py` 的 `DOCS` 规范清单内，故不经行号门禁，落地时须人工刷新。

---

## 0. 目标与不变量（由测试钉住，不可动）

| 不变量 | 钉住它的测试 |
| --- | --- |
| `generate_upi_qr_link` 全仓**只有一处定义**，且必须在 `sms_tool/upi_link/pipeline.py`，`__module__ == "sms_tool.upi_link.pipeline"` | `tests/test_upi_link_entrypoint_unique.py`（AST 扫描全仓 `def generate_upi_qr_link`） |
| `UPI_CALL_OPTIONS` == 入口函数的可选关键字参数集 | 同上 `test_public_option_set_matches_the_pipeline_signature` |
| `gen_pp_link.generate_upi_qr_link is upi_link.generate_upi_qr_link` | 同上 |
| 既有 `monkeypatch.setattr(p, "<name>", …)` / `patch("sms_tool.upi_link.pipeline.<name>")` 必须继续生效 | 10 个 `tests/test_upi_*.py` + `test_gen_pp_link.py` + `test_payment_egress_gate.py` |

**推论**：`generate_upi_qr_link` **不能搬出** `pipeline.py`；只能拆 `_generate_upi_qr_link_once`
与它调用的 helper。「移动即完成」对本模块不成立（见 §2）。

---

## 1. 现状取证

### 1.1 顶层定义（快照）

| 行范围 | 行数 | 名称 |
| --- | --- | --- |
| 119–154 | 36 | `_upi_link_unverified_contract` |
| 157–322 | 166 | `_resolve_upi_runtime` |
| 325–372 | 48 | `upi_invocation` |
| 375–418 | 44 | `_upi_assert_egress_contract` |
| 434–437 | 4 | `_upi_india_exit_probe_enabled` |
| 440–447 | 8 | `_upi_india_exit_attempts` |
| 450–457 | 8 | `_upi_india_exit_timeout` |
| 460–465 | 6 | `_upi_exit_country` |
| 468–484 | 17 | `_upi_exit_admits_checkout` |
| 487–491 | 5 | `_upi_rotate_region_session` |
| 494–531 | 38 | `_upi_select_india_exit` |
| 535–544 | 10 | `_upi_repeat_tax_region` |
| 566–575 | 10 | `_upi_rounds` |
| 578–581 | 4 | `_upi_round_retryable` |
| 584–600 | 17 | `_upi_rotate_proxy_set` |
| **603–1740** | **1138** | **`_generate_upi_qr_link_once`** |
| 1743–1824 | 82 | `generate_upi_qr_link`（wrapper，Increment 3） |

模块级常量只有两个 `AnnAssign`：`_UPI_ADMISSION_PATHS`（:431）、
`_UPI_PRECONFIRM_RETRY_CODES`（:553）。

### 1.2 `_generate_upi_qr_link_once` 的真实依赖面

- **18 个参数**：`access_token, proxy, auth_context, checkout_proxy, provider_proxy,
  approve_proxy, target_country, checkout_country, payment_country, require_zero,
  qr_path, runtime_config, proxy_state, device_id, session_token, wait_paid,
  paid_timeout, require_server_upi_mandate`
- **123 个局部 `Store` 名**（跨阶段存活的状态，是 ctx 的字段来源）
- **4 个内嵌闭包**：`_resubmit_with_fresh_pm` / `_absorb_mandate` / `_absorb` /
  `_rescue_mandate`（捕获上文局部，必须变成「方法 + ctx」或模块级函数）
- **71 个模块全局引用** = 8 个本模块 helper（`_resolve_upi_runtime`、
  `_upi_assert_egress_contract`、`_upi_india_exit_*`、`_upi_repeat_tax_region`、
  `_upi_select_india_exit`、`_upi_link_unverified_contract`）+ 约 55 个**兄弟模块**
  helper + 常量/标准库（`json/time/uuid/Any/Mapping` 与 `UPI_*/STRIPE_*` 常量）
- **17 个 `emit()` stage 标签**，注释边界清晰（见 §5）

### 1.3 导入面（决定 seam 的注入项）

| 来源 | 代表名 |
| --- | --- |
| `._extract` | `_upi_extract_next_action`、`_upi_extract_qr_candidates`、`_upi_get_free_trial_status` … |
| `.constants` | `UPI_CHECKOUT_*`、`STRIPE_PAYMENT_PAGE_*`、`UPI_SENTINEL_APPROVAL_FLOW` … |
| `.env` | `_emit`、`_env_bool`、`_env_int` |
| `.dump` | `_approve_backoff`、`_upi_dump_http` |
| `.config` | `_load_json`、`_method_cfg`、`_upi_retarget_region` |
| `.session` | `_upi_new_chatgpt_session`、`_upi_apply_fingerprint`、`_upi_server_mandate`、`_write_qr_png` … |
| `.browser` | `_upi_browser_approve` |
| `.stripe` | `_upi_stripe_init`、`_upi_create_upi_pm`、`_upi_build_confirm_body`、`_upi_confirm_local_mandate` … |
| `.sentinel` | `_upi_sentinel_ping`、`_upi_wait_paid`、`_upi_fetch_oaics_state` … |
| `.extract` | `_upi_hydrate_qr_data`、`_upi_resolve_external_redirect` |
| `.flows` | `_upi_run_cpmt_flow`、`_upi_run_oaics_flow` |
| `.verify` | `verify_instructions_verdict as _upi_verify_instructions_verdict`、`INCONCLUSIVE as UPI_VERIFY_INCONCLUSIVE` |
| 顶层（带 `try/except ImportError` 直跑回退） | `resolve_proxy_geo`、`probe_openai_edge`、`CHATGPT_CHECKOUT_PATH`、`rotate_session`、`_new_session`、`redact_proxy_text`、`payment_egress`、`_new_session` |

> **直跑约定**：`pipeline.py` 顶部对顶层模块用 `try: from ..x import y / except ImportError:
> from x import y`，因为该文件可被当作裸脚本执行。抽出到**包内兄弟模块**的东西用相对
> 导入即可（它们只在包路径下被 import）；但 `pipeline.py` 对**新兄弟模块**的导入若要兼容
> 直跑，须沿用同款 `try/except`。

### 1.4 测试 patch/访问面（迁移清单的输入）

| 名字 | 次数 | 测试文件 |
| --- | --- | --- |
| `resolve_proxy_geo` | 7 | `test_upi_india_exit` |
| `probe_openai_edge` | 8 | `test_upi_india_exit` |
| `rotate_session` | 7 | `test_upi_india_exit`、`test_upi_rounds` |
| `_generate_upi_qr_link_once` | 4 | `test_upi_tax_repeat`、`test_upi_rounds` |
| `_upi_select_india_exit` / `_upi_exit_admits_checkout` / `_upi_exit_country` / `_upi_india_exit_probe_enabled` / `_upi_india_exit_attempts` / `_upi_india_exit_timeout` | 6+2+1+3+4+1 | `test_upi_india_exit` |
| `_upi_rounds` / `_upi_round_retryable` / `_upi_rotate_proxy_set` | 4+4+1 | `test_upi_rounds` |
| `_upi_repeat_tax_region` | 3 | `test_upi_tax_repeat` |
| `_resolve_upi_runtime` | — | `test_upi_local_mandate`、`test_upi_approve_shape`、`test_upi_verify`（`from …pipeline import _resolve_upi_runtime`） |
| `_upi_confirm_local_mandate` / `_upi_assert_egress_contract` / `_load_json` / `_new_session` / `_upi_stripe_init` / `_upi_create_upi_pm` / `_upi_post_with_degrade` / `_upi_hosted_fallback_result` / `_upi_new_chatgpt_session` / `_upi_sentinel_ping` / `_upi_sentinel_headers` / `_upi_dump_http` / `_upi_build_confirm_body` | 1 each | `test_upi_local_mandate`、`test_gen_pp_link`、`test_payment_egress_gate`、`test_upi_approve_shape` … |
| `generate_upi_qr_link` | 5 | `test_upi_egress_gate`、`test_rounds`、CLI/adapter 契约测试 |

---

## 2. 为什么不能「移动即完成」

1. **闭包全局**：`_upi_select_india_exit` 直接调用 `pipeline` 模块全局的
   `resolve_proxy_geo` / `probe_openai_edge` / `rotate_session`。测试用
   `monkeypatch.setattr(p, "resolve_proxy_geo", …)` 后又调 `p._upi_select_india_exit`。
   若把函数原样搬到 `india_exit.py`，它解析的是 `india_exit` 的全局，patch 失效，
   `test_upi_india_exit` 立即红。
2. **入口不可搬**：§0 的 AST 守卫把 `generate_upi_qr_link` 钉在 `pipeline.py`。
3. **本仓历史代价**：`docs/audits/README.md` 的 `plan-2026-09-30` 条目记录
   `pay_link/` import 头修剪**两次失败回滚**——第一次 `ruff --fix` 删了被另一模块
   消费的名字导致 collection ImportError；第二次带通道建模的 216 删把
   `PaymentRoutePlan`/`PaymentRoutePlanner` 删成 `F821`。结论原文：必须
   **逐文件 + 每步 `ruff check`/全量测试**。本方案的每个切片都照此执行。
4. **123 个局部状态**：拆函数最大的风险不是接口，是「某个跨阶段读取的局部忘了进 ctx」。
   这类错误静态不报、pytest 也未必覆盖，必须靠机械清单核对（§7 S3）。

---

## 3. 目标布局

```text
sms_tool/upi_link/
  pipeline.py        # 门面 + 编排（generate_upi_qr_link 单一定义在此）
  operations.py      # UpiOperations（只读依赖束） + build_upi_operations()
  stage_config.py    # 纯配置读取 / 纯 helper（无副作用）
  india_exit.py      # Increment 1：出口分级与结账准入（ExitOps 注入）
  stages.py          # Stage 1..8 的实现函数：(ops, ctx)
  context.py         # UpiStageContext（跨阶段可变状态）+ 子状态分组
```text

`pipeline.py` **保留**：`generate_upi_qr_link`（唯一 wrapper）、`_generate_upi_qr_link_once`
（变薄为编排）、`upi_invocation`、`_resolve_upi_runtime`、`_upi_assert_egress_contract`、
`_build_upi_operations()`、以及**所有被测试访问的 helper 的兼容 re-export**。

---

## 4. 依赖注入 seam（核心）

### 4.1 两个数据结构

```python
# operations.py
@dataclass(frozen=True)
class UpiOperations:
    """一次调用的只读依赖束：兄弟模块 helper + 顶层 helper + 常量。"""
    # —— 兄弟模块可调用（字段名沿用原名，便于逐行对照 diff）——
    stripe_init: Callable[..., Any]          # 原 _upi_stripe_init
    create_upi_pm: Callable[..., Any]
    build_confirm_body: Callable[..., Any]
    confirm_local_mandate: Callable[..., Any]
    sentinel_ping: Callable[..., Any]
    wait_paid: Callable[..., Any]
    # …约 55 个，按 §1.3 的模块分组，字段名 = 原 import 名去掉 `_upi_` 前缀
    # —— 顶层注入（供 india_exit 等）——
    resolve_proxy_geo: Callable[..., Any]
    probe_openai_edge: Callable[..., Any]
    rotate_session: Callable[..., Any]
    new_session: Callable[..., Any]
    load_json: Callable[..., Any]
    emit: Callable[..., Any]
    # —— 常量 ——
    checkout_url: str
    # …

# context.py
@dataclass
class UpiStageContext:
    """一次调用的可变运行态（原 123 个局部，按阶段分组）。"""
    # 通用
    access_token: str; device_id: str; session_token: str
    checkout_country: str; payment_country: str; require_zero: Any
    emit: Callable[..., Any]
    # checkout
    checkout_proxy: str; checkout_body: dict; checkout_data: dict
    cs: str; cs_id: str; processor_entity: str; checkout_ui_mode: str
    # stripe
    provider_proxy: str; stripe_pk: str; init: dict; tax_resp: Any
    # trial
    ft_status: ..., is_oaics: bool; cpm_id: str; _oaics_state: Any
    # confirm
    pm_id: str; confirm_body: dict; confirm_resp: Any; confirm_data: dict
    # approve / mandate
    approve_proxy: str; approval_data: Any; mandate: Any; mandate_ok: bool
    # redirect / verify
    qr_data: dict; redirect_url: str; verification_code: str
    # 结果
    result: dict
```text

### 4.2 保持零测试迁移的关键：**调用时**组装依赖束

`build_upi_operations()` 定义在 `pipeline.py`，**在调用时读取本模块的全局名**：

```python
# pipeline.py
def _build_upi_operations() -> UpiOperations:
    return UpiOperations(
        stripe_init=_upi_stripe_init,          # ← 读 pipeline 全局，patch p._upi_stripe_init 生效
        create_upi_pm=_upi_create_upi_pm,
        # …
        resolve_proxy_geo=resolve_proxy_geo,   # ← patch p.resolve_proxy_geo 生效
        probe_openai_edge=probe_openai_edge,
        rotate_session=rotate_session,
        new_session=_new_session,
        load_json=_load_json,
        emit=_emit,
        # …
    )
```text

因为是在**调用时**读 `pipeline` 的模块字典，`monkeypatch.setattr(p, "…")` 与
`patch("sms_tool.upi_link.pipeline.…")` **不需要任何改动**。这是本方案与「裸移动」
的决定性差别。

### 4.3 `india_exit.py` 的注入形态

```python
# india_exit.py
def select_india_exit(proxy, *, country, attempts, timeout, emit, ops: ExitOps) -> str:
    ...  # 内部一律走 ops.resolve_proxy_geo / ops.probe_openai_edge / ops.rotate_session

# pipeline.py（兼容包装，名字与签名不变）
def _upi_select_india_exit(proxy, *, country, attempts, timeout, emit=None) -> str:
    ops = ExitOps(
        resolve_proxy_geo=resolve_proxy_geo,
        probe_openai_edge=probe_openai_edge,
        rotate_session=rotate_session,
    )
    return india_exit.select_india_exit(
        proxy, country=country, attempts=attempts, timeout=timeout, emit=emit, ops=ops
    )
```text

`_upi_exit_country` / `_upi_exit_admits_checkout` / `_upi_rotate_region_session`
同理：实现搬走，`pipeline` 保留**同名调用时注入的薄包装**（或在测试只要读值时直接
`re-export`；但 `_upi_exit_admits_checkout` 被 `test_upi_india_exit` 直接调用且依赖
`probe_openai_edge`，必须走包装）。

### 4.4 注入规则（写成可在评审时逐条核对的约束）

1. stage 函数**只**通过 `ops.*` / `ctx.*` 访问外部；不 `import` 兄弟 helper，不读模块全局。
2. 任何 `pipeline` 里被测试 patch 的名字，其**唯一**读取点必须是
   `_build_upi_operations()` 或对应的兼容包装（都在 `pipeline.py`）。
3. 被移出的符号，`pipeline` **必须** `from .newmod import name` re-export（保 `p.<name>`）。
4. 若某 helper 既被 stage 调用又被测试直接调用，**保留薄包装**，实现体搬到新模块。
5. 新模块的顶层导入不用 `try/except ImportError`（只在包内被导入）；`pipeline` 对新兄弟
   模块的导入沿用 `try/except` 以保直跑。

---

## 5. Stage 分解表（快照行号）

| 切片函数 | 原行范围 | 新位置 | 主要 ctx 字段 | 主要 ops 依赖 |
| --- | --- | --- | --- | --- |
| （Increment 1 出口选择） | 692–711 | 留 `pipeline`（调 `india_exit`） | `checkout_proxy/provider_proxy/approve_proxy` | `_upi_india_exit_*`、`_upi_select_india_exit` |
| `stage1_checkout` | 720–817 | `stages.py` | `cs、checkout_body、checkout_data、processor_entity、checkout_ui_mode` | `_upi_new_chatgpt_session`、`_upi_sentinel_*`、`_upi_dump_http` |
| `stage2_stripe_init` | 818–890 | `stages.py` | `stripe_pk、init、currency、amount` | `_upi_stripe_init`、`_upi_apply_fingerprint` |
| `stage3_free_trial` | 891–934 | `stages.py` | `ft_status、payment_method_selection_flow` | `_upi_get_free_trial_status` |
| `stage3b_cpmt` | 935–957 | `stages.py` | `cpm_id、is_oaics` | `_upi_run_cpmt_flow` |
| `stage3c_oaics` | 958–991 | `stages.py` | `_oaics_state、pm_types` | `_upi_run_oaics_flow`、`_upi_fetch_oaics_state` |
| `stage4_tax_customer` | 992–1037 | `stages.py` | `tax_resp、customer_body、customer_session_secret` | `_upi_post_with_degrade`、`_upi_rebuild_ctx` |
| `stage4_5_repeat_tax` | 1038–1119 | `stages.py` | `refreshed_init、retax_resp` | `_upi_stripe_init`、`_upi_repeat_tax_region` |
| `stage5_confirm` | 1120–1239 | `stages.py` | `pm_id、confirm_body、confirm_resp、confirm_data` | `_upi_create_upi_pm`、`_upi_build_confirm_body`、`_upi_should_retry_second_confirm` |
| `stage6_approve` | 1240–1434 | `stages.py` | `approval_data、approval_ok、blocked_count、approve_session` | `_upi_browser_approve`、`_upi_sentinel_headers`、`_upi_wait_paid` |
| `stage6b_mandate` | 1435–1495 | `stages.py` | `mandate、mandate_ok、setup_intent` | `_upi_confirm_local_mandate`、`_upi_server_mandate` |
| `stage7_redirect` | 1496–1664 | `stages.py` | `qr_data、redirect_url、poll_*、upi_uri` | `_upi_extract_*`、`_upi_poll_payment_page`、`_upi_hydrate_qr_data` |
| `stage8_verify` | 1665–1740 | `stages.py` | `verification、verification_code、link_type、result` | `_upi_verify_instructions_verdict`、`_write_qr_png` |
| 4 个内嵌闭包 | 函数内 | `stages.py`（`ops, ctx` 版） | `pm_id、confirm_*、mandate` | 同 stage5/6b |

编排体（新的 `_generate_upi_qr_link_once`）只保留：参数→`UpiStageContext` 初始化、
`ops = _build_upi_operations()`、按序调用 `stages.stageX(ops, ctx)`、`ctx.result` 返回。

---

## 6. 测试迁移清单

### 6.1 结论：**预期 0 处测试改动**

只要遵守 §4.4 的规则，下表每个兼容点都由 `pipeline` 的 re-export / 薄包装 / 调用时组装满足。

| 测试文件 | 依赖的兼容点 | 由谁保证 |
| --- | --- | --- |
| `test_upi_india_exit.py` | `p._upi_select_india_exit`、`p._upi_exit_country`、`p._upi_exit_admits_checkout`、`p._upi_india_exit_{probe_enabled,attempts,timeout}`；`setattr(p,"resolve_proxy_geo"/"probe_openai_edge"/"rotate_session")` | §4.3 包装 + 调用时 `ExitOps` |
| `test_upi_rounds.py` | `p._upi_rounds`、`p._upi_round_retryable`、`p._upi_rotate_proxy_set`、`p.generate_upi_qr_link`、`setattr(p,"_generate_upi_qr_link_once")` | re-export + wrapper/编排留在 `pipeline` |
| `test_upi_tax_repeat.py` | `p._upi_repeat_tax_region`、`setattr(p,"_generate_upi_qr_link_once")` | re-export + 编排留在 `pipeline` |
| `test_upi_local_mandate.py` | `pipeline._resolve_upi_runtime`、`pipeline.__file__`、patch `pipeline._upi_confirm_local_mandate` | `_resolve_upi_runtime` 不移；`_upi_confirm_local_mandate` 留作 ops 字段 |
| `test_upi_approve_shape.py` / `test_upi_verify.py` | `from …pipeline import _resolve_upi_runtime` | 同上 |
| `test_upi_link_entrypoint_unique.py` | 单一定义 + `__module__` + `UPI_CALL_OPTIONS` | §0 不变量，本方案不动它们 |
| `test_gen_pp_link.py` | `pipeline._upi_assert_egress_contract` | 留在 `pipeline` |
| `test_payment_egress_gate.py` / `test_upi_egress_gate.py` | `pipeline.generate_upi_qr_link`、`pipeline.py` 文本 | wrapper 留在 `pipeline` |

### 6.2 只有违反规则时才会需要的改动（示例，作为反例）

- 若把 `_upi_select_india_exit` 做成**裸 import**（无包装），则
  `test_upi_india_exit` 的 `setattr(p,"resolve_proxy_geo")` 失效 → 须把该测试的 patch
  目标改成 `sms_tool.upi_link.india_exit.resolve_proxy_geo`。**本方案避免此改动。**
- 若把 `_generate_upi_qr_link_once` 整体搬到 `stages.py`，则
  `test_upi_rounds` / `test_upi_tax_repeat` 的 `setattr(p,"_generate_upi_qr_link_once")`
  失效 → 须改测试。**本方案保留编排在 `pipeline`，避免此改动。**

### 6.3 每切片强制验证（照 `plan-2026-09-30` 的处方）

```powershell
.venv\Scripts\python.exe -m ruff check sms_tool\upi_link          # F82 抓注解里被删的未定义名（历史失败点）
.venv\Scripts\python.exe -c "import importlib,typing; [typing.get_type_hints(o) for m in ('stages','operations','india_exit','stage_config','context','pipeline') for o in vars(importlib.import_module('sms_tool.upi_link.'+m)).values() if getattr(o,'__module__','').startswith('sms_tool.upi_link')]"
.venv\Scripts\python.exe -m pytest -q                              # 全量 5706
.venv\Scripts\python.exe scripts\docs_consistency_scan.py
.venv\Scripts\python.exe scripts\module_coverage_check.py
.venv\Scripts\python.exe scripts\unused_import_ratchet.py
```text

---

## 7. 执行切片（每片一个 commit，独立可回滚）

| 切片 | 内容 | 风险 |
| --- | --- | --- |
| **S1** | 新建 `stage_config.py`，移入纯 helper：`_upi_link_unverified_contract`、`_upi_india_exit_{probe_enabled,attempts,timeout}`、`_upi_repeat_tax_region`、`_upi_rounds`、`_upi_round_retryable`、`_upi_rotate_proxy_set`；`pipeline` re-export | 低（无兄弟依赖） |
| **S2** | 新建 `india_exit.py` + `ExitOps`；`pipeline` 留同名薄包装 + re-export | 中（patch 兼容，§4.3） |
| **S3** | 新建 `operations.py` + `context.py`：`UpiOperations`/`UpiStageContext` + `_build_upi_operations()`；**只加结构不改 stage**；用机械脚本核对「每个跨阶段局部都在 ctx」 | 中（字段遗漏） |
| **S4** | 新建 `stages.py`，移 Stage 1/2/3/3b/3c | 中 |
| **S5** | 移 Stage 4/4.5/5 | 中 |
| **S6** | 移 Stage 6/6b/7 + 4 个闭包 | 高（闭包状态） |
| **S7** | 移 Stage 8 + 收尾，`pipeline.py` 目标 ≤ ~450 行 | 低 |

**S3 的机械核对**（本方案唯一「新增工程」）：用 AST 求 `_generate_upi_qr_link_once` 的
局部 Store/Read 集合，与按阶段切分后的「stage 边界读取」求差；任何「在 stage A 写、在
stage B 读」的局部不在 ctx 里即报错。这能把 123 局部的迁移从「靠人眼」变成「靠门禁」。

---

## 8. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| ctx 字段遗漏（跨阶段状态丢失） | S3 的机械核对 + 全量 pytest；每片只移动不改逻辑，diff 可逐行审 |
| 直跑脚本（`try/except ImportError`）在新模块缺失 | `pipeline` 对新兄弟模块沿用同款 fallback；新模块内部只用相对导入 |
| patch 目标漂移（测试悄悄测假对象） | §4.4 规则 + 全量 pytest；`ruff check` 的 F82 抓注解未定义名 |
| 行号引用文档漂移 | `docs_consistency_scan.py` 覆盖 `docs/architecture.md` 等规范文档；`docs/registration-and-proxy-architecture.md` 若含 `pipeline.py:NNN` 需同步 |
| 收益/风险比 | 纯可读性收益，零功能变更；若 S1/S2 任一红即停并回滚该片 |

---

## 9. 明确不做

- 不改 `generate_upi_qr_link` 签名、`UPI_CALL_OPTIONS`、`upi_invocation` 的对外语义。
- 不合并兄弟模块间的重复 helper（那是 `plan-2026-09-17` 的议题）。
- 不动 Stage 内部逻辑与网络时序（纯移动）。
- 不做全仓 `ruff format`（`plan-2026-09-30` 已判定需独立提交）。

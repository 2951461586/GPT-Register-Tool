# 阶段 2 真实链路回放方案评估

- **日期**：2026-09-28
- **状态**：**评估稿（未拍板、未动代码）**
- **范围**：`services/protocol-payment/{ideal,twint}` 的 23 个非等价函数（阶段 1 已落地后）
- **相关**：`plan-2026-09-17-protocol-payment-extractor-consolidation.md` §2.4 / §5 / §7、`docs/architecture.md` Rule 10

---

## 1. 结论先行

1. **阶段 2 比原计划风险显著更低。** 原文档按 2026-09-17 的快照把「35 个非等价差异」列为高危；阶段 1 落地后（12 个已收敛）实测**剩 23 个**，且逐一归一化 diff 后，**绝大多数只差日志/错误文本或 provider 数据，不是逻辑分支**。
2. **真正影响 HTTP 请求体的逻辑差异只有 3 处**：`stripe_create_*_pm` 的 `ideal[bank]` 块、`add_inline_*` 的同名块、`update_*_checkout_taxes` 的 `currency` 字段。其余「逻辑差异」影响的是日志、默认 country 字面量、重定向主机白名单。
3. **真实链路回放仍是必需的**，但目标从「防止把语义分支合并掉」变成「**证明参数化后 HTTP 请求流与最终结果逐字节不变**」。因此回放基建应做成**录制一次、离线回放、HEAD vs 参数化差分**的 VCR 形态。
4. **建议先做回放基建（零行为风险），再按「纯数据 → 纯措辞 → 真实逻辑」三档分批参数化。** 不要在回放基建可用之前动任何 `stripe_*` / `run_*`。

---

## 2. 差异构成（实测，可复核）

用 `scripts/extractor_parity_report.py` 的归一化逻辑对 23 个 `different` 逐一展开（provider 词元已抹除），结论：

| 分类 | 函数 | 差异实质 |
| --- | --- | --- |
| **纯措辞**（HTTP 不变） | `run_provider_flow`、`run_once`、`run_attempt`、`run_single_link_mode`、`run_single_link_attempt`、`run_single_link_parallel_mode`、`poll_payment_page`、`stripe_confirm_*`、`checkout_snapshot`、`chatgpt_approve`、`resolve_external_redirect` | 日志/错误字符串；默认 `billing_country` 字面量；重定向主机后缀 |
| **纯数据** | `*_billing_profile`、`payment_browser_locale`、`payment_elements_locale`、`payment_browser_timezone`、`currency_for_country`、`normalize_country`、`is_*_unavailable_error`、`load_proxy_seeds` | 国家/语言/时区/货币/错误标记/国家常量表 |
| **真实逻辑**（影响请求体） | `stripe_create_*_pm`、`add_inline_*_payment_method_data`、`update_*_checkout_taxes` | ① ideal 有 `ideal[bank]`，twint 无；② `type` 值；③ `currency` = 常量 `EUR` vs `currency_for_country(CH)` |

> 归一化 diff 样本（`run_provider_flow`，102 行里只有 6 处 `+/-`，全部是 `f'... iDEAL ...'` ↔ `f'... PROVIDER ...'`）。**没有一处控制流差异。**

---

## 3. 现有回放能力盘点

| 能力 | 现状 |
| --- | --- |
| HTTP dump | `common/http_dump.py` 写 `stage/request/request_body/status/url/response` 文本。**有损**：无请求头、无 Cookie、无响应头、无重定向链，不可直接回放 |
| 已提交录制 | `services/protocol-payment/*/dumps/` **均为空** |
| 回放/打桩库 | 未安装 `vcrpy`/`responses`/`requests_mock`；已装 `requests`、`curl_cffi`、`httpx` |
| extractor 导入测试 | `tests/test_extractors_contract.py` 等已用 `importlib` + 子进程式 `sys.path` 装配导入 extractor |
| 差分 harness 先例 | 本系列已有 `runtime/tmp/_proxy_state_diff_verify*.py`（HEAD vs 新实现，字节级比对）——形态可直接复用 |

**缺口**：一个能拦截两个 session 工厂、记录完整请求/响应、并可离线重放的 transport 层。

---

## 4. 注入点（已确认唯一）

extractor 的所有网络 I/O 都经过两个工厂，且**没有**任何 `requests.get/post` 模块级调用、**没有** subprocess/Node/QuickJS（Sentinel 只是普通 HTTP 端点）：

| 工厂 | 位置（ideal） | 产物 |
| --- | --- | --- |
| `new_session(proxy, use_pre_proxy)` | `ideal_qr_extract.py:809` | `curl_cffi.requests.Session`（有则用）或 `requests.Session` |
| `build_chatgpt_session(...)` | `:948` | 调用 `new_session` 后注入 `oai-*` / `Cookie` / UA 头 |

所以**只需在测试里 monkeypatch 这两个工厂**，即可捕获/重放全链路；无需改 extractor 一行（满足 Rule 10：回放代码留在 `tests/`）。

---

## 5. 回放方案设计

### 5.1 录制层 `RecordingSession`

组合包装（`__getattr__` 委托 + 覆写 `request`；`get/post/put/delete` 走 `request`），对 `requests` / `curl_cffi` 同构：

```python
record = {
  "method": method, "url": resp.url or url,
  "request_headers": dict(session.headers), "request_cookies": dict(session.cookies),
  "request_body": normalize_body(kwargs), "timeout": kwargs.get("timeout"),
  "status": resp.status_code, "response_headers": dict(resp.headers),
  "response_body": resp.text, "response_url": resp.url,
}
```

写 JSONL。**注意**：`dump_http` 保留原样，不替换——它服务运维排障，回放录制服务测试，两者职责不同。

### 5.2 回放层 `ReplaySession`

同一接口，按**录制顺序**返回响应（游标）。每次请求先算指纹并与当前记录比对：

```python
fingerprint = (method, canonical_url(url), canonical_body(body))
```

- **顺序匹配**而非 key 匹配：支付流里同一端点会被轮询多次（`poll_payment_page`、`approve_with_retry`），key 匹配无法区分第几次。
- 指纹不匹配 → **立即 fail**（这正是「请求流变了」的信号，不是噪声）。
- 录制耗尽但代码还要请求 → fail（「循环次数变了」）。

### 5.3 归一化（指纹与断言用）

动态字段在两侧都替换为占位符：

- body：`time_on_page`、`client_session_id`/`elements_session_id`/`stripe_js_id`（uuid）、时间戳、`random_*` 产物；
- url：query 顺序、可丢弃的 tracking 参数；
- header：`Authorization`/`Cookie`/`oai-*` 按**哈希**比较（不落明文，见 §7）。

### 5.4 非确定性固定（run 前统一 patch）

| 源 | 固定方式 |
| --- | --- |
| `uuid.uuid4()` | 递增计数器（`device_id`、`stripe_js_id`、`elements_session_*`） |
| `random.randint/choice/uniform` | `random.seed(0)` + 固定 `Random` |
| `time.time/strftime` | 冻结常量 |
| `time.sleep` | no-op（否则 `poll`/`approve` 循环跑真实秒数） |
| `os.environ` | 每场景固定（`*_BANK`、`*_CHECKOUT_COUNTRY`、`*_FLOW_MODE`、`*_DUMP=0`） |
| 代理串 | 固定假串（走 ReplaySession，不真的拨号） |

### 5.5 差分断言（HEAD vs 参数化）

```python
R = load_recording(scenario)
S_head, H = run(HEAD_snapshot, ReplaySession(R))
S_new,  N = run(refactored,   ReplaySession(R))
assert normalize(S_head) == normalize(S_new)   # 请求流（method+url+body）
assert H == N                                  # (redirect_url, qr_urls)
assert logs_head == logs_new                   # 去时间戳
assert proxy_state_bytes_head == proxy_state_bytes_new
```

HEAD 快照用 `git show HEAD:<file>` 物化到临时目录（与既有差分 harness 同法），保证「改前 / 改后」可同场比对。

### 5.6 场景清单（各录一次真实运行）

1. ideal 成功（0 元 → QR/redirect）
2. twint 成功
3. `generic_decline` / provider 不可用
4. promotion 后金额非 0（`0 元优惠未生效` 分支）
5. already-paid
6. `approve blocked` + 重试（覆盖 `approve_with_retry` 循环）

---

## 6. 验收标准

对每个被参数化的函数，必须同时满足：

1. 相关场景的**请求流指纹序列逐项相等**（HEAD vs 参数化）；
2. **最终结果** `(redirect_url, qr_urls)` 相等；
3. 日志（去时间戳）相等；
4. `proxy_state.json` 落盘字节相等；
5. 回放**不拨真网**（断网也能跑），且录制缺失时 pytest **skip** 而非 fail。

---

## 7. 安全与脱敏

录制体含 **access_token、session cookie、`cs_*`/`pm_*`/`seti_*` id、`cs_secret`、sentinel token、代理凭据**。

- 录制默认落 `runtime/`（gitignored），**不提交**；测试对缺失录制 skip。
- 若确需提交夹具：先跑统一脱敏（token/cookie 哈希化，id 保留前缀+哈希），并过 `sensitive_field_scan` / `scripts/scan_release_payload.py`。
- 指纹里 `Authorization`/`Cookie` 只存哈希，不存明文。

---

## 8. 风险与工作量

| 项 | 评估 |
| --- | --- |
| 录制成本 | 每次一条真实链路（活代理 + 真实账号 + 触发条件）。cs/pm 是**一次性**的，录制是时间点快照 |
| 服务端状态 | 回放离线进行，不重放真网；录制完整性靠 §5.6 场景覆盖 |
| 循环驱动 | `poll`/`approve` 由响应驱动；请求流差异会被 §5.2 顺序匹配直接捕获 |
| 两套 session 库 | `requests` + `curl_cffi` 同构，一个包装类可覆盖；`impersonate` 在回放中无意义 |
| 基建量 | `RecordingSession`+`ReplaySession`+归一化+固定器：约 300–400 行（`tests/`，Rule 10 不触碰 services） |
| 参数化量 | 23 函数：纯数据/措辞约 20 个（低风险），真实逻辑 3 个（中风险） |

---

## 9. 建议实施顺序

```text
S0  回放基建（RecordingSession / ReplaySession / 归一化 / 固定器）—— 零行为风险，先做
S1  录 2–3 个场景（ideal/twint success + 一个失败），跑通「HEAD vs HEAD」自证（必须零差异）
S2  参数化「纯数据」：billing profile / locale / timezone / currency / normalize_country /
    unavailable error / redirect host 表 —— 预期请求流完全不变
S3  参数化「纯措辞」：run_provider_flow / run_once / run_attempt / run_single_link_* /
    poll_payment_page / stripe_confirm_* / checkout_snapshot / chatgpt_approve /
    resolve_external_redirect —— 请求体不变，只改文案
S4  参数化真实逻辑：stripe_create_*_pm（bank 块）、add_inline_*（bank 块）、
    update_*_checkout_taxes（currency）—— 逐一用回放验证
S5  评估 run_* 编排层是否合并（收益最低、需最多场景覆盖）
```

**S1 的自证很关键**：HEAD 对自己回放必须零差异，否则是归一化/固定器不完整，而不是代码差异。

---

## 10. 与既有规则的关系

- **Rule 10**：`RecordingSession`/`ReplaySession`/夹具全部在 `tests/`，extractor 不 import 测试代码，维持进程边界。
- **Rule 13**：不涉及代理字符串语义；回放用假串绕过拨号。
- **Rule 16/18**：S2–S4 的参数化应设计显式接口（如 `ProviderProfile` 数据类），而不是继续让调用方读环境变量。
- **本仓库踩坑清单**：差分脚本用**逐字节**读写；变异验证必须**先还原后判定**；改 `services/` 后跑全量 pytest。

---

## 附：为什么「35 → 23」

阶段 1 的 `common/` 抽取（redaction / file_loading / http_dump / proxy_bookkeeping / proxy_selection / geo / proxy_state / proxy_seed_file / extractor_helpers）把这批「AST 完全相同」的函数收敛成薄封装，parity 的 `different` 从 35 降到 23。剩下的 23 个正是**带 provider 数据或措辞**的那些——也就是本方案的目标。

---

## 11. 落地进展（2026-09-28）

### ✅ S0 — 回放基建（已落地）

- `tests/payment_replay.py`：`RecordingSession`（透明包装，记全量交换）/ `ReplaySession`
  + `ReplayCursor`（**顺序**匹配 method+URL，指纹另存用于跨运行**精确**比对）、
  `canonical_url` / `canonical_body`（uuid/epoch 掩码）、`freeze_determinism`
  （`uuid.uuid4` / `random` / `time.time|strftime|sleep`）、`load/save_recording`
  （LF）、`install_replay` / `install_recording`（只 patch 提取器的 `new_session`，不碰 services）。
- `tests/test_payment_replay.py`：**11 例**（归一化双向、录制→回放往返、严格不匹配/
  耗尽报错、非严格、固定器可复现、install_* 接线）。

### ✅ S1 — 自证 harness（已落地）

- `tests/test_stage2_replay_selfcheck.py`：函数级驱动 `stripe_pm` / `inline` / `taxes`
  三个切片，证明「同代码零差异」且「对请求体变化敏感」；真录制存在时
  （`PAYMENT_REPLAY_DIR` 或 `runtime/payment_replays/`）启用全链路自证，否则 skip。
  当前 **10 passed / 1 skipped**。
- 全链路录制落地方式：`install_recording(module, sink)` 包住一次真实运行 →
  `save_recording(path, sink)` → 把 path 放进 `runtime/payment_replays/`。

### ✅ S2 — provider 数据（已落地）

- 新建 `services/protocol-payment/common/provider_profile.py`：`ProviderProfile`
  frozen dataclass + `IDEAL_PROFILE` / `TWINT_PROFILE`，覆盖 country/currency、
  locale/timezone 默认、unavailable 标记、随机账单池、固定账单与环境变量映射。
  共享函数：`normalize_country` / `currency_for_country` / `payment_*_locale` /
  `payment_browser_timezone` / `is_unavailable_error` / `build_email` /
  `billing_profile`（注入 `env_bool`）。
- ideal / twint 对应 8 个函数变薄封装；删掉已迁入 profile 的死数据表
  （`NL_/CH_BILLING_*`、`DEFAULT_*_BILLING`、`EMAIL_DOMAINS`）。
- **注意**：`COUNTRY_CURRENCY` 按 profile 区分——ideal 表**无 CH**（回退 NL），
  twint 表**含 CH→CHF**；两边各自的 `module.COUNTRY_CURRENCY` 保留为 profile 的
  派生视图（`dict(_PROVIDER_PROFILE.country_currency)`），因为
  `test_protocol_payment_provider_differences.py` 是它的契约。
- **验证**：provider-difference / geo / geo-gate 共 **35 例**全过；parity 再降
  ideal:twint 56→55、ideal:blik 38→36、twint:blik 33→32。
- 顺带修了 3 个**既有**潜在缺陷（编辑触发 pyright 全文件重扫才现形）：
  `run_single_link_attempt` 里 `checkout_proxy` 在 `try` 内赋值却在 `except` 使用
  （ideal + twint 各一处，现初始化为 `""`）；`common/geo.py` 的 `remove_failed`
  回调类型过窄；`geo.py` / 两提取器的 `int()` 改安全强转。

### ⏸ S3 / S4 / S5 — 状态与建议

- **S4（3 个真实逻辑函数：`stripe_create_*_pm` / `add_inline_*` /
  `update_*_checkout_taxes`）**：变更本身小（bank 块、`type`、默认账单、currency），
  且 S1 的 `stripe_pm`/`inline`/`taxes` 切片可直接验证。**但**按本方案 §5 与
  计划文档 §6，这一步属于「需要真实链路回放才授权」的改动；在拿到真实录制前
  不落地，避免在无证据的情况下改支付请求体。
- **S3（`run_*` / `poll_payment_page` 等纯措辞）**：逐字 diff 显示只差日志字符串，
  但共享它们需要把 provider 专属的被调函数（`stripe_*` / country 常量 /
  `is_*_unavailable_error`）经 hooks 注入——这正是计划 §6 明确否决的
  「单一参数化引擎」形态，**不建议做**。
- **S5（`run_*` 编排层合并）**：同上，收益最低、风险最高；计划 §7 Q1 的结论是
  长期应「ideal/twint 向 blik 收敛」（迁移），而非抽中间骨架。

**启用 S4 的唯一前置**：按 §5.6 录 6 个场景（ideal/twint 成功 +
`generic_decline` / 未生效 / already-paid / approve-blocked），放进
`runtime/payment_replays/`，`test_stage2_replay_selfcheck.py` 会自动开始跑全链路。

# 立项：协议支付提取器公共骨架抽取（ideal / twint / blik）

- **日期**：2026-09-17
- **状态**：**提案，未拍板**（本文档不含任何已落地的改动）
- **范围**：`services/protocol-payment/{ideal,twint,blik}/`
- **相关**：`docs/architecture.md` Rule 10、Rule 13；`docs/directory-map.md`
- **背景**：2026-09-17 的 P1 优化轮次。P0 已收敛 `normalize_proxy_url` 的 7 份重复实现
  （见 `services/protocol-payment/common/proxy_url.py`），本轮只处理这一个遗留的大件。

---

## 1. 结论先行

**建议做，但不建议现在做，也不建议一次做完。**

- 这**不是**「三个文件长得像」的观感问题：ideal 与 twint 有 **108/134 个顶层函数在
  标识符归一化后 AST 完全相同**，占 81%。这是一个真实的、可量化的复制粘贴簇。
- 但它**是真实支付链路**（Stripe payment method 创建、确认、轮询、代理分组命中率统计），
  且三份文件里有 **35 个函数是非等价差异**，不是纯改名。盲目抽骨架会把
  「看起来一样、行为不同」的分支合并掉，属于高危改动。
- 因此建议**分阶段**：先做**零行为风险的度量基建**（阶段 0），再只抽**已被证明
  完全等价**的那一层（阶段 1），把有差异的部分留到最后单独决策。

**不建议**做「大爆炸式合并」——把三个文件合成一个参数化引擎。理由见 §6。

---

## 2. 量化现状（可复核）

### 2.1 文件规模

| 文件 | 行数 | 顶层函数 | 函数体行数 |
| --- | ---: | ---: | ---: |
| `blik/blik_qr_extract.py` | 3655 | 154 | 842 |
| `ideal/ideal_qr_extract.py` | 3188 | 134 | 691 |
| `twint/twint_extract.py` | 3174 | 134 | 687 |
| 合计 | **10017** | 422 | 2220 |

> 行数与函数体行数差距很大，说明**大量体积在模块级常量与数据表**（国家/货币/代理/
> locale 表）。这部分是否也该共享，是 §7 的开放问题。

### 2.2 逐行相似度（`difflib.SequenceMatcher`，按行）

| 对比 | 相同行 | 基准 | 占比 |
| --- | ---: | ---: | ---: |
| ideal vs twint | 2915 | 3188 | **91.4%** |
| ideal vs blik | 2307 | 3655 | 63.1% |
| twint vs blik | 2174 | 3655 | 59.5% |
| momo vs kakao（对照组） | 50 | 2046 | 2.4% |

momo/kakao 作对照组的意义：**重复是 ideal/twint/blik 这个簇特有的**，不是
`services/` 的普遍现象，所以不能用「反正都这样」把问题合理化。

### 2.3 AST 级等价度（标识符归一化后）

方法：把 `ideal`/`twint`/`blik`/`IDEAL`/`TWINT`/`BLIK` 出现在 `Name.id`、
`arg.arg`、`Attribute.attr`、字符串常量里的 token 全部替换为 `PROVIDER`，
再比对 `ast.dump`。这排除了「只改了名字」的伪差异。

| 对比 | 结构完全相同的顶层函数 |
| --- | ---: |
| ideal vs twint（共同 134 个中） | **108** |
| 三份共同且完全一致 | **71** |

**81% 的顶层函数是纯复制**，这是本提案的核心事实依据。

### 2.4 真正不同的 35 个函数（ideal vs twint）

这是**改造时必须逐个看**的清单，也是工作量的真实来源：

```
名称类（只差一个词，但需确认是纯参数化还是语义分支）
  normalize_country               currency_for_country
  payment_browser_locale          payment_elements_locale
  payment_browser_timezone        normalize_proxy_url
  load_proxy_seeds                checkout_snapshot
  stripe_confirm_return_url       chatgpt_approve
  poll_payment_page               run_provider_flow
  run_once                        run_attempt
  run_single_link_attempt         run_single_link_parallel_mode
  run_single_link_mode

provider 后缀成对（ideal_* / twint_* 各一份，共 8 对）
  is_*_unavailable_error          *_proxy_chain
  log_*_proxy_chain               update_*_checkout_taxes
  *_billing_profile               stripe_create_*_pm
  add_inline_*_payment_method_data  stripe_confirm_*
  resolve_confirm_payload_*
```

**关键观察**：`stripe_create_ideal_pm` / `stripe_create_twint_pm` 这类成对函数，
应该被参数化成 `stripe_create_pm(provider, ...)`——这正是阶段 1 的目标形态。

### 2.5 blik 独有的 32 个函数

blik 比另两个多出 32 个函数（154 vs 134），集中在**代理国家/地理探测**：

```
clean_country_code  default_payment_country  ensure_proxy_country
ensure_proxy_targets  expected_proxy_countries  geo_lookup_urls
load_proxy_groups  lookup_proxy_country  parse_geo_country
precheck_proxy_group  proxy_file_for_group  target_probe_urls
...
```

这暗示 blik 携带了一套**更新的代理选址实现**，而 ideal/twint 还是旧版。
**这是一个独立的问题**：不是"重复"，而是"三个文件处在不同的演进阶段"。
在决定抽公共骨架之前，必须先回答 §7 的 Q1。

---

## 3. 建议方案：三阶段

### 阶段 0 —— 度量基建（零行为风险，建议立即做）

**产出**：`scripts/extractor_parity_report.py`

- 对指定两个提取器做 §2.3 的 AST 归一化比对，输出：
  - 结构等价的函数列表及行数
  - 不等价的函数列表
  - 等价行数 / 总行数的占比
- 以 **JSON 基线 + ratchet** 形式落盘（复用
  `scripts/mailbox_private_import_ratchet.py` 的模式）：
  **重复度只许下降，不许上升**。这样"又有人复制一份"会被 CI 立刻拦住。
- 配套 `tests/test_extractor_parity_report.py`，含**双向自证**：
  对合成的"纯改名副本"断言判为等价、对"改了一个比较符"断言判为不等价。

**为什么先做这个**：没有可信的等价度度量，任何"抽公共骨架"都无法证明自己没改行为。
这一步同时把「重复度不许增长」变成可执行约束，价值独立于后续阶段。

### 阶段 1 —— 抽取"已证明完全等价"的纯工具层（中风险）

只抽 **§2.3 判为 SAME 且不含任何 provider 语义**的函数，按类别分批：

| 批次 | 内容 | 例（均来自 108 个 SAME） |
| --- | --- | --- |
| 1a | 文本/脱敏 | `_redact_text`、`redact_log_text`、`proxy_short` |
| 1b | 文件/加载 | `load_token`、`load_proxy_file`、`normalize_token` |
| 1c | HTTP dump | `dump_http`、`new_session` |
| 1d | 代理簿记 | `record_proxy_result`、`remove_failed_proxies`、`unique_proxy_seeds` |
| 1e | 代理选择 | `pick_random_proxies`、`is_preferred_proxy`、`proxy_for_country` |

- 落点：`services/protocol-payment/common/`（与 P0 的 `proxy_url.py` 同级，
  遵守 Rule 10：**不得 import `sms_tool`**）。
- **每批单独提交**，每批都要过 §5 的差分验证。

**不做**：`run_*` 系列（编排层）与 `stripe_*` 系列（支付语义）留在阶段 2。

### 阶段 2 —— 参数化成对函数（高风险，需单独拍板）

把 `stripe_create_ideal_pm` / `stripe_create_twint_pm` 这类合并成
`stripe_create_pm(provider=...)`。**这一步必须先答完 §7 的 Q2/Q3**，
且必须对**真实链路**做端到端回放验证。

若 §7 Q1 的答案是"blik 是新一代、ideal/twint 是旧版"，
那么正确的动作可能是**让 ideal/twint 向 blik 收敛**，而不是抽一个中间骨架——
这会让阶段 2 变成一次迁移而非一次重构。**这是本提案最大的不确定点。**

---

## 4. 建议的目录形态（阶段 1 后）

```
services/protocol-payment/
  common/
    proxy_url.py          # 已存在（P0）
    protocol_core.py      # 已存在（P0/P1-4）
    redaction.py          # 新增：脱敏与日志裁剪
    proxy_bookkeeping.py  # 新增：代理命中率簿记
    proxy_selection.py    # 新增：代理挑选与偏好判定
  ideal/    ideal_qr_extract.py     # 保留 provider 特有 + 编排
  twint/    twint_extract.py
  blik/     blik_qr_extract.py
```

目标不是"删掉重复代码"，而是**让每个 provider 文件只剩它的差异**。
行数下降是结果，不是目标——用行数当 KPI 会诱导过度合并（见 §6）。

---

## 5. 验证策略（不可省略）

沿用本仓库既有的差分验证口径：

1. **差分验证**：对 HEAD 导出字节副本，逐输入比对公共函数调用结果。
   P0 的 `normalize_proxy_url` 收敛用了这一手法（1806 次调用零差异），
   可直接复用其 harness 形态。
2. **AST 等价校验**：抽取后的调用点必须与原实现 AST 等价（就是阶段 0 的工具本身）。
3. **端到端回放**：`services/protocol-payment` 是**进程边界**（Rule 10），
   必须跑它的现有测试集 + 至少一条真实链路回放。
4. **变异验证**：任何新增守卫（含 ratchet 与 parity 报告）必须做
   **红 → 还原 → 再绿**，且**还原必须先于判定**。
   ⚠️ 本仓库 2026-09-17 已在这条上踩过两次：变异脚本若先判定后还原，
   abort 会把变异留在盘上。
5. **换行符**：改动用**逐字节**读写，不要用 `Path.read_text/write_text`。
   Windows 下 text 模式会把 `\n` 写回成 `\r\n`，而
   `scripts/line_ending_guard.py` **只禁止混用、不禁止全 CRLF**，抓不到这种污染。
   （同类事故：P1-5 的变异脚本真的把目标文件的 359 行全刷成了 CRLF。）

---

## 6. 明确否决项（不建议做）

| 否决 | 理由 |
| --- | --- |
| 三文件合并为单一"参数化引擎" | 会把 35 个非等价差异压进同一批 `if provider == ...` 分支，把一个可读的复制粘贴问题换成一个不可读的配置地狱。且三份文件处在不同演进阶段（§2.5），强行统一等于冻结演进。 |
| 用「行数下降」当验收指标 | 诱导过度合并。真正的验收是 §5 的差分零差异 + AST 等价。 |
| 一次提交完成阶段 1 | 2220 函数体行、5 个批次，无法有效 review，也无法在出问题时定位到批次。 |
| 现在就动 blik | blik 携带独一无二的代理选址实现（§2.5）。在弄清它是否是新版之前动它，会把信息差掩埋掉。 |
| 抽 `run_*` 编排层 | 编排层是 provider 差异最集中的地方（35 个差异里 8 个是 `run_*`），收益/风险比最差。 |

---

## 7. 待回答的开放问题（开工前必须定）

- **Q1（最关键）**：blik 多出的 32 个代理选址函数，是"blik 特有的需求"还是
  "ideal/twint 落后的版本"？决定阶段 2 是**重构**还是**迁移**。
  验证方式：查 git 历史 + 比对 `sms_tool` 侧同类实现（`proxy_entry`、
  `lookup_proxy_country` 等）。

  **✅ 已拍板（2026-09-19）**：证据与结论——
  - **git 历史**：blik 的 geo 函数在 `20ab44e`（2026-09-18，"registration-hardening
    and account-health series"）中作为**加固系列的一部分**进入；同一批次里
    ideal/twint 只收到了 P1-3 的 batch 2/3/4/5（redaction / file_loading /
    http_dump / proxy_selection 抽取），**没有**收到 geo 选址。即：三者同步于
    09-18，geo 选址是**只落在 blik 上的新增能力**，不是 ideal/twint 被落下的旧版。
  - **sms_tool 侧对照**：`sms_tool/geo/resolver.py`（09-14 独立演进）已是
    **设计更完整的权威**——hint/probe 优先级链、正/负缓存 TTL、`ProxyGeo`
    数据类；blik 自带的 `geo_lookup_urls` 用的端点（ipwho.is / ipapi.co）
    是 resolver 端点表的**真子集**，且 blik 版**无缓存、无 hint 优先级**。
    ⇒ blik 不是"新一代"，而是**在 sms_tool 已有更好实现之外的平行副本**。
  - **Rule 10 约束**：`services/` 是进程边界，**不得 import `sms_tool`**，
    所以不能直接换用 `geo/resolver`。
  - **结论**：阶段 2 对该子集的性质是**「让 ideal/twint 向公共实现收敛」的迁移**
    （而非三者向中间骨架的重构）；落地形态是把 blik 的 geo 选址**下沉为
    `services/protocol-payment/common/geo.py`**（services 自己的权威，对齐
    `proxy_url.py` 的既有先例），三个提取器共用；**长期方向**是把
    `geo/resolver.py` 的 hint/TTL 缓存语义移植进 `common/geo.py`，消除两套语义。

  **✅ 已落地（2026-09-19，阶段 2 的 geo 子集）**：
  - 新建 `services/protocol-payment/common/geo.py`（347 行）：纯函数
    （`clean_country_code` / `geo_lookup_urls` / `parse_geo_country` /
    `target_probe_urls` / `target_response_error` / `format_expected_countries`）
    直接共享；状态函数（`lookup_proxy_country` / `lookup_proxy_targets` /
    `ensure_proxy_country` / `ensure_proxy_targets` / `precheck_proxy_group` /
    `expected_proxy_countries`）按既有 `proxy_selection.py` 的**依赖注入**模式
    承载，注入 `record` / `save_state` / `new_session` / `env_bool` / `env_int` /
    `redact` / `log` / `label` / `remove_failed*` / `record_health_failure` 与
    `env_prefix`（`IDEAL`/`TWINT`/`BLIK`）。遵守 Rule 10：纯 stdlib，无 sms_tool。
  - **差分验证**（`runtime/tmp/_geo_diff_verify.py`）：40 个输入用例（9 个
    `clean_country_code` + 4 个 `format` + 10 个 `parse_geo_country` + 8 个
    `target_response_error` + 4 个 `target_probe_urls` + 3 个 `expected` + 状态
    函数 `lookup_proxy_country` 的 result/record 双比对）**零分歧**。
  - blik 已重接线：12 个原函数变为薄封装（签名与调用点不变），删除约 180 行
    重复实现。
  - 测试 `tests/test_protocol_payment_geo.py` 18 例全过；`test_extractors_contract`
    / `test_geo_resolver` 等 89 例全过；parity ratchet 未升。
  - **注意**：parity 数字（ideal:blik=66 等）**不变是预期** —— 阶段 1 只挪实现、
    保留同名薄封装，重复度 ratchet 量的是"顶层函数 AST 等价"，薄封装仍同名。
    真正的收益是"行为单一权威"：geo 逻辑现在只有 `common/geo.py` 一份。
  - **下一步（ideal/twint 接入）**：两者当前**没有** geo 选址（blik 独有）。是否给
    ideal/twint 接上 `common/geo.py` 是一个**功能决策**（要不要让 ideal/twint 也做
    出口国家/目标站预筛），不是重构——需单独拍板，不在本提案授权范围。

  **✅ 已落地（2026-09-19，ideal/twint geo 预筛——可选、默认关闭）**：
  - **关键勘察结论**（`runtime/tmp/_probe_chain*.py`）：ideal/twint 的
    `*_proxy_chain(seed)` 对单条 sticky seed **并不改写代理串**（`proxy_for_country`
    找不到未编码的 `country=`/`region=` 选择器 ⇒ 原样返回），即 checkout/promotion/
    provider 当前共享**同一条出口 IP**。因此 geo 检查探测的是"这条出口的国家是否符合
    预期"，**有意义且不构成恒定误报**——这推翻了"blik 池预筛模型不适用"的担忧。
  - **接入方式**：在 `run_once` 的 `*_proxy_chain()` 之后、`build_chatgpt_session`
    之前插入 `maybe_check_proxy_geo(checkout_proxy, provider_proxy)`。它是**默认关闭**
    的可选门：`IDEAL_PROXY_GEO_CHECK` / `TWINT_PROXY_GEO_CHECK` 默认 `False`（默认
    路径零网络、零异常、零行为变化）；`IDEAL_PROXY_TARGET_CHECK` /
    `TWINT_PROXY_TARGET_CHECK` 再单独控制目标站可达性。开启是行为变更（一次探测 +
    可能拒绝可用代理），故做成显式 opt-in，由运维拍板。
  - 新增薄封装 `ideal_/twint_lookup_proxy_country`、`ideal_/twint_lookup_proxy_targets`、
    `ideal_/twint_expected_proxy_countries`，全部委托 `common/geo.py`（单一权威）。
  - 测试 `tests/test_protocol_payment_geo_gate.py` 5 例：默认 OFF 为 no-op、opt-in 后
    调用共享实现且 env_prefix 正确、目标站检查需独立 flag。
  - **开启建议**：先在单账号 canary 上开 `IDEAL_PROXY_GEO_CHECK=1` 观察一轮（确认
    出口国家判定不误伤），再考虑 TWINT。
- **Q2**：`payment_browser_locale` / `payment_elements_locale` /
  `payment_browser_timezone` 三者在三个 provider 间的差异，是
  **数据差异**（该进配置表）还是**逻辑差异**？
- **Q3**：`normalize_proxy_url` 在三份提取器里各有一份**未被 P0 收敛**的副本
  （见 §2.4）。P0 只收敛了 7 份中的一部分——**这三份是否属于同一批**？
  若是，本提案可以和那条线合并，省掉一批工作。
- **Q4**：模块级常量与数据表（占文件体积的大头，§2.1）是否也该共享？
  国家/货币/locale 表在三个文件里高度重复，抽取方式（Python 常量 vs JSON 资源）
  需要单独设计。

---

## 8. 工作量与顺序建议

```
阶段 0（度量基建）      —— 独立可交付，建议立刻做
   ↓ 产出等价度基线 + ratchet
Q1 定案                —— 决定阶段 2 的性质（重构 / 迁移）
   ↓
阶段 1（纯工具层 5 批）  —— 每批单独提交、单独差分验证
   ↓
Q2/Q3/Q4 定案
   ↓
阶段 2（参数化成对函数）—— 需重新拍板，不在本提案授权范围内
```

---

## 9. 与既有规则的关系

- **Rule 10**：`services/protocol-payment/` 与 `sms_tool` 零 import 级耦合。
  新增的 `common/` 模块**必须**遵守——这也是 P0 的 `proxy_url.py` 已经做到的。
- **Rule 13**：代理串解析的单一权威是 `sms_tool/proxy_entry.py`。
  注意这**不适用于** `services/` 内部——P0 已确认 `common/proxy_url.py` 是
  `services` 侧自己的权威，两者是不同进程边界。本提案的 `proxy_selection.py`
  同理，不要试图并入 `proxy_entry`。
- **Rule 16/18**：新增的 `common/` 模块会成为新的"私有符号消费面"。
  抽取时应**刻意设计公开接口**，而不是让调用方继续 `from .common.x import _y`。

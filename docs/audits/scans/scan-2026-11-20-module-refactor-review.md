# 2026-11-20 全仓模块评审：需更新 / 解耦 / 重构 / 移除的项

- **日期**：2026-11-20
- **HEAD**：`5f5b07e`（评审开始时 `3009b75`）
- **范围**：`sms_tool/`（111K 行 / 335 模块）、`services/`（~20K 行）、`SmsWorkbench/` + `SmsWorkbench.Contracts/`（~21K 行 / 133 个 `.cs`）、`scripts/`（17.6K 行 / 64 文件）、`tests/`（7669 例）
- **方法**：先把仓内 **26 个 ratchet / check 脚本全部跑一遍**（全部 exit 0），再用独立测量补它们看不见的部分：跨文件重复窗口（10 行窗口哈希）、未读 CLI flag、未读设置字段、未读环境变量、跨语言契约路径对齐、C# 方法长度与代码建 UI 计数、`unittest`/`pytest` 双栈比例、覆盖率实测。
- **结论一句话**：门禁全绿不等于干净 —— 那些数字是**被允许**的，不是"通过"的。本轮据此落地 6 批改动，其余按下表分级处置。

## 0. 门禁余量（本轮开始时的实测，全部为脚本原始输出）

| 门禁 | 数值 | 含义 |
| --- | --- | --- |
| `dependency_graph` | 336 模块 / 1083 模块级边 / **0 循环** / 17 对互指 | 健康 |
| `import_layer_ratchet` | 494 边 / 78 对 / 17 mutual / 326 delayed | 冻结 |
| `function_length` | **64 个函数 ≥150 行** | 冻结，最大 615 行 |
| `broad_except` | 969 handler / **158 静默**，预算 971/158 | 余量 **2** |
| `unused_import` | 256 / 基线 257 | 余量 **1** |
| `root_module` | **81 个平铺根模块**（31K 行） | 冻结 |
| `bare_print` | 488 | 冻结，见 §3.2 |
| `lane_coupling` | payment→registration **15** 边 | 冻结 |
| `provider_decoupling` | 34 个同名异义函数 | 冻结 |
| 覆盖率 | `57721 / 16107 = 72%`，**无阈值** | 只测不判 |
| 测试 | 7669 例（176 文件 `unittest` / 261 文件 pytest） | 双栈并存 |

## 1. 已落地（本轮 6 个提交）

| 批次 | 提交 | 内容 | 证据 |
| --- | --- | --- | --- |
| P0-1 | `2ce9b6a` | `new_session` / `fetch_redirect_page` 下沉 `common/session.py`（阶段 1 batch 10），blik 的 `IDEAL_USE_LOCAL_PROXY_ONLY` 闸门用「注入解析」保留 | parity 25→24 / 12→11 / 9→8；`tests/payment` 2298 通过；新差分测试 22 例 + 变异证明 |
| P0-2 | `08a55cf` | 删 6 个死 CLI flag + 6 个 test-only C# builder；`capability_browser` 改为 `PP_CAPABILITY_BROWSER_FALLBACK` 显式接线 | 全量 7665 通过；`dotnet test` 745；CLI 参考由 parser 重新生成 |
| P0-3 | `5f5b07e` | 行状态判定改读机器态（`AccountMachineState`），`StatusSeverityConverter` 降级为遗留行回退；`PoolRow` 迁出 `MainWindow.xaml.cs` | `dotnet test` 787（新增 42 例，含"机器值双向优先"与遗留回退） |
| P1-1 | `00442cd` | 三个代理字符串 helper 定义迁入 `proxy/entry.py`（ADR-0011 的前置条件），payment lane 直连权威；删 3 个死设置项 + 新设置路径 parity 测试 | payment→registration **15→7**；`tests/governance` 840 通过；C# 787 |
| P1-3a | 见 §4 | 覆盖率门禁 `--cov-fail-under=70` + `test_coverage_gate.py` | 实测 72% → 下限 70 |
| P1-2 | 见 §3.2 | 静默异常**分类复核**（结论：以拆除/DOM 回退为主，0 处吞逻辑错误），写进 `testing.md` | 41 个静默点逐处读 try 体 |

## 2. 仍待处理：按价值排序

### P2-2 `Dialogs/` 的代码建 UI（1991 行 / 9 文件，无 XAML、无单测）

`ExportDialogService.cs` 31 处 `new TextBlock/Border/StackPanel`，
`MainWindow.Detail.cs::ShowAccountDetailAsync` 250 行手建整个详情窗格，
全仓 **101 处**代码建控件。代价：换主题只能逐处 `FindResource`、`dotnet test`
覆盖不到（34 个生产 `.cs` 从未在测试里被点名，`Dialogs/` 占 4 个）、无法预览。

**建议顺序**：`ExportDialogService` → `AccountDetailDialog` →
`SmsProviderDialogService`（最长、最常改、最缺测试的三个）。每个转换配一个
`DesktopWindowSmokeTests` 式断言（`Template.FindName` + `FindResource`）。

### P2-3 根模块收拢（**已落地 5 簇：81 → 62**）

`scripts/module_move.py --spec scripts/move_specs/<cluster>.json --apply`，每簇一次
提交，工具在落地后自动跑 8 个门禁：

| 簇 | 目标包 | 模块 | 备注 |
| --- | --- | --- | --- |
| wallet | `sms_tool/wallet/` | `wallet_provider` · `wallet_transport` · `gcash_provider` · `gcash_transport` | 四者同为 layer 4，包即 4，零方向变化 |
| importers | `sms_tool/importers/` | `cpa_import` · `sub2api_import` · `import_targets` | 同为 layer 3 |
| codex | `sms_tool/codex/` | `codex_export` · `codex_oauth` · `codex_phone` · `codex_sentinel` | 包 3 + **模块级 override**：`codex/sentinel` 留 4（`sentinel_tokens` 是 layer 4 的消费者）；整包取 4 反而会让 `codex/oauth -> mailbox.router` / `account_creation` 越界 |
| session | `sms_tool/session/` | `session_builder` · `session_converter` · `session_refresh` | 包 4；`session_builder` 原名 layer 3，上移后无越界边 |
| phone | `sms_tool/phone/` | `phone_config` · `phone_pool` · `phone_provider_lifecycle` · `phone_registration` · `phone_reuse` | 包 4 + override：`phone/registration` 留 3（导入 `registration.outcome`）。**`phone_proxy` 故意不搬**：它是 Rule 13 / ADR-0011 的包装壳，名字本身是文档事实 |

顺带修的两处“搬家后遗症”，都值得记：
* `mailbox_private_import_ratchet` 按**模块名**追踪私有 hub，`session_refresh` 变成
  `session.refresh` 后它的最后一段不再等于 hub 名 ⇒ 新增 `PACKAGED_HUB_PATHS`
  （按最后两段匹配）。**不能用 `session` 当 hub 名**：`upi_link/session.py` 与
  `paypal/session.py` 的相对导入会被误记成同一个 hub（实测 8 条误报）。
* `test_registration_refresh_token` 原本断言 `ImportFrom.module == "codex_oauth"`，
  搬家后应写成 `*.codex.oauth` —— 否则这种“按路径钉死”的测试每次搬模块都会红。

**剩余**：62 个根模块（`auth_headers` / `phone_proxy` / `checkout_contract` /
`operator_output` / `endpoints` 等共享叶子）。再往下收拢需要先定层与命名
（例如 `sms_tool/contracts/`、`sms_tool/output/`），属于独立决策，不在本轮。

### P1-3b 测试框架统一（**已落地：棘轮 + 一个样板文件**，175 个待逐目录迁移）

`unittest.TestCase` 拿不到 pytest 的 fixture/parametrize，且 `setUp` 自己抛错时
teardown 不执行。175 个文件一次性 codemod 属于本仓明确视为不可验证的改动（丢掉
`setUp` 会让测试空转通过），所以：`scripts/unittest_style_ratchet.py` 逐目录冻结、
只许下降；`tests/config/test_logging_crash_hooks.py` 是样板（shared `setUp` →
一个 `hooks` fixture + `yield`/`finally` 恢复，测试数 8 → 8）；迁移配方写在
`docs/current/testing.md`（含“测试数必须不变”）。逐目录顺序建议
`config → proxy → workbench → accounts → mailbox → governance → payment → registration`。

### P2-1 治理层公共骨架（原估 600–900 行，复测后**降级**）

复测结论：`scripts/` 的重复部分只有 baseline 读写 + argparse（~15–20 行/脚本，
全仓约 300 行），**规则与失败文案才是那 17.6K 行的主体**，而文案被 59 个治理测试
逐字钉住。抽公共模块的收益从"600–900 行"降到"约 300 行"，且迁移会碰到所有被钉住的
输出。**建议**：只抽 `scripts/_baseline.py`（JSON 读写 + `--update-baseline` +
确定化输出），新门禁用它，旧门禁在下次被改动时顺带迁移；不要为这个做一次专项重构。

### 其他（各自独立，风险低）

| 项 | 现状 | 建议 |
| --- | --- | --- |
| `services/mail-otp-web/` | 独立 Flask 诊断站，仓内零引用，Graph/IMAP 逻辑与 `providers/mailbox_graph.py` 重复 | 产品决定：归档（保留 README 指向 `--doctor`）或接到 `--diagnose-mailbox` |
| playwright | 安装 1.60.0 / PyPI 1.65.2（差 5 个 minor） | 受 `dependabot.yml` 的 camoufox 门控，**保持**；解禁条件写进注释 |
| xunit | `xunit 2.9.3`(v2 API) + `runner.visualstudio 3.1.5`(v3 runner) | 统一到 v3 是一次性成本，收益有限；排在 P2-2 之后 |
| `cpa_import` ↔ `sub2api_import` | 14 行重复（不是 130 行窗口命中） | **不做**，不构成合并理由 |

## 3. 复核掉的（原判断被自己的测量推翻）

### 3.1 `capability_browser` 是"死码"

`scan-2026-11-02-unused-module-inventory.md` 已定性为"文档化的 plug-in seam，不是死码"。
本轮按它的处置顺序执行 —— 先决定能力是否保留（**保留**），再给真实调用点
（`PP_CAPABILITY_BROWSER_FALLBACK`，默认 off），而不是删模块。删模块会同时留下
指向空气的文档与孤立测试。

### 3.2 488 个裸 `print` 应迁到 `operator_output`

`operator_output.py` 的 docstring 已写明：`print` 经 `SanitizingTextIO` +
`StdoutMirror` 已经**脱敏并落盘**，`emit` 只多一条 logging 记录；批量重写 500+ 调用点
只增加风险。**本轮不做批量迁移**，只在 §3.3 的分类里说明静默点的处置口径。

### 3.3 158 个静默 handler 应清零

对 6 个静默最多的文件（41 个静默 handler）逐处读 try 体：

| 类别 | 数量 | 例 |
| --- | ---: | --- |
| 拆除/清理 | 主导 | `proxy/pool.py` 的 pipe teardown、`external_sessions/managed.py` 的幂等关窗 |
| JS/DOM 探测回退 | 次之 | `paypal/dom_fields.py`、`browser_flow/page_state.py` |
| 吞掉逻辑错误 | **0** | — |

结论：这条按"不许增长"处理（现状），不按"清零"处理。判据与操作口径写进
`docs/current/testing.md` §静默异常。

### 3.4 provider 三胞胎应合并成参数化引擎

`plan-2026-09-17-protocol-payment-extractor-consolidation.md` §6 **明确否决**单一参数化
引擎（会把 35 处非等价差异压成 `if provider == ...` 配置地狱），阶段 2 还需真机回放。
本轮只做阶段 1 的纯传输层（P0-1），并把 docs 里的"有意没做"补全。

## 4. 新增/收紧的门禁

| 门禁 | 新行为 |
| --- | --- |
| `test_settings_catalog_paths_parity.py` | 109 个设置字段的配置路径必须存在于 `config.example.json`（`{provider}` 按任一 provider 解析）；三个已删的 `codex_oauth.*` 装饰项按**代码行**钉死 |
| `test_coverage_gate.py` | CI 必须带 `--cov-fail-under=70`，且与 `docs/current/testing.md` 的数字一致；`pytest.ini` 不得带 |
| `test_session_batch10_parity.py` + `stage1_batch10_head.py` | 冻结体差分 + blik 闸门的变异证明 |
| `AccountMachineStateTests.cs` | 机器值双向优先、遗留回退、RT/AT/2FA/优惠四列按码取色 |
| `lane_coupling` 基线 | payment→registration 15 → **7**（只剩 `sentinel` / `auth_headers` 两条 documented 允许） |
| `import_layer` 基线 | 78 → 80 对（paypal/upi_link 直连 `proxy`），逐对计数在测试里带理由 |

## 5. 验证快照

- `python -m pytest tests -q -n 4 --dist loadfile` → **7669 passed / 12 skipped / 1367 subtests**
- `python -m pytest tests -q`（带 `--cov`）→ **72%**（57721 / 16107）
- `dotnet test GPTRegisterTool.slnx -c Release` → **787 passed**，`dotnet build` 0 warning
- `pyright` → 0 error；`format_guard` / `line_ending_guard` / `docs_consistency_scan` /
  `module_coverage_check` / `architecture_scan` / `config_schema_check` /
  `ipc_schema_check` / `result_schema_check` 全绿
- C# LSP（pi-lens）对 `Models/AccountMachineState.cs` 与
  `tests/SmsWorkbench.Tests/AccountMachineStateTests.cs` 的 `fresh` 探针为 0 诊断；
  它的 session 缓存里残留过两条该文件创建前的错误记录，与 `dotnet build`/`test` 冲突，
  按后者为准（干净重建 + `--filter AccountMachineStateTests` 42/42 通过）。

## 6. 与既有文档的关系

- `plan-2026-09-17-protocol-payment-extractor-consolidation.md` §11（batch 10 落地记录）
- `plan-2026-10-25-wpf-row-status-decoupling.md` 文末（P2-3 落地记录）
- `docs/adr/0011-proxy-redaction-helper-ownership.md` 的 2026-11-20 修正案
- `docs/rules/rule-13-proxy-string-authority-single.md`（权威名单 5 → 8）
- `docs/current/ui-architecture.md` §4.1 / §8、`docs/current/testing.md`（覆盖率与静默异常两节）

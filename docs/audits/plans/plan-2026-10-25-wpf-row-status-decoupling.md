# Plan: 把账号行状态判定从 WPF `Views/` 上移（P2-3 立项）

| 项 | 内容 |
| --- | --- |
| 状态 | **已落地（2026-11-20）**；落地记录见文末「落地」一节 |
| 立项日期 | 2026-10-25 |
| 来源 | 2026-10-25 扫描 §P2-3（协议注册 / 代理池 / WPF 三模块扫描） |
| 相邻已落地项 | P1-2 状态判定去本地化（同批落地，见 `landing` 段落与 `docs/current/ui-architecture.md`） |

## 1. 问题

`MainWindow` 是 5,139 行 / 16 个分部类的窗口级实现，决策规则散落在 `Views/`：

| 位置 | 现在承担的判定 |
| --- | --- |
| `Views/AccountStatusInterpreter.cs` | 把后端机器码（`at_invalid` / `paypal_ready` / `k12_joined` …）翻成**展示串** |
| `Views/AccountGridPresentation.cs` | 状态串 → 徽章颜色（`success` / `danger` / `warn` / `info`） |
| `Views/RegistrationStatusPresentation.cs` | `partial_registered` 语义 + "需要关注" |
| `ViewModels/RegisterViewModel.cs` | "这行还要不要注册" |
| `ViewModels/ExportViewModel.cs` | "这行能不能导出" |
| `ViewModels/PoolViewModel.cs` | 计数与筛选 |

2026-10-25 的 P1-2 已证明这条链的脆弱性：展示串经 `Loc.T` 本地化后，
六个判定点里有四个仍在比较中文源字面量 ⇒ 英文界面下"已注册"计数归零、
已注册邮箱被重新排进注册批次。当批修复用 `Loc.DisplayContains` 把
**语言**变量消掉，但**根因没动**：判定仍然解析展示串。

判定解析展示串的代价是可以量化的：

* 一次文案改写（"支付完成✅" → "已支付"）会静默改变业务判定，而
  `tests/workbench/test_localization_catalog.py` 只保证**每个键有翻译**，
  不保证"翻译后的串仍被判定认识"；
* 同一条信息（是否已注册）在 Python（`registration_status` /
  `at_probe_status`）与 C#（展示串）各存一份，两边可以独立漂移；
* 每加一种状态，要同时改 `AccountStatusInterpreter`、`StatusSeverityConverter`
  和若干 `Contains` 列表 —— `AccountGridPresentation.cs:16-60` 现在就是
  中英两份手写清单，注释已经写明"converter 必须对着界面实际显示的字面量"。

## 2. 目标

把"这一行是什么状态"变成一个**机器态**事实，展示层只负责渲染：

1. `PoolRow` 持有从后端原样带来的状态码（已有 `RegistrationStatus`、
   `AccessTokenProbeStatusCode`；需要扩到 `payment_status` / `promotion_state`
   / `import_state`）。
2. 新增 `Contracts/AccountRowState.cs`（无 WPF 依赖，可 xunit 直测）：
   `IsRegistered(row)` / `NeedsAttention(row)` / `IsExportable(row)` /
   `Severity(row)` 全部只读机器态；展示串只作为**未知状态**的兜底。
3. `Views/` 里只剩下 `Display*` 渲染函数；`StatusSeverityConverter` 变成
   `row.Severity` 的绑定。
4. `RegisterViewModel` / `PoolViewModel` / `ExportViewModel` 的判定改为委托
   `AccountRowState`。

## 3. 判据（做没做成，可自动验）

* `grep -c "Contains(\"已" SmsWorkbench/Views SmsWorkbench/ViewModels` 为 **0**
  （当前 P1-2 后仍有 6 处，`AccountGridPresentation.cs` 全部是徽章颜色）。
  governance 守卫 `tests/governance/test_wpf_localized_decision_guard.py`
  先覆盖 decision 层；本项落地后覆盖到 `Views/`。
* 新增 `tests/SmsWorkbench.Tests/AccountRowStateTests.cs`：每个后端机器码一条
  用例，**不经过 `Loc`**（即 `Loc.Instance.SetLanguage(En)` 前后断言相同）。
* Python ↔ C# 契约：复用 `tests/fixtures/` 的跨语言 fixture 形式
  （现有 `promotion_status_cases.json` / `proxy_input_cases.json` 同类），
  由 `store/normalize._status` 的取值集合生成 `account_row_states.json`，
  两侧各自断言"每个机器码有定义"。
* `dotnet test` 用例数只增不减；`scripts/dotnet_warning_ratchet.py` 保持 0。
* 行为不变式的对照：用现有的 `tests/workbench` 布局/展示测试
  （`AccountGridPresentationTests`、`PoolViewModelTests`、`ExportViewModelTests`、
  `RegistrationStatusPresentationTests`）作为**回归基线**，改前全绿、改后全绿。

## 4. 明确不做

* **不改 Python 的状态产出**。本项只消费已有机器码；`store/normalize._status`
  是唯一 owner（`docs/current/account-health.md` 已声明）。
* **不动 `Loc` 的键形态**。中文源串仍是 key，展示文案继续由
  `tests/workbench/test_localization_catalog.py` 兜底；本项只切断"判定读展示串"。
* **不合并 `MainWindow` 分部类**。窗口拆分已由
  `plan-2026-10-24-mainwindow-and-handlers-decomposition.md` 落地；本项只搬判定，
  不碰 XAML 与控件。
* **不为迁移顺手改文案**。任何展示串变化都会让本项的效果无法与原基线区分。

## 5. 分步（每步独立可回滚）

| 步 | 内容 | 验收 |
| --- | --- | --- |
| S1 | `Contracts/AccountRowState.cs` + 机器码 fixture（只读，无调用方） | 新测试全绿；`dotnet test` 无回归 |
| S2 | `PoolRow` 补齐机器态字段（`payment_status` / `import_state`），由 `MainWindow.Pools` 原样填充 | 池加载测试断言字段被填 |
| S3 | `RegisterViewModel` / `PoolViewModel` / `ExportViewModel` 改委托；展示串保留为兜底 | 现有 ViewModel 测试全绿 + 双语新用例 |
| S4 | `StatusSeverityConverter` 改读 `Severity`，删除 `AccountGridPresentation.cs` 的中英清单 | 徽章快照测试（现状 0 覆盖，需先补） |
| S5 | 守卫扩到 `Views/`，删除兜底前的死列表 | governance 守卫覆盖 Views；`grep` 判据为 0 |

## 6. 风险与待答

* **未知状态兜底**：后端新增状态码而桌面未更新时，`AccountRowState` 若返回
  `false`，该行会被当作"未注册"。S3 必须保留"展示串兜底"并**记一条日志**，
  否则新状态会静默掉出"已注册"计数 —— 与 P1-2 的事故同形。
* **`AccountType.Contains("Session")`**：`PoolViewModel.IsRegistered` 目前靠
  `AccountType` 子串判断来源，属另一类"展示串判定"；S2 应改为显式的
  `source_kind` 字段。待答：`AccountType` 是否有跨语言契约（当前是硬编码中文）。
* **工作量**：S1–S5 约 2–3 个工作日 + 1 天回归；S4 的前置是补徽章快照测试。

---

## 落地（2026-11-20）

判定整体搬到 `SmsWorkbench/Models/AccountMachineState.cs`，输入是后端已持久化的
机器字段（`registration_status`、`refresh_token_status`、
`at_probe_status_code`、`promotion_state`、`payment_qualification_state`、TOTP
布尔），输出是四个谓词加五个徽章色。逐步对照本文 §1 的判定点：

| 原位置 | 现在 |
| --- | --- |
| `Views/AccountStatusInterpreter.cs`（机器码 → 展示串） | 不变，仍只做展示翻译 |
| `Views/AccountGridPresentation.cs`（状态串 → 徽章色） | `AccountMachineState.*Severity`；`StatusSeverityConverter` 降级为**遗留行回退**（`SeverityFor` 公开以便单测） |
| `Views/RegistrationStatusPresentation.cs`（"需要关注"） | `NeedsAttention` 委托机器态 |
| `ViewModels/RegisterViewModel.cs`（"这行还要不要注册"） | `StatusSaysRegistered` |
| `ViewModels/ExportViewModel.cs`（"这行能不能导出"） | `CanExport`（AT / 机器 registered / 有资格判定），标签只保留 `待支付`/`PM已创建` 两个遗留分支 |
| `ViewModels/PoolViewModel.cs`（计数与筛选） | `IsRegistered` / `NeedsAttention` |

验收（本文 §3 的判据）：

1. **机器值优先，双向**：`registration_status=pending` + 文案 `已注册` → 不计入已注册；
   `registration_status=registered` + 一段任何语言目录里都不存在的文案 → 照常计入。
   这是"改文案不再改行为"的负向证据。
2. **遗留行仍可判定**：没有机器字段的行走原来的 `Loc.DisplayContains` 回退；
   `LocalizedStatusDecisionTests` 与 `RegistrationStatusPresentationTests` 一行未改
   仍全绿。
3. **顺带修掉的实测缺陷**：RT 徽章拿原始码（`no_rt` / `oauth_present`）去匹配中英文
   展示串，一个都匹配不到 ⇒ 缺 RT 的行渲染中性灰。现在按码取色。
4. **结构**：`PoolRow` 从 `MainWindow.xaml.cs`（395 行，已到 383 行上限）搬到
   `Models/PoolRow.cs`；`MainWindow.xaml.cs` 329 行。
5. 测试：`dotnet test` **787 passed**（新增 `AccountMachineStateTests` 42 例）；
   Python 侧 `tests/workbench` 293 passed / 2 skipped。

**没做**：`AccountGridPresentation.AccountGridOrdering` 的排序键仍来自 DataGrid 列的
`SortMemberPath`（`PromotionStatus` 展示串），虽然 `PromotionStatusPresentation.SortRank`
内部已优先读 `PromotionState`；换成机器键要动 XAML 排序契约与分页，属独立改动。

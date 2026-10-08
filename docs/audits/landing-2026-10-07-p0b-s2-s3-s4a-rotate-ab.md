# 落地记录：P0-B S2（换出口）+ S3（预注册 A/B）+ S4a（unknown 偏向换出口）（2026-10-07）

> 落地来源：`docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` §6 的 **S2 + S3**，
> 以及 §7 拍板后的 **§7-5**（`unknown` 偏向换出口）。
> 前置：S0 + S1 见 `landing-2026-10-07-p0b-s0s1-challenge-verdict.md`。
>
> **S4b（浏览器交棒）未落地**，理由见 §4 —— 那不是保守，是纪律。

---

## 1. S2 — 同池换出口一次

**开关**：`registration.edge_challenge_rotate_exit`（默认 **false**）。它改控制流（在另一个出口上多发一次有状态请求），所以要自己的 A/B。

**分层**（这是本项最容易做错的地方，所以写死）：

| 部分 | owner | 为什么在那里 |
|---|---|---|
| 重试一次 + 「每请求最多一次」上限 | `http_client.request_with_retry` | 它持有响应与 attempt 预算 |
| 决策 + 真正换出口 | 注册 handler，经 session 上的 `_openai_edge_challenge_hook` | `s.proxy`、池游标、审计计数、`clear_session_circuit` 都归它 |
| 计数 | `proxy_audit` 的 `edge_challenge_*` → 批次报告 funnel 的 `edge_challenge` 块 | 它们是**计数不是身份**：换前/换后的出口是 sticky-session 凭据，永不入审计 |

三条要点：

1. **§3.5 不变式 1 是结构性的，不是靠自觉**：`_apply_edge_challenge_policy` **不写熔断**，
   熔断由调用方在它返回**之后**才写。于是「换成功 ⇒ 不留下熔断」「换失败 ⇒ 回落到旧行为」
   同时成立，不需要额外判断。
2. **重试是逐字节重放**（同 method/URL/headers/body）：挑战的意思是「这个出口被拒了」，
   不是「请求错了」。
3. **单槽池短路且如实报告**：没有 sticky session id 的供应商 URL 不变 ⇒ 记
   `edge_challenge_rotate_failed`，**不**记成一次成功旋转。否则 A/B 的 `rotate` 臂
   就是换了个标签的 `observe` 臂。

**一处刻意不复用**：`rotation_generation` 保持它既有的「批次级池游标代数」语义，流程内旋转
不混进去；流程内是否真的换了出口由 `edge_challenge_rotations` 表达。

## 2. S3 — 预注册 `p0-2b-inflow-challenge-handoff`

`scripts/registration_ab.py` 新增该实验，三臂 `observe` / `rotate` / `handoff`（§4 的设计原样），
判定规则与 §4.1 的**无效结果判据**都实现了：

| 判据 | verdict | 为什么不是别的 |
|---|---|---|
| 任一臂的 `edge_challenge` 计数缺失 | `inconclusive` | 报告没测，不是 0 |
| 三臂 `hits` 全为 0 | `not_judgeable` | §4.1：空窗口**测到的是零**，不是「无效果」 |
| `rotate` 臂命中挑战但 `rotations == 0` | `not_judgeable` | 单槽池 / 开关未生效 ⇒ 该臂是 `observe` 的副本 |
| `rotate` 臂出现控制臂从未有过的失败类别 | `stop_all_arms` | §4 的停止规则优先于速率 |
| `rotate` 速率高出 0.05 以上且无新类别 | `favor_rotate` | |
| 低 0.05 以上 / 区间内 | `keep_default_off` / `inconclusive` | |

两个设计取舍：

- **P0-2b 故意不注册机制行**。它的机制检查是**有条件的**：换出口那行只在确实发生挑战时
  才出现。用 P0-A 那道常开的机制门禁会把「窗口没发生挑战」误判成 `manipulation_failed`。
  所以机制检查写在判定规则里，返回 `not_judgeable`。
- **`mailboxes_consumed_per_registered` 是派生量**（`attempted / registered`），不是第二个测量：
  一次 attempt 消耗一个新鲜邮箱槽，所以它就是计划里那个「真正的收益指标」（被抳回的挑战 =
  没被烧掉的邮箱）。报告里同时给出，因为操作者读的是它。

**观测管道补齐**：`registration_funnel` 新增 `edge_challenge` 块
（`hits` / `unknown` / `rotations` / `rotate_failed`），从每行的 `proxy_audit` 汇总 ——
没有它，§4 的指标就是空话。

**线上 A/B 仍未跑**：工具不发起任何流量，`p0-2b` 的 H1 需要操作者在受控批次上跑
`observe` / `rotate` 两臂。

## 3. S4a — §7-5：`unknown` 偏向换出口，单独计数

判据返回 `unknown`（既非 `challenge` 也非 `not_challenge`）时，按代价不对称偏向**换出口一次**，
但计入 `edge_challenge_unknown` 而不是 `edge_challenge_hits` —— 计划明写「否则 §4 的操纵检查
会被稀释」。

诚实说明：从 `request_with_retry` 出发这条分支**当前不可达**（策略只在 403/429 进入，而判据只在
非 403/429 上回答 `unknown`）。分支写下来并单测，是为了契约完整、下个更宽的调用方一出现就可用；
hook 签名因此从 `(session, rotate_allowed)` 变成 `(session, verdict, rotate_allowed)`。

## 4. S4b 未落地：拍板已定，能力缺口仍在

§7 四项已拍定（记录在 `plan-2026-10-05` §3.4.2）：**同进程内存直传不落盘** ·
**复用 `browser_flow` + 显式开关** · **回程两个都实现、默认回协议** ·
**`unknown` 偏向换出口并单独计数**。

但拍定的默认语义是「浏览器只闯关、然后回协议」，而现有浏览器驱动**没有这个模式**：
`registration_drivers/browser_flow/orchestrator.run_browser_registration` 接受
`driver_name` / `proxy` / `password` / `mailbox` / `config`，**不接受 cookie jar / storage state**，
也没有「导航 → 闯关 → 导出 cookie → 停」的入口 —— 它是一次完整注册。

⇒ 本轮**不落地** `registration.edge_challenge_browser_handoff`。一个打开了也无人执行的开关
就是**假开关**，正是 P0-A 那道机制门禁要挡的同一类缺陷。S4b 的能力
（`adopt(storage_state) → navigate(resume_url) → wait-cleared → export(cookies) → stop`）
落地并跑过 `handoff` 臂的受控对照后，再连同开关一起提交。

## 5. 验证证据（本轮）

| 门禁 | 结果 |
|---|---|
| `pytest -q`（全量） | **6016 passed, 8 skipped** |
| `ruff check`（`sms_tool/` `tests/` `scripts/registration_ab.py`） | All checks passed |
| `ruff format --check`（本轮改动文件） | already formatted |
| `scripts/import_layer_ratchet.py` | OK（288 / 31 对 / 11 mutual / 60 minority；185 delayed） |
| `scripts/delayed_import_ratchet.py` | OK: 420 ≤ baseline 420 |
| `scripts/config_key_ratchet.py` | OK（`registration=37`, `email_registration=33`） |
| `scripts/bare_print_ratchet.py` | OK（514，未新增裸 print；新行走 `operator_output.emit`） |
| `scripts/unused_import_ratchet.py` | OK（354 ≤ 355） |
| `scripts/docs_consistency_scan.py` | passed |
| 新增离线测试 | `tests/test_edge_challenge_rotate_exit.py`（22 例，含三条不变式的变异用例）、`tests/test_registration_ab.py` 的 `InflowChallengeHandoffExperimentTests`（9 例）、`tests/test_registration_funnel.py`（2 例） |

`pytest` 数字说明：S0/S1 收尾时是 5983，本轮新增 33 例。

## 6. 附带发现（本轮未改）

- `scripts/batch_enable_2fa.py` 用 `isinstance(CFG.get("chatgpt"), dict)` 判配置段
  —— 与 P1-D 是**同一类缺陷**（生产段是 `mappingproxy`，`dict` 判据恒假 ⇒ 静默回落）。
  该文件本轮未触碰，未纳入本次改动；建议单独一轮按 P1-D 的收尾建议做一次全仓
  `isinstance(..., dict)` 扫描 + 冻结配置回归测试。
- pyright 在 `sms_tool/batch_runner.py` / `registration_outcome.py` /
  `registration_progress.py` / `scripts/batch_enable_2fa.py` 报的若干 `Optional` 访问是
  **既存**的（这些文件本轮均未修改），本轮未引入新诊断。

## 7. 相关

- `docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` — 契约、开关、A/B、§3.4.2 凭据流图与 S4b
- `docs/current/protocol-registration.md` — "In-flow edge-challenge judgement" + "Rotating the exit once"
- `docs/current/registration-ab-runbook.md` — P0-2b 行、机制表例外、判定与停止规则
- `sms_tool/http_client.py` — `_apply_edge_challenge_policy` / `EDGE_CHALLENGE_HOOK_ATTR`
- `sms_tool/registration_handlers.py` — `_on_edge_challenge` / `_install_edge_challenge_hook`
- `tests/test_edge_challenge_rotate_exit.py` — S2 + S4a 的全部断言

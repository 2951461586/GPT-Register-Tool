# 落地记录：2026-11-02 broad-except 预算放宽的复核与 transport 收窄

- 来源：2026-11-02 全仓扫描（协议注册 + WPF 前端）收口时对未提交重构的审计
- 日期：2026-11-02
- 回归：**pytest 7394 passed / 10 skipped / 1345 subtests**
- 桌面回归：**dotnet 731 passed / 0 failed**；`dotnet build` 0 warning / 0 error
- 门禁：**13 个 ratchet/scan 全绿**；`broad_except` 959/158（预算 960/158，whitelisted 9）
- 结论来源：本文件是**对已发生变更的复核记录**，不是提案。变更本身在提交 `b437d20`（收窄）、`e53b291`（记账纠错）与本次复核提交里

---

## 为什么要有这份记录

`scripts/broad_except_baseline.json` 的 `policy.max_silent` 从 **155 放宽到 158**，这是该轮重构里唯一一次预算放宽，而且放宽在「静默吞异常」这个更危险的轴上。放宽本身**合理**（见下），但它此前没有被记录在任何地方：`--write` 会用实测值直接覆盖 `policy`，所以下一次 re-freeze 之后，这个数字**为什么**是 158 就彻底无从追溯。

同时暴露出两个更深的问题，本文件一并记录处置：

1. **whitelist 的 `broad`/`silent` 数字从来没被校验过。** 校验只查「有没有 reason」和「文件是否还有 broad handler」。实测发现 9 条里有 2 条是陈旧数字 —— `providers/sms_vendor_api.py` 声明 `broad 4 / silent 0`，实测 `5 / 3`；`registration/preflight.py` 声明 `6 / 4`，实测 `5 / 4`。一个只能回答「是/否」的记录，无法发现文件在被批准之后又变了。
2. **`--write` 会静默丢掉复核结论。** 与 2026-11-01 修过的 `lanes` 是同一个缺陷（当时 lane 预算被 `--write` 抹掉，门禁变装饰品），说明「复核决策 vs 实测值」这条分界当时只修了一处。

---

## 结果总表

| # | 落地内容 | 主要改动 | 验证 |
| --- | --- | --- | --- |
| 1 | broad handler 收窄到 kernel 叶子 | 新增 `sms_tool/kernel/transport.py`（`TRANSPORT_ERRORS = (RequestsError, OSError, TimeoutError)`）与 `kernel/flags.py`；**7 处** `except Exception` → `except TRANSPORT_ERRORS`，**1 处** → `except KeyError`。理由是这个仓已经吃过一次亏：`except Exception` 会把「响应结构变了导致的 `KeyError`」也当成网络失败，既掩盖缺陷、又落进 funnel 的 `unknown` 桶，同时批次还在继续消耗邮箱 | 全量 `tests/registration` 2011 passed；`pyright` 0 error |
| 2 | 其余 broad handler 加显式复核标记 | **51 处** `# broad-except: compat`，全部为新增（`git grep HEAD` = **0 处**）。已核实**零行为变更**：46 处对应的行原本就是 `except Exception`，被删的 `except TRANSPORT_ERRORS` = 0 | 见下「这项标记不是豁免」 |
| 3 | **预算放宽的书面记录** | `policy.budget_history` 新增一条 `max_silent 155 → 158`（本文件即其 `record` 引用）；`build_baseline` 改为像保留 `lanes` 一样保留 `budget_history`，否则 `--write` 会把它抹掉 | 新增 `test_a_refreeze_cannot_drop_the_budget_history`、`test_shipped_budget_history_is_well_formed_and_cited`、`test_the_shipped_budget_history_matches_a_cap_that_actually_moved` |
| 4 | whitelist 数字纳入校验 | `broad_except_ratchet.validate` 增加「声明值必须等于实测值」，失败信息给出两侧数字与「重新测量，不要 re-freeze」的处置。`--write` **故意不刷新** whitelist 数字 —— 那是复核证据，只能重新复核 | 新增 `test_whitelist_counts_that_disagree_with_the_tree_fail`、`test_a_matching_whitelist_entry_passes`；两条陈旧条目已重新测量 |
| 5 | `import_layer` 记账纠错：`kernel` 入节点表 | `scripts/import_layer_ratchet.py` 的 `SUBPACKAGES` 增加 `kernel`。此前任何 `..kernel.<leaf>` 导入都并进 `sms_tool` 节点：对**子包**来源是对的，对**根模块**来源是错的（导入方与目标都映射到 `sms_tool`，边直接不可见） | 模块级边 438 → 473（**+35 是原本就存在、只是没被计入的 root → kernel 依赖**），目录对 64 → 78；`registration → sms_tool` 91 → 56、`accounts → sms_tool` 44 → 36 读到真实值；延迟导入 469 → 473 |
| 6 | 厂商目录改走 backend 通道，并接上 `ports` 端口 | 删除 703 行 `SmsWorkbench/Interop/SmsProviderCatalogClient.cs`；新增 `sms_tool/providers/sms_vendor_api.py`；`providers/ports.py` 增加 `request` 端口，使适配器**接收**共享重试传输而不是在模块级 `import http_client` | `providers → sms_tool` 保持 10（新增模块没有让这对变宽）；`tests/workbench/test_sms_vendor_api.py` |
| 7 | `providers/__init__` 的 `__all__` 变成真的 | 该 `__all__` 列的是子模块，此前完全靠 import 系统的隐式子模块回退才能工作，声明烂掉也没人知道（pyright 报 7 条 `reportUnsupportedDunderAll`）。改为 `TYPE_CHECKING` 块（静态可见）+ PEP 562 `__getattr__`（运行时惰性），白名单用导入时快照的不可变元组 | pyright **7 → 0 warning**；包导入仍然 **0 个子模块被加载**；`p.__all__.append("os")` 无法扩大可导入面 |
| 8 | 模块覆盖补齐 | `sms_vendor_api.py` 加入 `scripts/directory_map_groups.json` 的 "Mailbox and phone inventory" 并同步 `docs/directory-map.md` 的 Files 列与正文 | 覆盖 324 → **327/327**，0 unclaimed |

---

## 预算为什么可以放宽到 158

`+5 broad / +3 silent` **全部来自一个新文件** `sms_tool/providers/sms_vendor_api.py`，不是某处 handler 被放松了：

- 3 个静默 handler 中没有一个是「吞掉失败」：两个是 `finally` 里的 `session.close()`（清理失败不能覆盖上面 `except` 已经报告的真实结果），一个是 `session.trust_env = False` 的能力探测（旧版 curl_cffi 没有这个属性）。
- 该文件在 whitelist 里有独立条目和逐条理由，**不属于任何 lane 配额**；协议注册 lane 自己的 `max_silent` 仍然钉死在 **0**。
- 同一轮里 `max_broad` 是**向下**的（962 → 960）。

所以这次放宽的实质是「一个新适配器带来了 3 个设计上就该静默的清理点」，而不是预算被侵蚀。真正的问题是它当时没留下任何记录 —— 本文件补的就是这个。

---

## 这项标记不是豁免机制（复核证据）

`# broad-except: compat` 是**纯注记**，`scripts/broad_except_ratchet.py` 用 AST 计数、**不读任何注释**；`grep -r broad-except scripts/` 只命中 `print` 格式串。它的作用是给下一次预算复核的人一个可 grep 的线索。

复核时的三条判据：

| 问题 | 证据 |
| --- | --- |
| 是不是这批改动新引入的？ | `git grep 'broad-except:' HEAD` = **0 处**，51 处全部未跟踪/未提交新增 |
| 有没有偷偷放宽异常范围？ | 被删的 `except TRANSPORT_ERRORS` = **0 行**；46 处标记对应的行原本就是 `except Exception` |
| 是不是拿注释绕过门禁？ | 不是。该轮同时新增 **7 处**收窄与 1 处 `except KeyError`，预算在 broad 轴上是**下降**的 |

顺带确认：`broad_except_ratchet` 的 lane 机制与本次放宽无关 —— `max_silent` 是全局值，lane 内的协议注册仍是 0。

---

## 遗留与后续

1. **`# broad-except: compat` 这个约定现在有文档了：** [`docs/rules/rule-26-broad-except-marker.md`](../../rules/rule-26-broad-except-marker.md)（本轮补上，规则号接在现有 25 条之后，编号连续性与索引由 `tests/governance/test_architecture_rules.py` 钉住）。规则里写清了三件此前只能靠口传的事：先收窄（`kernel/transport.TRANSPORT_ERRORS`）再考虑保留；标记是给人看的、**不是许可**，许可全部在 `broad_except_baseline.json`；以及 `--write` 可以重新测量但不可以重新决策（`lanes` 与 `budget_history` 必须存活、whitelist 数字必须重测）。
2. **`broad_except` 的全局 `max_silent` 只剩 0 余量**（158/158）。下一次任何静默 handler 都会失败 —— 这是有意的压力，但也意味着下一个合法的清理点必须连同一次有记录的放宽一起提交，请照本文件的样子留痕。
3. 本次复核未覆盖 `services/`：`broad_except_ratchet` 的 `TREES` 是 `("sms_tool", "services")`，但 `services/protocol-payment/` 的 5 个 handler 全部落在既有 `files` 冻结值内，本轮无变化。

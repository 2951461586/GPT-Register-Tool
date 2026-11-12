<!--
PR 模板：这个仓库的门禁很多，空白 PR 描述会让 review 变成"猜你跑过什么"。
下面每一节都不长，但请在提交前真的填。
-->

# 这个 PR 做了什么

<!-- 一到三句。如果修的是缺陷，写清"改前会发生什么 / 改后会发生什么"。 -->

## 落点

| 项 | 值 |
| --- | --- |
| 层 | <!-- UI / CLI / 应用工作流 / 领域契约 / provider-持久化 / 构建与门禁 --> |
| 主要模块 | <!-- sms_tool/… 或 SmsWorkbench/… --> |
| 是否改协议行为 | <!-- 是 → 见下"受控对照" / 否 --> |
| 是否改发布产物 | <!-- 是 → 说明是否需要重打安装包 --> |

## 判据（本仓库最看重这一节）

- [ ] 缺陷有**可复现的判据**（哪一行日志、哪个字段、哪个断言），不是"感觉不对"
- [ ] 修的是**根因**，并在代码里留下了"为什么不能改回去"的注释
- [ ] 如果新增/改动了跨层取值（指纹、`oai-*` 头、Sentinel 字段、代理字段），
      有一条门禁把两侧钉在一起（参考 `tests/registration/test_sentinel_proof_parity.py`）

**受控对照**（仅当改了协议行为时填）：

| 项 | 值 |
| --- | --- |
| 实验 id / 开关 | <!-- registration.* 键名，,或"无开关、直接改" --> |
| 对照口径 | <!-- 同一出口、同一邮箱源、n=?，或"未做线上对照" --> |
| 结果 | <!-- e.g. 10/10 registered、AT 200 ×10、promo ×10 --> |

## 验证

- [ ] `python -m pytest tests -q`（或受影响的子集）——贴结果行
- [ ] `python -m pytest tests/governance -q`（改了文档/门禁/新模块时**必跑**）
- [ ] `python -m sms_tool --doctor` 仍全绿（改了配置键 / 依赖 / Sentinel 时）
- [ ] `dotnet test GPTRegisterTool.slnx -c Release`（改了 C# 时）
- [ ] 新增模块已登记到 `scripts/directory_map_groups.json` + `docs/directory-map.md`
      （否则 `module_coverage_check` 会在 CI 红，而本地 untracked 时跑不出来）

## 风险与回滚

<!-- 最坏情况是什么？怎么一键回滚（配置项 / 开关 / revert 一个 commit）？ -->

## 检查表

- [ ] 没有提交凭据、cookie、token、可用邮箱、真实代理地址（`precommit_guard` 会拦，但请先自己看一眼 diff）
- [ ] 文档已更新：`docs/current/`（契约变了）、`docs/audits/`（一次扫描/落地）、
      或 `docs/releases/`（发版）——按改动性质选一处，不要三处都写
- [ ] 本 PR 是一个逻辑改动；如果混了格式化/重命名，说明为什么不拆

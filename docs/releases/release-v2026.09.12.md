# v2026.09.12

本版本落地 2026-09-12 全项目深度扫描的第一批结构性收口：10 项快修 + 8 个重构候选
中的大部分。主线是把"同一语义两处实现、各自漂移"的模式逐个收敛为单一 owner，
并用跨语言契约夹具钉死。已知真 bug（`_run_stage` 错误分类、测活双探、测活队列
浏览器槽回归）在本版修复。

## 注册链路

- **`_run_stage` 异常分类修复**（潜在真 bug）：`registration_handlers.py` 此前只把
  NameError/AttributeError/ImportError/KeyError/TypeError 归入 `_internal:`，其余
  一律落 `_transport:` 计为可重试网络错误——IndexError 等编程错误会被当成网络抖动
  重试（"报错名掩盖真因"的镜像）。现按异常类型显式分类，并与
  `error_classification.INTERNAL_ERROR_MARKERS` 同步扩展（indexerror/recursionerror）。
- **浏览器失败结果入约（候选3-1）**：新增
  `registration_result.build_registration_failure_result`，浏览器编排器 7 处手拼
  失败 dict（键集各自漂移：首处缺 `email`、`mailbox_required` 缺 `failure_class`）
  全部改走与成功路径相同的装配，补齐全量 `COMMON_RESULT_KEYS` + 重试决策。
- **浏览器池身份感知复用（候选8）**：池复用路径曾静默丢弃 `browser_identity`
  （`profile_id` 只在 relaunch 生效）。现记录常驻进程的启动身份，身份或代理变化
  触发干净重launch；四处静默 close 改为 `logger.warning` 可观测。

## 账号测活

- **一键扫描去双探（真 bug）**：扫描对每个账号已跑过 wham 探针，配额刷新此前会再探
  一遍（双份快照、双倍 Cloudflare-401 暴露、两套存储可能互相矛盾）。
  `refresh_local_quota_statuses` 新增 `fresh_probes`：确定性判定（active/token_invalid/
  account_deactivated）直接复用，仅传输未知结果重新探测。
- **测活队列浏览器槽修复（真 bug）**：`account_health_queue` 浏览器回退槽 acquire
  从 `timeout=1.0` 改为 30s 有界等待——原值复刻了 account_recovery 已修复的
  "并发满即跳过"病。
- **快照治理**：`runtime/account_liveness_batches` 终态写入后自动清理（保留最新
  20 份）；快照写失败从静默 `except OSError: pass` 改为 `logger.warning`；WPF
  `TryReadLatestLivenessSnapshot` 终态快照优先，超过 10 分钟的非终态快照视为崩溃
  残渣不再当作当前答案。
- **终态判定唯一 owner（候选3-1）**：新增 `accounts/account_terminal.py`——
  可移除终态集（补入 `token_revoked`，掉号行此前永远清不掉）与停用文本标记的唯一
  owner；`account_cleanup`/`account_scan`/`account_recovery` 改为消费同一词汇，
  与 `store/normalize` 被 C# pin 的字面量副本加防漂移测试。

## 优惠状态（查优惠）

- **跨语言机器契约（候选4）**：新增 `sms_tool/promotion_states.py` 机器状态词汇
  （trial_eligible/subscribed/free/auth_invalid/probe_failed/unknown）。此前 C#
  靠 `Contains("可试用")+Contains("plus")` 子串猜语义，改一个词就碎。现：
  Python 探针结果统一携带 `promotion_state` 并随 `mark_promotion_status` 持久化、
  随桌面读透出；C# `IsTrialEligible`/排序/"有试用"过滤全部机器状态优先（旧行回落
  子串）；`promotion_marker_is_stale`（"401 标记被后续 AT 200 清除"）收为单 owner。
  两侧由 `tests/fixtures/promotion_status_cases.json` 同一夹具钉死。

## 支付链路

- **BA-token/approve-URL 解析单 owner（候选7）**：三份解析（`paypal_protocol`
  返回 None、`pp_link_helpers` 返回 ""、approve-URL 三处拼装）收敛为
  `pp_link_helpers` 单一实现，`paypal_protocol` 保留历史 Optional 语义仅做委托。
- **`PLUS_TRIAL_CAMPAIGN_ID` 常量单源**：`checkout_contract` 定义，8 处字面量
  （payment_capability/paypal_extract/wallet_provider/upi_link/gen_link）全部改引。
- **零_due promotion stage 转公开缝**：`_checkout_update_promotion` →
  `checkout_update_promotion`，`payment_capability` 不再直调私有方法。
- **Rule 6 关闭**：`paypal/orchestrator` 不再 import `gen_pp_link`；链接生成以
  `link_factory` 由命令适配层注入（懒生成语义保留），无 factory 时缺链接为显式
  `paypal_link_missing` 失败而非静默二次下单。
- **Rule 14 决策落档**：`paypal/config_picker` 是 auto-pay 结果持久化的文档化
  owner，enforceable grep 收敛为 `upsert_account` 在 `paypal/` 内仅允许
  `config_picker`（docs/architecture.md "Current Violations" 更新）。
- `protocol_core.ProtocolResult` 增加 `command_id`（继承 `SMS_TOOL_COMMAND_ID`），
  协议支付终端报告与桌面 IPC 包共享同一相关性 ID；并新增双端脱敏一致性测试
  （protocol_core 手搓规则集 vs `sms_tool.sanitizer` 对同一批秘密形状都要打码）。

## 代理池

- **快修（候选5）**：`start_proxy_pool.py` 上游改读 `proxy.json` 分片的
  `proxy.pool`；死键 `config.json proxy_pool.upstreams` 与静默 clash
  `127.0.0.1:7897` 默认一并移除，无上游即报错退出。`proxy_pool.UpstreamProxy.from_url`
  删除 urlparse 回退——垃圾上游曾会变成"活的" `127.0.0.1:1080`，现抛 ValueError。
  `verify_proxy.py` 打印走 `redact_proxy_url`、来源说明改分片、socks5h 给友好提示。
- **跨语言代理解析夹具**：`tests/fixtures/proxy_input_cases.json`——C#
  `ProxyInputNormalizer` 写出的值必须被 `proxy_entry.parse_proxy`（唯一权威，
  规则 13）无损解析，任一侧漂移两侧同时失败。
- **geo 表防漂移**：协议池（`_GEO_PROFILES`）与浏览器池（`BROWSER_LOCALE_PROFILES`）
  共享市场时区/主语言必须一致、VN 迁移必须两侧同时存在（此前"新市场改两处"漏一边
  不报错）。US 时区分歧作为已记录例外；表合一需要运维决策，防漂移先行。
- `PROXY_GUIDE.md` 重写为分片认知版本。

## 日志与文档

- `docs/architecture.md` 退出码表改为与 `BackendResultInterpreter` 实现一致
  （1=参数 2=前置检查 3/其余=运行时），注明 doctor 的计数控不属于此表。
- CI：`pip install` 补 `-c constraints.txt`；新增窄规则 ruff 门
  （pyproject.toml、`.editorconfig` py 段、requirements 不含工具依赖）。
- ignore 规则测试改用 `check-ignore -v` 规则解析，规避 git 2.21~2.22 对例外规则的
  退出码缺陷（本机 2.21 上模板可入库被误报为"被忽略"）。
- **OAuth client id 跨语言契约 pin**：`tests/fixtures/oauth_client_id.json` 同时钉住
  桌面回落值与 Python 默认值。

## 延后项（记录在案）

- 候选1（`registration_handlers` 1133 行 + `account_recovery` 1530 行 god file 拆解）
  与完整失败词汇注册表：需要独立设计会话，避免半迁移状态。
- 协议/浏览器两套 geo 表合一（需运维决策各市场取值）。
- protocol_core 脱敏改读 `sensitive_policy.json`（支付路径重写，现有一致性测试先行
  看守）。
- WPF 邮箱凭据解析迁 Contracts（rule 8 收尾）、`build_installer` 双胞胎脚本合并、
  一次性 `_diag_*.py` 归档、README_EN 内容对齐。

## 验证

- Python：`.venv/Scripts/python.exe -m pytest tests/` — **3477 passed / 6 skipped**
  （608 subtests 含内），新增覆盖：失败结果契约、终态词汇、优惠跨语言夹具、
  代理跨语言夹具、geo 防漂移、双端脱敏一致性、池身份重launch、快照清理、
  fresh_probes 复用、client id 契约。
- .NET：`dotnet test GPTRegisterTool.slnx -c Release` — **372 passed / 0 failed**。
- 守卫：`architecture_scan` / `docs_consistency_scan` / `config_schema_check` /
  `ipc_schema_check` / `ruff check` / 三道秘密扫描全部通过。
- 既有 15 条边界规则复核：Rule 6 由 open 转为已解决；Rule 14 决策落档；
  其余成立。

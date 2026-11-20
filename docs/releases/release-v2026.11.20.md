# v2026.11.20

自 v2026.11.12 以来的收口：**账号测活的两处状态不更新**（一条 2 账号实测线索引出，
连带清掉 160 份重复的机器字段拷贝）、协议传输可换后端与 curl_cffi 0.17.0 档位、
Sentinel 版本对齐 + 可选刷新、CPA 导出回导，以及一批门禁与 CI 卫生项
（发布改由 tag 触发、`ci_watch` 成为推后的最后一道闸）。

## 修复问题

- **测活摘要说正常，状态列却写「失败」** —— 2 账号批次打印
  `测活完成：共 2 个账号，正常 2，掉号 0，其他失败 0`，两行却都显示「失败」。
  根因是账号行的 `error` 停在了更早一轮 `web_session` 重登的传输错误
  `web_session_access_token_probe_failed:unknown` 上：`status` 由**有 AT** 推出
  `registered`（`normalize._status`），而 WPF 的状态列把「error 非空」直接渲染成
  「失败」。三处清理都只认 401 字样，这个传输形态一条都不匹配，于是 200 探活之后
  永远清不掉。现在**探针给出 200 就是判据**：`store.markers.mark_quota_status`
  清掉陈旧 error（列 + `raw_json` + session JSON 一起），
  `account_recovery._mark_successful_relogin` 与
  `account_scan._persist_scan` 的 alive 分支同步放行。确认掉号是唯一不被探针推翻的
  判决，其 error 保留。
- **AT 已经失效，状态列还写「已注册」** —— 同一个缺陷的镜像面：这条写回路径
  **从未写过 `status`**，401 只落在 `quota_status` / `quota.last_result` 上，而这两个
  键没有任何网格列读。实测 `avawhite155379@outlook.com`：探针 401 `invalid_jwt`，
  行里 `status=registered`、`error=''`。现在确定性 401（或探针给出的 `token_invalid`）
  把行降为 `at_invalid`（状态列显示 AT失效）；`unknown` / `status_code=0` 的传输失败
  与已确认掉号都不降级。判定「什么是确定性 401」只有一处定义
  （`store.normalize._probe_is_token_invalid`），恢复链路由复用同一份。
- **陈旧机器字段：160 份重复拷贝** —— `at_probe_status_code` 是**读时推导**字段
  （`desktop.read._access_token_probe_status_code` 按时间戳在
  `account_scan.token_probe` / `token_probe` / `scan.token_probe` /
  `quota.last_result` 之间取最新），却同时被存了一份；顶层 `token_probe` 是
  `quota.last_result` 的同车道副本，只在 200 时刷新。扫描**治好**账号不经过该函数，
  于是 5 行的 401 拷贝比 200 活得久（09-04 起）。现在这两份拷贝不再写入，存量由
  `scripts/repair_stale_probe_markers.py`（默认 dry-run）从 `raw_json` 与 session JSON
  清掉，并**拒绝任何会让推导结果改变的行**——清理不允许移动一个徽章。
- **ReMail iCloud 注册全灭（两个独立原因）** —— 旧域名 `remail.aishop6.com` 自
  11-16 起 NXDOMAIN（DoH 确认 Status 3），失败发生在 TLS 握手，看起来像网络问题；
  同时 iCloud 订单的 `serviceToken` 只存在于订单详情接口，旧守卫在读它之前就拒单，
  每次白烧 36 credits。现在域名与 shard 一起迁移（旧 shard 以
  `mailbox_transport_unavailable` 快速失败），缺失 token 时重读一次订单详情。
  线上验证：两批 5 账号共 **10/10 成功**，全部 `at_http_200`（360 credits）。
- **批次跑完才在 stderr 吐 traceback** —— 健康协调器是 daemon 线程，
  `ThreadPoolExecutor.submit` 在解释器关闭期抛
  `cannot schedule new futures after interpreter shutdown`，未捕获的异常在
  5/5 成功批次的摘要**之后**打一行 traceback，扫日志的人只会读成失败。现在精确匹配
  该消息后安静退出（其它 `RuntimeError` 照常抛出），待处理作业已落盘不丢。
- **预检拦下健康的 curl_cffi 0.17.0** —— `environment_preflight.ps1` 里活着第三份
  版本区间与一个已被表淘汰的 profile 名（`^0\.(15|16)` / `chrome146`）。现在脚本
  **向属主问**：从 `sms_tool/auth_headers.py` 读 `CURL_CFFI_MIN/MAX_MINOR` 与最新
  Chrome 档位，不再有本地副本；顺带修掉 `python --version` 经管道进
  `Select-Object -First` 在 PowerShell 5.1 下自杀、`$LASTEXITCODE` 变 -1 的问题。
- **`ci_watch.py` 自身的三个缺陷** —— `gh run list --commit` 只认真 sha（短 sha 静默
  返回空表，与「CI 从未启动」无法区分）、`--no-wait` 仍等满三分钟、`gh` 的 UTF-8
  输出按 locale 解码在 em dash 上报 `UnicodeDecodeError`；另加对 `EOF` / 502/503/504
  的三次重试，其余失败立即退出。判决改读 run 自己的 `conclusion`（排队中没有 per-job
  结论，`skipped` 不是失败），未完成的 run 返回 2 而不是静默绿。

## 新增功能

- **协议传输可选后端 + 更宽的指纹档位** —— curl_cffi 升到 0.17.0（首个修好
  http2shell brotli 且 certifi 未受污染的版本），profile 表 16 → 23，每个条目都对着
  已装库实测（chrome150/151/152、firefox153/155、safari260/270）；「最新 chrome 条目」
  的断言改为从库推导，依赖升级不再需要改测试，而落后的表仍会红。
  `sms_tool/http_backend.py` 让传输成为选择
  （`email_registration.protocol_http_backend`，默认 curl_cffi；可切 wreq），
  选不动的后端**快速失败**而不是降级；逐账号 TLS 置换在后端不支持 curl options 时
  报 `backend-without-curl-options:wreq`，不再声称做过没做的轮换。
- **Sentinel 版本钉住 + 可选 SDK 刷新** —— 版本串会进 `frame.html?sv=`、`Referer`
  与证明里的 `p[5]`，而 `config.example.json` 曾写 `20260219f9f6`、实际执行的是
  8-10 构建，等于每个账号三处自相矛盾。现在示例指向打包的那份，
  `tests/governance/test_sentinel_version_pin.py` 钉住双向漂移；OpenAI 换路径的自动
  刷新做成显式开关（`email_registration.sentinel_sdk_auto_update`，默认关），
  校验状态码 / 5 MiB 上限 / `SentinelSDK` 标记 / 不是 HTML，按
  `<runtime>/sentinel-sdk/<version>/<sha256>.js` 缓存 —— 门户页永远不会被当成脚本。
- **导出可回导** —— `--import-account-json FILE`（可重复）把导出的 CPA 形状 JSON 读回
  本地库，全程离线，不碰 OpenAI / CPA / SUB2API。三条规则：按 JWT `exp` 去重
  （文档内 + 与库里现有 AT 比），**旧 token 永不覆盖新 token**；`id_token_synthetic`
  标记的伪造 JWT 不入库；单条坏数据不中断整批，每种拒绝都有稳定理由
  （`missing_email` / `missing_token` / `disabled` / `older_token` /
  `newer_token_in_store`）。写入走注册那条路径（原子写 session JSON 再
  `upsert_account`），`register_method="import"` 便于事后区分。
- **行判定改读机器态** —— `Models/AccountMachineState.cs` 成为唯一归属：五个徽章色与
  四个谓词（已注册 / 需注册 / 需关注 / 可导出）从 `registration_status`、
  `refresh_token_status`、`at_probe_status_code`、`promotion_state`、
  `payment_qualification_state` 与 TOTP 布尔取值，文案只作旧行回退；顺带修掉 RT 徽章
  拿原始码匹配展示串、缺 RT 的行渲染成中性灰的实测缺陷。三个手搭对话框改 XAML，
  `StatusSeverityConverter` 只剩回退。
- **推完就看 CI** —— `scripts/ci_watch.py`（`--no-wait` / `--commit` / `--workflow`，
  退出码 0/1/2），并把这条写进 `AGENTS.md`：本地门禁是必要不充分，dotnet 测试、
  warning 棘轮、预检与密钥扫描只在 runner 上跑。
- **发布由 tag 触发** —— `release.yml`：从被 tag 的提交里铺 digest-pinned CPython、
  构建 setup exe + 便携 zip + sha256、三份文件互验后上传。发布说明不是生成的，
  `docs/releases/release-<tag>.md` 是必需输入，缺文件在这里失败而不是发一个空 release。
- **仓库治理补件** —— actions 按 SHA 固定、dependabot（pip + 4 个 NuGet 项目 +
  actions，周更、分组、7 天 cooldown）、`SECURITY.md`、issue/PR 模板（bug 表要求
  version + `--doctor --json` + 完整日志 + 出口 + 邮箱来源五项，空白 issue 关闭）、
  `FUNDING.yml`；`push` 触发面收到 `main`（分支工作由 PR 事件覆盖，重复跑一次
  12 分钟套件的浪费消失）。

## 工程与文档

- `sms_tool` 继续按角色收拢：`browser/`、`desktop/`、`codex/`、`wallet/`、
  `session/` + `phone/`、`protocol-payment/common/`（迁移期用同一批棘轮钉住
  模块级导入面）。
- 单一代理串权威（`proxy/entry.py`）+ 删除三处死设置；删除只有测试在用的 CLI flag 与
  规划器构建函数（参数清单回到命令行，行为不变）。
- 棘轮族改用**一个共享的 baseline plumber**（`scripts/_baseline.py`），并钉住这类
  「门禁悄悄失效」的形态。
- unittest → pytest 迁移**冻结**：现有 unittest 原地保留，给出一次可对照的转换。
- 文档：`docs/current/faq.md` 用实测证据回答八个反复出现的问题；2026-11-20 模块复审
  落地并补门禁覆盖；directory map 补齐新模块（新模块必须先 `git add` 再跑
  `scripts/module_coverage_check.py`，否则 CI 才看得见）。

## 线上验证

- 测活状态列：2 账号批次 **正常 2 / 掉号 0 / 其他失败 0**，两行状态与摘要一致；
  存量按存证的 401 探活回写为 AT失效；机器字段清理 160 行 / 155 个 session 文件，
  跳过 0，495 个账号推导出的判据与清理前逐行一致。
- ReMail iCloud：10/10 注册成功（360 credits），全部 `at_http_200`。
- 健康 worker：同一形状的 5/5 批次，stderr 为空。
- 门禁：本地全量 `pytest` 7709 passed / 12 skipped，`dotnet` 门禁 0 warning；
  发布资产由 tag 上的 CI 构建并互验。

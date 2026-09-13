# v2026.09.14

本版本收口 2026-09-12 专项扫描的三项延后项（HTTP 重试覆盖、代理池 HTTP 上游、
文档指针漂移），并修复协议注册日志输出的四个缺陷（假成功信号、机器通道静默
停写、失败分类失真、人读通道无法归因）。

## 日志输出修复（L1–L4）

### L1 失败轮次先报成功

`registration_state.RegistrationStateMachine.transition()` 把
`RegistrationState.COMPLETED` 直接翻成 `status="success"` 交给 stage 回调。
但 `completed` 只是流水线**最后一个阶段名**，账号是否真的注册成功要等末阶段
返回后由调用方判定。

后果：同一个 attempt 在同一秒内先打
`阶段 · 完成 (completed) — 成功`，再打 `阶段 · 失败 (failed) — 失败`。
09-08~09-13 共 **27 / 179** 次失败命中（09-12 单日 14/26——正是「已注册邮箱
回登」成为主失败模式、且开始在 `finalize` **之后**失败的时段）。桌面端批次计数器
还会把这些 attempt 记成完成，因为 WPF 信封把 `status=success` 当终态。

修法：`transition(COMPLETED)` **不再发 stage 事件**；整轮终态由唯一知道结果的
`registration_progress.persist()` 独家发出。`machine.snapshot()` 仍记录该迁移。

### L2 机器通道静默停写

`RotatingFileHandler.emit` 把轮转异常交给 `logging.Handler.handleError`
（默认只写 stderr）**并丢弃该条记录**。Windows 上日志文件被其它进程持有 ⇒
rename 被拒 ⇒ 文件始终高于 `maxBytes` ⇒ 之后每条都失败 ⇒ **通道永久静默停写**，
而另一条通道照写不误（`sms_tool.jsonl` 停在 10:50 就是这么来的）。

修法：新增 `ResilientRotatingFileHandler`——`doRollover` 的 `OSError` 吞掉并降级
为 append，**每段连续失败只告警一次**（走 logging，不走 stderr：桌面宿主不捕获
stderr），每 200 条重试一次轮转。`configure_logging` 改为**两条通道各自独立
`try`**，一个文件打不开不会让两条都哑。

顺带删除一处死代码：`doRollover` 里显式重开 `self.stream` 是无用的——
`FileHandler.emit` 自己会重开 `None` 流。给死代码补测试不如删掉它。

### L3 失败分类失真

两处修正：

- `failure_registry` 补 4 个标记进 `auth_state`：
  `missing_auth_session_access_token`、`browser_passwordless_otp_state_unknown`、
  `browser_email_value_mismatch`、`browser_profile_submit_timeout`。
- 新增**派生**常量 `GENERIC_TRANSPORT_MARKERS`，把**裸传输词降为最后手段**。
  `NETWORK` 在注册表里排在 `AUTH_STATE` 之前，而 `timeout` 这类裸词是
  `browser_profile_submit_timeout` 的子串，会把 stage 级失败抢成 `network`。
  复合 token（`curl: (35)`、`session_circuit_open`、`connection reset`）仍单独
  决定性，不受影响。

同一份真实语料复测：`unknown 25 → 0`，`auth_state → network` 误判 `30 → 0`。

> ⚠️ **行为变更**：`missing_auth_session_access_token` 由 `unknown`（不可重试）
> 改为 `auth_state`（可重试），现在会累计重试守卫冷却（同一邮箱连续 2 次后进入
> 30 分钟冷却）。依据是 18 条真实数据（协议跑到 `finalize` 却拿不到 access
> token，占失败约 10%，且不是代码缺陷）。**回退点只有 `failure_registry.py`
> 里那一行标记。**

### L4 人读通道无法归因

`HumanLogFormatter` 追加 ` · account_ref=<sha256 前 16 位>`；`.log` / `.jsonl` /
WPF `[后端进度]` 三处用**同一个哈希**，一条账号可跨三个通道 grep。

此前 `sms_tool.log` 里 `account_ref` 出现次数是 **0**——并发多账号时只能靠行邻接
归因，而本仓自己的 playbook 明确禁止这种做法。`account_ref` 不在脱敏策略的
敏感键/片段里，脱敏层不会吞它。

## HTTP 重试覆盖（09-12 扫描 P2）

`request_with_retry` 补齐到最后三处直连调用点：`accounts/account_2fa.py`（7 处）、
`sentinel/client.py`（challenge）、`registration_preflight.py`（scheme 探针与边界预检）。

关键动机在 scheme 探针：一次瞬时 reset 会被判成「该 scheme 不可达」，进而把**整轮**
代理协议降级为 `http://`。先重试传输层，再下结论。

配套新增 AST 门禁 `tests/test_registration_http_retry_coverage.py`：逐点改调用 +
扫描兜底，防止新代码绕过。

## 代理池：HTTP 上游（09-12 扫描 P3）

`UpstreamProxy` 新增 `scheme` 字段，上游按**自身 scheme** 分派：

| 上游 scheme | 拨号方式 |
| --- | --- |
| `socks5` / `socks5h` | SOCKS5 握手（RFC 1928 + RFC 1929 鉴权）；`socks5h` 主机名不解析，交给上游做远程 DNS |
| `http` / `https` | HTTP `CONNECT` 隧道（`https` 给隧道套 TLS） |

**客户端始终对监听口说 SOCKS5**，不受上游 scheme 影响。

动机：**供应商给的全是 `http://`** —— live `proxy.pool` 是 30/30 `http`。只认
SOCKS5 的过滤器会把一个完全可用的池子筛成 0 条并以
`no upstreams configured` 退出。无 scheme 的条目默认 `socks5`（向后兼容）；
不认识的 scheme 跳过并告警。

## 文档指针漂移（检测 + 修复两半）

`docs_consistency_scan` 的 **strong tier（用 ast 校验符号真在那一行）只查表格行**；
散文指针只受 weak tier 管，**行号在范围内就算过**。实测后果：
`registration-and-proxy-architecture.md` 的散文指针 10 处全是旧值、与同一文档的
表格值互相矛盾，CI 仍然全绿。

新增修复半 `scripts/refresh_doc_symbol_lines.py`（按符号名反查行号回写）；
扫描失败时打印修复提示；CI 由 `test_live_docs_have_no_drift_left` 兜底。
护栏：符号必须是**该文件**的模块级定义 / 散文取**最近的前一个**反引号名 /
只改 `.py`。变异 7/7 KILLED。

## 配置文档：三条出口线相互独立

`docs/current/configuration.md` 新增决策段：`proxy.registration`、
`phone_reuse.proxy`、`paypal_browser` 各带自己的地区，**互不派生**——注册出口迁移
不是全系统出口迁移，混地区是预期而非 bug。同时明确：`config.json` 在任一分片存在
时即为死文件，读当前出口只能读 `proxy.json`；`paypal_browser.country` 当前**没有
Python 消费方**，改它是 no-op。

并记录手机线一处**已接受**的自相矛盾（智利号池 + 美国出口、
`proxy_match_phone_country=false`），说明为什么不要在不测量的前提下打开该开关。

## 验证

- Python：`pytest tests/` — **3547 passed / 6 skipped / 616 subtests**
  （158.51s，0 failed）。新增：L1–L4 行为用例 15 条、failure_registry 分类优先序
  5 条、代理池 HTTP 上游与 `proxy_entry` scheme 往返、文档指针刷新与漂移守卫、
  HTTP 重试覆盖 AST 门禁。
- .NET：`dotnet test GPTRegisterTool.slnx -c Release` — 386 passed / 0 failed
  （本版未改 C#，沿用 v2026.09.13 基线）。
- 守卫：architecture_scan / docs_consistency_scan / config_schema_check /
  ipc_schema_check / ruff / 秘密扫描全部通过。
- 变异：L1–L4 变异器 **9/9 KILLED**（每个变异体杀死的测试集都能用设计理由逐条
  解释），判定探针双向通过。

# 故障排查

每条故障给出**按顺序执行**的编号清单 —— 照着做即可，不需要先读完叙述。

本文件与 `docs/current/` 下的契约文档同级：里面的 `path.py:NNN` 指针受
`scripts/docs_consistency_scan.py` 门禁校验，改名/拆分会立刻让 CI 变红。

---

## 0. 所有故障的第一步：先跑自检

| 命令 | 用途 |
| --- | --- |
| `python -m sms_tool --doctor` | 离线自检：Python / Node / Playwright / curl_cffi / 配置完整性。**退出码 = 失败项数** |
| `python -m sms_tool --doctor --json` | 同上，机器可读（WPF 首启探测与安装器复用同一份报告） |
| `python scripts/preflight_env.py` | 安装前的环境预检 |

`fail` 必须先解决，`warn` 是可选依赖或非阻塞配置缺口，记下再继续。

---

## 1. 收不到邮箱验证码（OTP 一直轮询不到）

按顺序检查：

1. `python -m sms_tool --doctor` 是否全绿。
2. 该邮箱**行的形态**是否被解析器接受 —— 池文件支持 4 段（`email----url----client_id----rt`）、
   3 段与 `---` 分隔等不同分支，分隔符数量决定走哪条解析路径（`sms_tool/mailbox_parsers.py`）。
3. 日志里有没有 `[!] Skip ... refresh_token ...` 行。占位符或模板形态（`<...>`、`your_token`、
   重复字符）在导入时就被拒绝，**根本不会进池**（`sms_tool/mailbox_parsers.py:77`）。
4. 邮箱代理是否可达。`email_registration.mailbox_proxy_fallback_to_operation_proxy`
   （默认开启）会把 operation proxy 追加为兜底候选，但它**不是**「邮箱与注册一定同出口」的承诺
   （`docs/current/mailbox.md`）。
5. 看轮询失败的**处置类别**。`sms_tool/mailbox_errors.py:25` 是唯一 owner：
   - 凭据失效 / token 过期 → 立即终止轮询并**隔离**该邮箱；
   - endpoint 不可达 → 终止轮询并打开 endpoint 冷却；
   - transport 抖动 / 收件箱为空 → **可重试**，直到轮询截止。
6. 是否被隔离区挡掉了。隔离是 token-free 的；per-account 模式下按凭据过滤，
   只有**隔离凭据数达到阈值**才允许熔断整批（`sms_tool/mailbox_quarantine.py`）。
7. `MAILBOX_RT_WRITEBACK` 是否被设成了 `0`。设成 `0` 会关掉 RT 回写，见下一节。

---

## 2. 邮箱被判定为「已失效」，但邮箱本身没问题

症状通常是账本里只有「取件失败」，看不出原因。按顺序检查：

1. 日志里搜 `mailbox_refresh_token_writeback_missed`（`sms_tool/mailbox_pool_writer.py:209`）。
2. 该事件大量出现 ⇒ **池里的 refresh token 是旧的**。Microsoft / Google 在刷新时会轮换 RT，
   池文件若没跟着回写，**下一次**刷新就是 `invalid_grant`，而邮箱是完全健康的。
   成功回写会记 `mailbox_refresh_token_writeback`（`sms_tool/mailbox_pool_writer.py:200`）。
3. 确认 `MAILBOX_RT_WRITEBACK` 没被设成 `0`。
4. 确认池文件**可写**。回写走跨进程字节锁（`sms_tool/cross_process_gate.py`），
   拿不到锁会记 `_missed` 而不是抛异常 —— 锁超时看起来和「文件不可写」一样。
5. 回写只改**锚定的那一行**：锚点值在文件里必须**唯一出现**、且该行的属主邮箱必须匹配，
   否则拒绝写入（记 `_missed`）。所以「池里有重复的 RT」会让回写静默失效。
6. 🔴 若这批账号用的是 `app_password` 模式，**失效与 RT 轮换无关** —— 那是另一条失败源，
   不要按本节排查。

---

## 3. 邮箱池导入了，但一条都不生效

按顺序检查：

1. 源文件是几段？供应商 4 段与 3 段走不同解析分支，段数对不上会整行丢弃。
2. 分隔符是 `---` 还是 `----`？两者语义不同，不是同一个分隔符的不同写法。
3. 是否被占位符筛除。长度偏短的 RT 只会 **WARN 并保留**，形态明显是模板的才 **reject** ——
   所以「日志里有 WARN 但没有该邮箱」说明它在更早的分支就被丢了（`sms_tool/mailbox_parsers.py:46`）。
4. 别名池（如 `@icloud.com`）可能**已被别人注册过**，导入成功但注册阶段会失败。
5. 导入后按 `docs/current/mailbox.md` 的 machine-readable 契约核对，而不是只看导入日志的行数。

---

## 4. 改了配置不生效

按顺序检查：

1. **配置分片是权威真源**（`docs/current/configuration.md`：*shards remain authoritative*）。
   改错文件是最常见的原因。
2. 一旦存在分片，`config.json` 就是**死文件** —— 往里写什么都无效。
3. 键名是否在棘轮白名单里。新增键需要同时更新校验器与 `scripts/config_key_baseline.json`，
   否则 `tests/test_config_key_ratchet.py` 会红。
4. 环境变量覆盖优先于配置文件（`README.md` 的「应急环境变量覆盖」一节）。
5. 桌面端下拉框的**选项列表**与后端不是同一份数据 —— 见第 10 节。

---

## 5. 上游代理报错

按顺序检查：

1. **403 / 429 不重试**，改为在 session 上记一条 `blocked_until` 熔断（`sms_tool/http_client.py:140`）。
   所以「没有重试日志」是预期行为，不是重试被吃掉。
2. 冷却时长：403 默认 **900s**，429 默认 **300s**（取 `Retry-After` 优先）。
3. 429 是**出口 IP 级短窗口限流**，不是并发上限 —— 调低并发不会解决它。
4. 出口地区必须**「标签 + 主机子域」一起测**：只改地区标签而主机子域不变，可能整体回落到默认地区。
5. 代理池与服务端的配置边界见 `PROXY_GUIDE.md`。

---

## 6. 浏览器注册启动失败（profile 被占用）

按顺序检查：

1. 日志里搜 `browser_profile_contention`（`sms_tool/registration_drivers/browser_session.py:131`）——
   它会把**占用该 profile 的进程**列出来（PID + 映像名）。
2. 🔴 **不要用 `parent.lock` 判断**。Camoufox 每次**正常退出**也会留下它，
   「文件存在」不等于「有进程持有」。
3. `describe_contention`（`sms_tool/browser_profile_reclaim.py:204`）只把**精确引用**该 profile
   路径的进程算作占用者；命令行里只是**提到**这个路径的进程不算。
4. 需要清理时用 `reclaim_stale_profile(..., terminate=True)`，它会跳过非浏览器映像与自身进程。

---

## 7. 注册卡在 `email-verification` 路由

按顺序检查：

1. 该状态被单列为 `email_verification`，判定「卡死」需要走完**一次显式续进 + 一次 reload 重探**
   （`docs/adr/0007-email-verification-stuck.md`）—— 慢 SPA 不会被误判成死路由。
2. 二次仍卡死才抛 `browser_email_verification_stuck`，归类为 `auth_state`。
3. 该错误码是**批次事件的稳定性锚点**：改名必须同步 `sms_tool/error_classification.py` 与
   `docs/current/registration-recovery.md`。

---

## 8. 取消注册后没有停下来

按顺序检查：

1. 取消是**协作式**的，只在检查点生效（`sms_tool/registration_cancel.py:48`）。
   不落在检查点上的长调用会跑到返回为止。
2. 第一次 Ctrl+C（或 SIGBREAK）请求**优雅停止**，第二次才硬中断。
3. 检查点位置：批次账号之间、重试之间、注册阶段边界、OTP 轮询内、浏览器页面操作前。
4. 取消会清空标志并还原信号处理器，所以**取消不会泄漏到同进程的下一个批次** ——
   若观察到泄漏，那说明 `cancel_scope` 没被正确包住。

---

## 9. 注册成功但 2FA 没回填

按顺序检查：

1. 是否显式传了 `--no-2fa`（跳过注册成功后的 TOTP 2FA 登记）。
2. 结果里的 `already_enrolled` 是否为 `true` —— 表示该账号此前已登记，
   这不是失败（`sms_tool/accounts/account_2fa.py:225`）。
3. 回填是**多个持久化分支**共同完成的，不要只按其中一个分支的日志下结论。

---

## 10. 桌面端下拉框里选不到某个注册驱动

按顺序检查：

1. 驱动注册表是单一真源：`sms_tool/registration_drivers/base.py:53` 的 `DRIVERS`。
   CLI 的 `--registration-driver` choices 与配置校验都从它派生。
2. 会话工厂表必须与注册表一致 —— 不一致会在 **import 时**直接断言失败
   （`sms_tool/registration_drivers/external_sessions/__init__.py:22`）。
3. 🔴 **桌面端是手写的第二份**：`SmsWorkbench/SettingsCatalog.cs:56` 单独列了驱动枚举。
   Python 加了驱动而这里没加 ⇒ 下拉框里选不到；C# 留了已删的驱动 ⇒ 能选中但后端
   抛 `unsupported_registration_driver`。
4. 两侧一致性由 `tests/test_settings_catalog_driver_parity.py` 钉住。它变红就是上面第 3 条。

---

## 11. 日志里出现 Sentinel / Stripe 相关的 403 / 404

按顺序检查：

1. Sentinel SDK 下载返回 403/404 通常表示当前版本**已被轮换失效**。
   用 `OPENAI_SENTINEL_VERSION` 覆盖，或改配置的 `sentinel_version`（默认值内置于
   `sms_tool/sentinel/bundle.py`）。
2. `OPENAI_SENTINEL_DISABLE_QUICKJS` 设任意值会禁用真实 SDK 路径 ——
   **生产注册不要设**，纯 HTTP PoW 过不了深层校验。
3. OpenAI 轮换 Stripe publishable key 时用 `PP_STRIPE_PUBLISHABLE_KEY` 覆盖。
   该 key 通常由 checkout 响应自带，仅在响应缺失时用到回退值，**回退时会打 WARN**。
4. 以上都是**临时**手段；根因是上游轮换，确认后应更新默认值而不是长期依赖环境变量。

---

## 12. 接码相关：`no_number_source` / `phone_pool_unavailable`

2026-09-22 移除了**静态号池模式**（三条泳道全删，见
`docs/current/configuration.md`「PayPal checkout SMS has no number source」）。
号码现在只有一个来源：`phone_reuse.create_phone_pool` 建的租号池。

按顺序检查：

1. 看到 `phone_pool_unavailable`（注册链路）⇒ 池子没建起来。配置里要同时有
   `phone_reuse.source`（供应商 key，默认 `smsbower`）与**该供应商自己的** `api_key`，
   例如 `phone_reuse.smsbower.api_key`，或环境变量 `SMSBOWER_API_KEY`。
   供应商清单见 `sms_tool/sms_providers.py`（唯一真源）。
2. 看到 `no_number_source`（PayPal 结账链路）⇒ **不是配置错误，是这条泳道没有替代品**。
   静态号池是它唯一的号码来源，租号池没有接进来。要么按第 1 条接一个源并接到
   `sms_tool/paypal/orchestrator.py`，要么关掉 PayPal 结账短信（见第 4 条）。
3. 🔴 报错里出现 `phone_pool` / `static` / `legacy` / `sms_link` / `link` 被拒 ⇒
   这些是**已删除的 source 取值**，不是打错字。把 `phone_reuse.source` 改成供应商 key。
   `sms_tool/sms_providers.py` 的 `REMOVED_SOURCE_VALUES` 会给出逐值的替换建议。
4. 三种泳道的失败**故意不同**，别当成不一致：
   - `flow_steps` / `paypal_reverse` 会先看页面有没有验证码输入框，**有**才失败 ⇒ 直接抛错；
   - `nodriver_paypal` 是盲点「Send Code」，分不清「没有闸门」和「有闸门但接不了」，
     所以只打日志继续跑 —— 抛错会让**每一次**支付都失败，包括绝大多数根本不要短信的。
5. `no_number_source` **不再**等价于「等 120 秒然后 `sms_code_timeout`」。
   后者是在轮询一个空 URL，报的是错的原因。判据现在落在轮询**之前**
   （`sms_tool/sms_utils.py` 的 `NO_NUMBER_SOURCE_MESSAGE`）。
6. 老配置里残留的 `paypal_browser.phone_pool` / `paypal_nocard.phone_pool` /
   `phone_index_file` 都是**死键**，没有任何读取方。示例模板里的已删；
   本机未跟踪的分片（`config.json` / `payment.json` / `proxy.json`）里可能还在，
   删不删由操作者决定 —— 那几个块里挨着真实的接码中继密钥。

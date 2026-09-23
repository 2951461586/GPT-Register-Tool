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

   四家供应商的配置骨架已在 `proxy.json` 和 `config.example.json` 里给出，
   `api_key` 一律是**占位符**形式（默认走环境变量），把值填成自己的 key 即可：

   | 供应商 | 配置路径 | 环境变量 |
   |---|---|---|
   | `smsbower` | `phone_reuse.smsbower.api_key` | `SMSBOWER_API_KEY` |
   | `herosms` | `phone_reuse.herosms.api_key` | `HEROSMS_API_KEY` |
   | `grizzly` | `phone_reuse.grizzly.api_key` | `GRIZZLY_API_KEY` |
   | `nexsms` | `phone_reuse.nexsms.api_key` | `NEXSMS_API_KEY` |

   🔴 **占位符不等于「已配置」**：`$HEROSMS_API_KEY` 只是个指向，变量没设就是没密钥。
   想知道哪家真配上了，跑 `python -m sms_tool --doctor` 看 `phone_providers` 一行：

   ```
   [ OK ] phone_providers: selected=smsbower (origin=config, endpoint=https://smsbower.page/stubs/handler_api.php);
                           smsbower=config, herosms=missing, grizzly=missing, nexsms=missing
   ```

   `origin` 只有三种取值：`config`（配置里写了字面量）、`env`（从环境变量解析出来）、
   `missing`（没解析到）。只有 `source` 选中的那家没密钥才告警，其余三家未配置属正常。
   报告**从不打印密钥本身**，只报来源，所以可以直接贴进工单。
   `--doctor --json` 的 `phone_providers` 字段是同一份数据的结构化版本（额外带 `endpoint`）。
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
7. 🔴 **配置写对了、`--doctor` 也是绿的，但一个号都买不到** ⇒ 先查**价格窗口**。

   `phone_reuse` 会把 `max_price` **全局兜底成 `0.06`**（`phone_reuse.py:555` 的 `or "0.06"`），
   而 `_price_window_refusal()`（`sms_tool/nexsms.py:499`）会拒掉任何高于它的报价。
   **「空值不是边界」** —— 空 `min_price` 确实无下界，但 `max_price` 有默认上界。

   ⇒ **换一家供应商时必须重估价格窗口，不能沿用 0.06。** 各家的价差极大，实测 nexsms
   上 `dr` 的最低价（按国）：

   | 国家 | id | 最低价 |
   |---|---|---|
   | Indonesia | 6 | **0.1207** |
   | Kazakhstan | 2 | 0.216 |
   | Chile | 151 | 0.2586 |
   | Ghana（代码默认国） | 38 | 0.861 |
   | USA | 187 | 1.5551 |

   `0.06` 的上限在 nexsms 上**低于所有国家的最低价** ⇒ 结构性买不到，不是抖动。

   查价（零成本，只报价、不租号）：
   ```bash
   PYTHONPATH=. python -c "
   from sms_tool import phone_reuse as pr, sms_providers as sp
   from sms_tool.nexsms import NexSmsClient
   cfg = pr._phone_reuse_cfg()
   c = NexSmsClient(api_key=pr._provider_api_key(cfg,'nexsms'), endpoint=sp.default_endpoint('nexsms'))
   print(c.get_country_quote(service='dr', country='6'))"
   ```
   ⚠️ **`get_country_quote(service=...)` 不带国家参数时只返回第一条**（本项目里是加纳），
   别据此判断「只有这一个国家有号」—— 直接打 `/api/getCountryByService?serviceCode=dr`
   才拿到**全量国家数组**。

   修法：在该供应商 section 里**显式钉价格**（`min_price` / `max_price` / `target_price`
   全钉到你要的那一档，与 `smsbower` 的 `min=max=target=0.07` 同写法）。
   `country_name` **不用写**：数字国家 id 由 `phone_proxy.COUNTRY_ID_TO_ISO` 直接解析成 ISO
   （如 `"6" -> "ID"`），只有查不到 id 时才回退到 `country_name`。

8. 🔴🔴 **`min=max=target` 钉死单档的隐患：厂商的档位表会漂移，而失败是静默的。**

   上面第 7 条的修法（三价钉同一档）有一个副作用：**一旦该档位消失，配置就再也租不到号**，
   而症状与第 7 条一样 —— `--doctor` 全绿、余额充足、就是买不到。

   **已实测到的实例（2026-09-23，未改动，仅记录）**：`smsbower`（当前**活跃**供应商）
   配的是 `country=151`（智利）+ 三价钉 `0.07`，而 `0.07` 这一档**在 122 个有号国家里
   出现 0 次** —— 智利的档位是 `0.045 / 0.047 / 0.087 / 0.100 / …`。`get_number` 会把
   `minPrice=0.07&maxPrice=0.07` **逐字**发给 `getNumberV2`（`smsbower.py:112-115`），
   且 `_country_candidates("151")` 只有 `["151"]`（没有回退国）。

   ⚠️ **这里有一个尚未验证的前提**：厂商是否**严格按 `minPrice` 过滤**。
   - 若**按区间过滤**（`0.045 ≤ 0.07` 就能过）⇒ 当前配置是好的，`target_price` 只是用来选档；
   - 若**按精确档位过滤** ⇒ 当前配置结构性买不到号。
   仓库里**没有任何关于这一点的既有结论**，两种都说得通。判定方法（零成本，未执行）：
   用 `maxPrice=0.0001` 与 `minPrice=99` 两个不可能成交的请求测厂商是否真的按价格过滤 ——
   预期回 `NO_NUMBERS`；若厂商忽略该过滤会真的发号（约 $0.045，可用 `cancel` 退）。

   更稳的写法是**给窗口留区间**（如 `min=0.045 max=0.108 target=0.045`）：厂商仍按 `target`
   钉到最便宜那一档，但档位表变动时不会全盘失效。改不改由操作者定 —— 本机**没动**。

   三家的实测价（2026-09-23，零成本报价接口；`dr` / OpenAI）：

   | 供应商 | 余额 | 协议 | 报价接口 | 现配国家 | 现配价 |
   |---|---|---|---|---|---|
   | `smsbower` | 1.109 | `sms_activate_handler` | `getPricesV3` ✅ | 151 智利 | 0.07（⚠️ 见上） |
   | `herosms` | 7.6232 | `sms_activate_handler` | **`getPricesV3` 404**，只有 `getPrices` | 4 越南 | 0.03 |
   | `grizzly` | 8.8000 | `sms_activate_handler` | `getPricesV3` ✅ | 3 斐济 | 0.013 |
   | `nexsms` | 0.2000 | `nexsms_json`（REST） | `/api/getCountryByService` | 6 印尼 | 0.1207 |

## 13. 桌面端点过「开始接码」之后，本地 `test_config_usage` 变红

**症状**：`tests/test_config_usage.py::test_detector_produces_the_pinned_set` 报
`phone_reuse.<供应商>.service_name` 或 `phone_reuse.<供应商>.country_name_zh`
出现在 unread 集合里，但不在 `EXPECTED_UNREAD` 中（`<供应商>` 通常不是 `smsbower`）。
CI 上**看不见**这个问题。

**触发条件**：在桌面端选一个**非 `smsbower`** 的供应商，走一次「开始接码」弹窗。

**根因**（三个事实叠在一起）：

1. `SmsWorkbench/MainWindow.SmsProvider.cs:235/238` 会往**当前供应商**的 section 里写
   `service_name` 与 `country_name_zh`（纯展示用，两语言下都没有读取方）。
2. `sms_tool/config_usage.py` 的判定是「**leaf 名是否作为独立字符串字面量**出现在
   `sms_tool/` + `services/`」——`service_name` / `country_name_zh` 两个 leaf 都不出现，
   所以被判为死键；而 `country_name` / `service` / `endpoint` 是活叶子，可以随便写。
3. `config_usage.SHARD_NAMES` **包含 `proxy.json`**，而 `phone_reuse.*` 正是落在
   `proxy.json` 里 ⇒ 桌面端写进去的键会被扫描器看到。

`smsbower` 之所以不炸，只是因为 `EXPECTED_UNREAD` 里**恰好只钉了 `phone_reuse.smsbower.*`
这两条路径**。

**为什么不能顺手修**：`tests/test_config_usage.py::test_write_only_keys_are_still_reported`
里有一行

```python
self.assertIn(key.rsplit(".", 1)[-1], writes, "counter-example moved")
```

它把「**C# 写这两个键**」钉成了「扫描范围只看 Python、不看 C#」这个决策的**前提**
（`config_usage.py:29-49` 的 docstring 整段论证都建在这个反例上）。删掉 C# 那两行写入，
这条守卫会以 `counter-example moved` 失败，得连带重写模块 docstring 与测试 ——
**属设计变更，不是修 typo。**

**临时处置**（只想恢复绿）：把这两个键从**出问题的那个非 `smsbower` section** 里删掉即可
（它们是死键，删了没有任何行为变化）。
⚠️ 别去删 `phone_reuse.smsbower.*` 下的同名键 —— 那两条**被 `EXPECTED_UNREAD` 钉着**，
删了会让同一个测试往**反方向**红。

## 14. 切了接码供应商之后，桌面端「一键接码」报「加载失败 … 403 (Forbidden)」

**症状**：设置里把「接码供应商」换成 `nexsms` 之后，点「一键接码」弹出

> NexSMS 加载失败 / 无法读取 OpenAI 号码地区和价格档位：Response status code does not indicate success: 403 (Forbidden).

命令行跑批**不受影响**，只有桌面端这个弹窗走不下去。

**触发条件**：`phone_reuse.source` 指向一个**协议族不是 `sms_activate_handler`** 的供应商。

**根因**（四个事实叠在一起）：

1. 弹窗要先用**在线目录**（国家 + 价格档位）让操作员选，再把选择回写进配置；
2. 它读目录用的是 sms-activate handler 协议 —— `{endpoint}?api_key=…&action=getCountries`、
   `…&action=getPricesV3&service=dr`，余额还要求响应以 `ACCESS_BALANCE:` 开头
   （`SmsWorkbench/SmsProviderCatalogClient.cs`）；
3. `nexsms` 是 REST（`/api/` 路径 + `{code,message,data}` 信封），**上面三个 URL 全部 403**。
   实测：`openresty` 的 403 页；同机打它的合法路由 `/api/getCountryByService` 正常返回
   ⇒ 这是**路由级**拒绝，不是网络 / 主机 / 凭据问题，换代理也没用；
4. `GetTextAsync` 里的 `response.EnsureSuccessStatusCode()` 于是抛 `HttpRequestException`，
   弹窗把它显示成「加载失败」并 `return false`，调用方 `OneClickSmsAsync` 早退
   ⇒ 整个「一键接码」不可用。

**为什么只有桌面端中招**：`CreateOneClickSms` 那条路只把供应商 key 当**命令行参数**传给
Python 后端，协议细节全在 `sms_tool/`（`nexsms.py`）里 ⇒ 命令行与批量跑批一直是好的。

**现在的行为**（2026-09-23 第二次修订）：弹窗按 `SmsProviderCatalog.SmsProvider.Protocol`
分派到**两个在线读取器**，两者都在 `SmsWorkbench/SmsProviderCatalogClient.cs` 里：

| 协议族 | 目录 | 余额 | 供应商 |
| --- | --- | --- | --- |
| `sms_activate_handler` | `?action=getCountries` + `getPricesV3`（404 时退 `getPrices`） | `ACCESS_BALANCE:` | smsbower / herosms / grizzly |
| `nexsms_json` | `/api/countries` + `/api/getCountryByService?serviceCode=dr` | `/api/balance` | nexsms |

🔴 **neXSMS 现在也读在线目录**。本文档此前那句「非 sms-activate 的供应商不读在线目录，
直接取配置里的 `country` / `target_price`」已经作废 —— 它是上一版（只读配置）的行为。
当时不做 C# 侧实现的理由是「同一份协议的第二份实现会漂移」，那个判断对**协议**成立，
但对**规模**不成立：读它只需要一次 GET 加一次 `priceMap` 遍历，而配置路径的代价是让操作员
失去这个弹窗的全部意义 —— 真实余额、供应商实际服务的每个国家、每个档位的真实库存，
全都退化成配置里那一对硬编码值。实测（2026-09-23，同一把 key）：

| 指标 | 只读配置（旧） | 在线读取（新） |
| --- | --- | --- |
| 余额 | `--` | `0.2000` |
| 国家数 | 1 | **183** |
| 价格档位数 | 1 | **923** |
| 库存合计 | 未查询 | **125,257,831** |
| 中文名覆盖 | 无 | 183/183 |

**两个端点都必须读**：`/api/countries` 带中文名但**完全没有英文名字段**（按 id 键控），
`/api/getCountryByService` 带 `countryName`（英文）与整个 `priceMap`。实测两者各有 195 与 183 行，
**交集**才是真正有货的国家；有报价但没中文名的国家**保留**（可租），有名字但 `priceMap`
为空的**丢弃**（做成下拉项也只是个租不到的空条目）。

**配置回退仍然保留，且仍不是协议专属**：任何「在线目录读不到」都走到那里，不只是协议不匹配
（见 §15）。回退分支**不回写** —— 两个选择项本来就是从配置读出来的，回写只会把它们重新格式化，
顺便把 `service_name` / `country_name_zh` 两个只写不读的叶子塞进该 section，反而会踩 §13。

**回归守卫**（`tests/test_settings_catalog_provider_parity.py`）：

- `test_the_protocols_match_python` —— 每行的协议族与 `sms_providers.PROVIDERS[k].protocol` 逐项相等；
- `test_the_protocol_constants_match_python` —— 常量值也钉住（表里用标识符、比较逻辑用常量，两处都要钉）；
- `test_the_dialog_gates_the_online_catalog_on_the_protocol` —— 钉**三元分派**的形状：
  `provider.CatalogIsSmsActivate ? 读取器A : 读取器B`，且两个读取器**各自只被调用一次**。
  只断言「文件里出现过 `CatalogIsSmsActivate`」是没用的：判断可以写在那里却**包不住**那次调用，
  或者两个协议族**对调**（把 nexsms 交给 sms-activate 读取器）—— 症状与本次原始缺陷一模一样（一次 403）；
- `test_the_config_derived_path_does_not_write_the_dead_leaves` —— 配置派生分支必须在回写之前 return。
  闸门是**来源**（`fromCatalog`）不是协议，因为在线目录**读失败**的供应商也走配置回退（见 §15）；
- `test_only_a_successful_online_lookup_marks_the_choice_as_catalog_sourced` —— 上面那个闸门自己也要钉：
  `fromCatalog` 必须由在线结果**直接派生**（`= online is not null`），且之后不得再被赋值。
  否则「忘了在成功分支里赋值」和「无条件置真」两种失效都会让闸门失效而它自己仍然绿；
- `test_a_failed_online_lookup_reports_why_before_falling_back` / `test_a_failed_online_lookup_does_not_end_the_flow`
  —— 见 §15。前者现在钉**两处**：无路可退时原因插值进弹窗、有路可退时原因进日志。

**NeXSMS 解析器的形状测试**：`tests/SmsWorkbench.Tests/SmsProviderNexsmsCatalogTests.cs`
（12 条），覆盖 `{code,message,data}` 信封、`code` 为字符串 `"0"` 仍算成功、`priceMap` 的价格
**字符串原样保留**（`0.12070` 不能变成 `0.1207` 之外的东西 —— 回写进配置后后端要拿它比价）、
档位升序由**本地排序**保证而不是相信载荷顺序、`/api/countries` 与 `/api/getCountryByService`
两个载荷的合并与取舍规则、以及余额缺字段时**报错而不是显示成 `$0.00`**。

**变异验证**（守卫必须实测会红，否则是同义反复）：`runtime/tmp/mutate_protocol_guards.py`
破坏 12 处行为（表里换协议标识符 / 常量值写错 / **两个协议族对调** / **目录调用脱离协议判断** /
回退不读配置 / 闸门条件改成 `if (false)` / 把提前返回注释掉 / 来源开关改成恒真 /
来源开关在派生之后被重新赋值 / **回退分支条件改成 `else if (false)`** / 失败原因不进提示 /
失败原因不从异常捕获），要求对应测试**全部变红**，并核对还原后字节一致。实测 12/12 全部捕获。

⚠️ 其中「把提前返回注释掉」这一处**第一次漏过了**：`assertIn("return true;", block)`
会被**注释里**的同名子串满足。所以断言前必须用 `strip_csharp_comments()` 剥注释 ——
这与 `test_config_usage` 的 `test_the_extractor_ignores_commented_out_literals` 是同一类缺陷。

⚠️ **产物验证只认字符串字面量，不认方法名**。`SmsWorkbench.dll` 在 Release 下
**`internal` 成员名一律不进元数据**（实测连改动前就有的 `LoadOpenAiCatalogAsync` /
`ParseCatalog` / `SmsProviderCountryChoice` 都搜不到，只有 WPF 绑定用到的属性名如
`DisplayName` 在）。所以「用方法名扫产物」会得到假阴性；且 `$"价格 ${tier.Price} / 个"`
这种插值字符串编译后**字面量被拆开**，也要按片段扫。判据仍是**成组**：本次删的（须 absent）
+ 本次加的（须 present）+ 没动过的对照（须 present，否则是扫描器坏了）。

## 15. 换了接码供应商之后，桌面端「一键接码」报「暂无号码」或 `getPricesV3` 404

**症状**（两种，都不抛异常到操作员面前，所以特别容易误判）：

- `grizzly`：弹窗说「Grizzly SMS 当前没有可用的 OpenAI 号码」—— 而它实际有 9310 个；
- `herosms`：弹窗说「HeroSMS 加载失败 … 404 (Not Found)」—— 而它只是不认 `getPricesV3`。

**根因**（两个独立缺陷，都在 `SmsProviderCatalogClient`）：

1. **价格解析只认一种嵌套形态。** 旧代码把 `country → service` 的**子节点**当成 offer，于是：

   | 供应商 | `getPricesV3` | 层级 | 价格字段 |
   |---|---|---|---|
   | `smsbower` | ✅ | `country → service → provider_id → {…}` | `price` |
   | `herosms` | **404**（只有 `getPrices`） | `country → service → {…}` | **`cost`** |
   | `grizzly` | ✅ | `country → service → {…}` | `price` |

   后两家的 `service` 节点**自己就带价格**，被读成「子节点里没有价格」⇒ 国家全部被丢弃
   ⇒ `countries.Count == 0` ⇒ 「暂无号码」。**没有任何异常**，所以只看日志是查不出来的。

2. **`getPricesV3` 在 sms-activate 家族内部也不通用** —— `herosms` 对它回 404，
   只认旧版 `getPrices`。旧代码只打 V3，于是 404 被 `EnsureSuccessStatusCode()` 抛成
   `HttpRequestException`。

**现在的行为**（2026-09-23 起）：

- `LoadPricesAsync` 先打 `getPricesV3`，**只有**在它 404 / 响应体不是 JSON 时才回退到 `getPrices`
  （`TheLegacyActionIsNotRequestedWhenV3Answers` 钉住「V3 成功时不多打一次」）；两个都失败时
  异常消息**同时带上两次尝试的原因**，好让操作者能区分「厂商挂了」和「我们问错了接口」。
- `ParseOffers` 从**载荷**判形态（先看 `service` 节点自己有没有价格，再看子节点），
  `TryReadPrice` 依次试 `price` / `cost`。**不按供应商分表**：表会静默过期，而它过期时的
  表现恰恰就是「看着正常、报告没有号码」。
- 🔴 **在线目录降级为「增强项」，不再是前置条件。** 任何读取失败（403 / 404 / 形态不符 /
  返回零个国家）都回退到配置里已存的 `<section>.country` 与 `.target_price`。理由：目录读不到
  说明不了这家厂商能不能租，而 Python 后端读的**就是这两个配置键**，所以配置值正是真正会被
  用来下单的值。
  **失败原因必须说出来** —— 静默回退会让一份陈旧配置看起来像刚刚核验过。2026-09-23 起分成两处，
  各自面向正确的读者：**配置里也没有国家/档位**（无路可退）⇒ 弹窗正文，原因**插值进去**；
  **有配置可回退**（正常情形）⇒ 日志（`logger?.Warning("... catalog unavailable ({Error}) ...")`），
  因为这不是需要操作者处理的错误，不该拦着弹窗让人先点掉一个提示。

**为什么在线目录失败不该致命**：它失败的原因与「能否租号」无关 —— `herosms` 是 404、
厂商可能短暂宕机、代理可能在做过滤。把它当前置条件，等于让任何一个偶发故障都升级成
「这家供应商完全不可用」。

**回归守卫**（`tests/SmsWorkbench.Tests/SmsProviderCatalogClientTests.cs`，用假 `HttpMessageHandler`
做**真行为**测试而不是源码守卫）：

- 三种形态各一条（`NestedShapeKeepsEveryProviderAsItsOwnOffer` / `FlatShapeWithPriceFieldIsRead` /
  `FlatShapeWithCostFieldIsRead`），载荷是**真实响应的逐字前缀**（数字裁剪过，形态与字段名未动）；
- `ThePriceActionFallsBackToTheLegacyOneWhenV3IsMissing` / `TheLegacyActionIsNotRequestedWhenV3Answers` /
  `FailingBothActionsNamesBothInTheError`；
- ⚠️ 假 handler **按 `action=` 解析**而不是 `Contains`：`action=getPrices` 是
  `action=getPricesV3` 的**前缀**，用子串匹配的话无论回退有没有发生都会通过。


## 16. 切过接码供应商之后，另外几家报 `401 (Unauthorized)` / `NOKEY`

**症状**（一次会话里出现其中几条，`smsbower` 却是好的）：

| 供应商 | 桌面端弹窗原文 |
|---|---|
| `herosms` | `无法读取 OpenAI 号码地区和价格档位：Responsestatus code does not indicate success: 401 (Unauthorized).` |
| `grizzly` | `无法读取 OpenAI 号码地区和价格档位：NOKEY` |
| `nexsms` | `… 403 (Forbidden).`（这是另一回事，见 §14） |

三种消息都**没有指出是哪一家、哪个配置键**，所以看起来像「这三家厂商都坏了」，
实际是**同一份密钥被写进了四个 section**。

**根因**：桌面端「设置 → 接码供应商」的**下拉框与输入框是两个独立控件**。
`SettingsService.Load()` 只在打开弹窗时解析一次 provider 作用域路径
（`phone_reuse.{provider}.api_key` 等），而下拉框改选之后**没有任何东西去重新解析**。
于是：输入框里仍是**打开时那家**（`source` 指向的那家）的 key，操作者选另一家后保存，
`Save()` 就把它写进了**新选中的** section。

> 🔴 注意 `SettingsService.Save()` 里的 `ResolveProviderPath` 修的是**写入目标**，
> 不是**显示值**。只修前者反而更糟：密钥从「写到自己家（无害）」变成
> 「写到别人家、且顶着别人家的名字（静默错配）」。

**取证**（2026-09-23，`proxy.json` 3949 B 的那一份）：把「最后一次已知良好」的备份
（`runtime/proxy-backups/proxy.json.before-herosms-grizzly-geo-20260923-150155`）
按 geo 脚本改写后与当前文件逐叶子比对，**差异 100% 落在设置界面拥有的字段上**，
且每个值都等于 `smsbower` 的值：

| 叶子 | 良好状态 | 被覆盖后 | 来源 |
|---|---|---|---|
| `herosms` / `grizzly` / `nexsms`.api_key | 各自的 32/32/16 位 | 全部 = smsbower 的 32 位 | 输入框没刷新 |
| 同三家 `.endpoint` | 各自的完整 URL | `""`（smsbower 没配 endpoint） | 同上 |
| 同三家 `.sms_timeout` | `120` | `60`（smsbower 的值） | 同上 |
| `phone_reuse.source` | `smsbower` | `nexsms`（最后一次下拉选择） | 同上 |

**零成本确认**（不租号、只查余额）：

```
.venv/Scripts/python.exe runtime/tmp/probe_sms_keys.py
```

修复前 `herosms` / `grizzly` / `nexsms` 分别回 `401 BAD_KEY` / `NO_KEY` / `401 API密钥无效`；
四家的 key 换成各自的真值后全部 `HTTP 200 ACCESS_BALANCE:…`。

**现在的行为**（2026-09-23 起）：

- `SettingsViewModel` 订阅了 `phone_provider` 字段的 `PropertyChanged`，**下拉框一变就重新
  解析**所有带 `{provider}` 的字段（`SettingsService.ReloadProviderScopedFields`），
  于是输入框里显示的**永远是这次保存真正会写进去的值**。
- `--doctor` 的 `phone_providers` 增加一条**离线**判据：**两家以上解析后的 api_key 逐字相同**
  就告警，并点名要改哪些 section（`phone_reuse.provider_key_collisions`）。
  不打印密钥、不发请求，所以没网的机器也能跑。四家是四家独立生意，
  共用一个 key 永远不是合法配置。
  它同时仍会报告「选中那家没有 key」——**撞车分支不能把这条盖掉**，
  因为挡住跑批的是它，撞车只是更意外。

**回归守卫**：

- `tests/SmsWorkbench.Tests/SettingsServiceTests.cs`：
  `SwitchingProviderReloadsProviderScopedBoxesSoSaveCannotStampAnotherProvidersKey`
  —— 走**真的 `SettingsViewModel`**（直接调 `ReloadProviderScopedFields` 会对着未修的
  视图模型也通过，那样等于没测到缺失的那条订阅）。变异验证：注掉 `WatchProviderSelector()`
  后它以 `Expected: "hero-key" Actual: "bower-key"` 变红，正是事故本身的签名。
  同文件 `ProviderSelectorFieldIsAnOptionsFieldOnPhoneReuseSource` 钉住订阅所依赖的字段键
  与 `{provider}` 令牌，防止改名后订阅静默失效。
- `tests/test_doctor.py`：撞车告警、不撞车不告警、未配置不算撞车、撞车不掩盖缺 key。

## 17. 配置分片：三片 mtime 一起变 / 清空设置后旧值复活 / 中文被转义

**症状**（这几条是同一个子系统的不同面）：

| 症状 | 真实含义 |
|---|---|
| 保存一次设置，`proxy.json` / `runtime.json` / `payment.json` 的 mtime **同时**变化 | 写入器**无条件重写三片** |
| 清空全部设置后，之前删掉的旧配置**又回来了** | 空片被**删文件** ⇒ `AnyShardExists()` 为假 ⇒ 回落到 legacy 单文件分支 |
| 桌面端保存过的分片里中文是 `"\u667A\u5229"`、`+` 是 `"\u002B"` | C# 默认 `JavaScriptEncoder` 转义非 ASCII 与 `+`，Python 的 `ensure_ascii=False` 不转 |
| `--doctor` 报 `config_source: …\sms_tool` | 取的是 `default_config_path().parent`，根 `config.json` 归档后它回落到**包内**副本 |
| 分片旁边出现 `.bak` | 正常，是原子写留下的上一份内容 |

**根因**（四条彼此独立，2026-09-23 一并修）：

1. **写入粒度**：`ConfigStore.WriteShards` / `config._write_shards` 遍历三个 bucket
   无条件写。代价不只是 IO：分片 mtime 本来是「哪块配置被动过」的审计信号 ——
   §16 那次密钥写串就是靠它分工作流的。三片一起刷新之后，这个判据**直接失效**。
2. **空片语义两侧不一致**：Python 写 `{}`，C# **删文件**。两侧都用
   「至少存在一个分片文件」判断「是不是分片布局」，所以把三片删光会把应用**交回
   legacy 单文件分支** —— 那份没人维护的旧 `config.json` 于是被重新读进来，
   复活刚被删掉的键。空对象 merge 之后不产生任何键，所以写 `{}` **同样**能防止复活，
   却不会翻转布局判定。
3. **序列化不一致**：C# 默认编码器把 `+` 写成 `\u002B`、`智利` 写成 `\u667A\u5229`，
   Python 写原文。后果是两侧**互判对方写的文件为「已变更」**，内容裁剪永远命中不了，
   而且每次桌面端保存都重写整文件的非 ASCII 字节。取证：修复前 `proxy.json` 里
   逐字就是 `"country_name_zh": "\u667A\u5229"`。
4. **原子写不一致**：Python 有 `<name>.bak` + `fsync`，C# 两样都没有 ——
   「桌面端写、Python 备份」这条链**从未真正生效过**，而崩溃时被截断的那个文件
   **就是全部应用配置**。

**为什么删掉了根目录的 `config.json`**：

它是被 gitignore 的**死文件**（`.gitignore:1`，无 git 历史）。2026-09-23 实测：
386 个叶子、**独有叶子 0**（每个键都能在分片里找到）、88 个值不同且样本里分片一律是真值。
留着它只会让陈旧数字继续流通，并让 `default_config_path()` 优先返回它。
归档在 `runtime/config-backups/config.json.before-archive-20260923-195004`
（sha256 与原件逐字节核对通过之后才删）。回滚：把归档件拷回项目根即可。

> ⚠️ **不要**因此去删 legacy 迁移分支。`config.example.json` 是**单文件**模板，
> 首装与 CI 都靠它落地成分片 —— 分支是承重墙，删的只是那份数据文件。

**现在的行为**（2026-09-23 起）：

- 两侧都**只写内容真正变化的分片**，并返回「实际写了哪些文件」；
  未变的分片 mtime 不动、也不生成新 `.bak`（旧 `.bak` 因此不会被无意义保存冲掉）。
- 两侧空片都写 `{}`，**不再删文件**。
- C# 改用 `JavaScriptEncoder.UnsafeRelaxedJsonEscaping` 并补尾随 `\n`，与 Python 的
  `json.dumps(..., ensure_ascii=False) + "\n"` 逐字节对齐。
- C# 补上 `.bak`（`File.Copy`）与 fsync（`Flush(flushToDisk: true)`）。
- 新增 `default_config_dir()`，取代 `default_config_path().parent` 作为「配置目录」的答案。

**回归守卫**：

- `tests/test_config_sharded_atomic_write.py`：裁剪、返回值、`.bak` 归属、尾随换行、
  `default_config_dir()` 不被 legacy 回落带偏。
- `tests/SmsWorkbench.Tests/ConfigStoreTests.cs`：空片写 `{}`、
  `ClearingEverySettingDoesNotHandTheAppBackToTheLegacyFile`（清空设置 + 磁盘上放一份
  旧 `config.json`，断言它**不被读回来**）、裁剪不动未变分片、非 ASCII / `+` 不转义。
- **变异验证**（每处修复注掉都必须变红，否则测试是同义反复）：Python 裁剪 → 2 条红；
  `default_config_dir` 退回 legacy 语义 → 1 条红；C# 裁剪 → 1 条；C# 恢复删空片 → **3 条**；
  C# 去掉 `Encoder` → 1 条；C# 去掉尾随换行 → 1 条。
- 🔴 `test_write_shards_keeps_old_file_when_replace_fails` 原本**第二次写相同内容**，
  加上裁剪之后 `os.replace` 根本不会被调用 ⇒ 该测试会**永远通过**（同义反复）。
  已改成第二次写**不同**内容。任何「写两次」的测试在引入裁剪后都要重查这一点。

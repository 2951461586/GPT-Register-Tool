# scan-2026-09-16 — `api798.com` 的「0/33」是测量假象，33 个可用邮箱曾被误删

> **一句话结论**：`icloud2_probe_qixin.json`（2026-08-10）给出的 `api798.com 0/33` **不是渠道故障**，
> 而是**导入格式**造成的假阴性 —— 4 段格式的尾巴被拼进 `auth_code` 查询参数，服务端回
> `HTTP 403 错误：授权码无效`。2026-09-16 实测该渠道 **33/33 全部存活**。
> 据此执行的 `icloud2_remove403.py` 删掉了 33 个本来能用的邮箱。

本文件是一次取证记录，不改变任何当前架构契约。

---

## 一、触发点

导入 `icloud-2.txt`（50 个 `+oai02@icloud.com`）时做去重与前置核查，撞上两条互相矛盾的历史证据：

| 证据 | 内容 |
|---|---|
| `runtime/_retention/20260912-014722/mailbox_imports/icloud2_probe_qixin.json` | `probed:50 ok:17 fail:33`，`by_host: {'api798.com': {'ok':0,'fail':33}, 'icloud-api.top': {'ok':17,'fail':0}}`，失败明细统一为 `RuntimeError: iCloud OTP URL fetch failed: HTTP 403` |
| 今日文件（`icloud-2.txt`） | 50 行 = 33 行 `api798.com` + 17 行 `icloud-api.top`，与历史探测的 33/17 **按 base 邮箱一一对应** |

若直接采信历史结论，就会把 33 行当作死条目丢弃。

## 二、实测（2026-09-16，同一分钟、同一出口 `proxysg.rola.vip:2000 -country-vn`）

对 `api798.com` 做四路对照 —— 这是判定「渠道故障」还是「参数被污染」的关键：

| 请求 | 结果 |
|---|---|
| `?email=<base>&auth_code=SSS888` | **HTTP 200** `未找到匹配的邮件` |
| `?email=<base>&auth_code=SSS888----cyc08286688.----<TOKEN>`（即 4 段形态） | **HTTP 403** `错误：授权码无效` |
| `?email=<base>&auth_code=GARBAGE123`（对照） | HTTP 403 `错误：授权码无效` |
| `?email=<base>`（对照，无 auth_code） | HTTP 400 `错误：请提供邮箱地址和授权码` |

**第 2 行与第 3 行返回完全一致** ⇒ 带尾巴时服务端读到的是一个无效授权码。
`SSS888` 本身有效（第 1 行 200），渠道活着。

对 `icloud-api.top` 做同型对照（干净 vs 带尾巴）：两者都回 **HTTP 200**，且都是同一个邮箱页
（`最新邮件 0 封`），差别只在页面标题被尾巴污染（2585 B vs 2444 B）。
⇒ **尾巴在 `icloud-api.top` 上无害（落在 path），在 `api798.com` 上有害（落在 query 的 `auth_code` 值里）。**

## 三、因果链

1. `runtime/_retention/.../icloud2_import.py` 按供应商原始 **4 段**格式写入
   （`email----url----账号----2FA`，其 docstring 自称「password/2FA 字段仅供存档、取信逻辑不使用」）。
2. `sms_tool/providers/mailbox_icloud_url.py:split_icloud_url_line` 按**第一个** `----` 切分，
   尾部留在 `url` 里；`_valid_mailbox_url` 只校验 scheme + hostname ⇒ **尾部顺利通过校验**。
3. `_parse_icloud_url_line` 把整串存进 `MailboxAccount.token`；
   `_with_message_limit` 再 `parse_qsl`/`urlencode` 重排查询串 ⇒ 尾部被固化进 `auth_code`。
4. 探针脚本（同型见 `icloud3_probe.py:52`）从**池里** `_parse_mailbox_token_file` 取
   `MailboxAccount` ⇒ `mb.token` 天然带尾巴 ⇒ api798 全部 403，而 icloud-api.top 全部 ok。
5. `icloud2_remove403.py` 按 `probe["results"]` 的 `status == "fail"` 删行 ⇒ **33 个可用邮箱被删除**。

即：**一个「按第一个分隔符切分 + 只校验 scheme/hostname」的宽松解析器**，
叠加**一个把整串当 URL 用的调用方**，把「格式错误」伪装成了「渠道故障」。

## 四、处置（本次）

* `icloud-2.txt` 的 50 条按 **2 段形态**（`email----url`）导入 `mailbox_tokens.txt`；
  被丢弃的尾巴只有常量 `cyc08286688.` 与取信逻辑从不读取的 32 位参考串，
  原始 4 段行完整保存在导入报告的 `source_lines_verbatim`。
* 导入报告：`runtime/mailbox_imports/icloud2_import_20260916_234145.json`
* 实测探针报告：`runtime/tmp/icloud2_live_probe_20260916.json` —— **50/50 端点存活**
  （`icloud-api.top` 17 × ok，`api798.com` 33 × empty/HTTP 200）。
* 池规模：icloud_url 记录 **1013 → 1063**（+50），added 50 / removed 0，无新增带尾巴条目。

### 四之二、根因修复：把守卫前移到解析边界

只改数据不改解析器，等于每次导入都要靠人记得「别写 4 段」。所以直接修根：

**`sms_tool/providers/mailbox_icloud_url.py:split_icloud_url_line`**
—— 由「按第一个分隔符切分、保留其后全部」改为「切分后只取前两个字段」。
`parts[1]` 的边界用**同一个分隔符的下一次出现**确定（不是另写一套逻辑），
所以 `---` 分支与 `----` 分支语义一致，且与 C# 侧逐字对齐。

**为什么不是在 `_valid_mailbox_url` 加「含 `----` 即非法」的守卫**：那会让池里 223 条
**正在工作**的 `icloud-api.top` 条目**直接判非法**，从而掉出 `icloud_url` 分支、
被后续分支误解析 —— 修一个 bug 造一个更大的。截断则对两边都正确
（`icloud-api.top` 的干净 URL 已实测 200）。

**顺带修好的**：池里 223 条带尾巴条目的 `token` **全部变为干净 URL**，
而 `icloud_url` 记录数与渠道分布**零变化**（1063 / `icloud-api.top` 1012 + `ima3` 18 + `api798` 33）。

**C# 镜像（`SmsWorkbench.Contracts/MailboxCredentialLineParser.cs:TryParseICloudUrlLine`）同型缺陷一并修**。
它此前同样「取第一个分隔符之后的全部」，且 `Uri.TryCreate` 同样放行带尾巴的 URL。
当前调用面只用到布尔返回值，所以**生产影响为零**；但契约不对称会在下一次有人接线
`receiveUrl` 时继承同一个 bug（该文件已在 `audit-2026-09-02-round5-decoupling.md` 被标为
「C# 重实现子集，格式一改必漂」）。

**验证**：
- Python 契约 **9 例**（`ICloudUrlSplitContractTests`）—— 4 段 query 型 / 4 段 path 型 /
  5 段拼接行 / 2 段 / 3 横 2 段 / 非 http 第二段 / 非 icloud 域名 / 走加载器的端到端。
- 变异 **3/3 KILLED + 等效对照 MN SURVIVED**，源文件字节级还原（23690 B / 566 CRLF）：
  M1 退回旧行为 → 5 failed；M2 取**最后**两段 → 5 failed；M3 要求 3 段 → 4 failed。
- 全量 **4081 passed / 6 skipped / 656 subtests / 0 failed**（基线 4072 + 9 个新用例）。
- C# 测试 **+2 例**（`ICloudReceiveUrlDropsEverythingAfterTheUrlField` /
  `ICloudReceiveUrlKeepsTwoFieldLinesIntact`）。

### 四之三、第二个缺陷：api798 的正文藏在 JS 字符串里（已修，有活体复现）

修好格式之后，**`api798.com` 仍然读不到码** —— 而且这次是在**真实生产批次里当场复现**的：

| 时刻 | 证据 |
|---|---|
| 23:47:10 | 批次抽中 `jags-burly4k+oai02@icloud.com`（`account_ref=d87dcd49fdc9c7a5`，host `api798.com`） |
| 23:48:03 | `email_otp_send` → OpenAI 发码（日志 `issued_after=1789573546`） |
| 23:48:03 | api798 页面显示**同一秒**的接收时间 + 越南语主题 `Mã xác minh tạm thời của bạn cho ChatGPT` |
| 23:52:35 | `Email OTP poll end provider=icloud_url matched=False elapsed_ms=303610` |
| 23:52:35 | `stage=failed, failure_class=mailbox, detail=email_otp_poll_timeout:mailbox_side_no_code` |

**页面里明明有码**（`494652`），但我们读成 0 封。原因：

* 正文**不在 DOM 里** —— 它被塞进一段 JS 字符串（`var htmlContent = "…"`），
  再写进一个**没有 `src`** 的 `<iframe>`。页面级可见文本因此**只有主题、没有验证码**。
* `_parse_card_messages` 期待的是 `icloud-api.top` 的 `<div class="card">` 布局 ⇒ 0 封。
* 「0 封」与「邮箱里确实没邮件」在返回值上**完全一样** ⇒ 只能表现为轮询跑满超时。

**修法**：新增 `_latest_mail_js_message()`，识别「最新邮件信息 / 接收时间： / 邮件主题：」
三标记的页面，用 `json.loads` 把 JS 字符串**解回邮件 HTML**，再交给既有的验证码提取链路；
接收时间按页面标注的北京时间解析成 `+08:00`（这样 `issued_after_unix` 比较拿到真实时刻，
而不是落到「解析不出 ⇒ 不参与比较」那条宽容分支）。两条装配路径（`fetch` / `snapshot`）
都接线。**无邮件页（`未找到匹配的邮件`）与卡片页都不含这些标记 ⇒ 返回 `None`**，
「我们读不出」与「确实没邮件」仍然分得开。

**活体验证（同一封真实邮件，修复前后对照）**：

| | 修复前 | 修复后 |
|---|---|---|
| `fetch_icloud_url_messages` | **0 封** | **1 封** |
| `subject` | — | `Mã xác minh tạm thời của bạn cho ChatGPT login code` |
| `receivedDateTime` | — | `2026-09-16T23:48:03+08:00` |
| 下游 `_email_otp_candidate` | `None` | `{'otp': '494652', 'received_ts': 1789573683}` |

`recv_ts=1789573683` 与批次日志的 `issued_after=1789573546` 相差 137 s ⇒ 新鲜度检查通过；
主题被 `_normalize_otp_subject` 改写出 `login code` ⇒ 注册泳道关键词过滤也通过。

**验证**：契约 **12 例**（`LatestMailJsChannelTests`）· 变异 **5/5 KILLED + MN SURVIVED**
（源文件字节级还原 28405 B / 662 CRLF）· 全量 **4093 passed / 6 skipped / 656 subtests / 0 failed**。

## 五、仍未做 / 残留风险

1. **这个修复尚未在下一个批次里生效** —— 进程内的模块是启动时加载的，跑着的批次不受影响。
   要确认只能看**新起的**后端进程是否出现 `matched=True`。
2. **C# 的导入路径仍把原始行逐字写入池**（`MailboxPoolFileStore.ImportSupportedLines`
   的 `additions.Add(line)`）。解析器修好后 4 段行已无害，但 GUI 导入依旧会把尾巴带进池
   —— 这正是 223 条遗留条目的来源。是否让导入侧统一归一化成 2 段，待拍板。
3. **`api798.com` 已经在生产里被真正用到过**（本节的 23:47:10 批次就是），
   但它**不走**通用 HTML 卡片解析路径 —— `sms_tool/` 里没有任何 `api798` 专用分支，
   通用卡片解析器对它**恒回 0 封**（页面是 JS 版式，见 §四之三）。
   现在改由 `_latest_mail_js_message()` 这条**新分支**接管。
   ⚠️ 这意味着：**在下一个批次起来之前，api798 的 33 个邮箱仍然是"能发码、读不到"的状态。**
4. **`/latest` 页只暴露"最新一封"** ⇒ 新增分支只能读到**最新的那一封**。
   若在我们轮询期间有**更新的一封**（非验证码邮件）落到箱顶，就会读到错的那封
   —— 表现为 `_email_otp_candidate → None`，退化成本次故障之前的行为（不会更糟，但也不会更好）。
   当前无对策；如需对策要走"按主题/发件人筛最新验证码邮件"的路线，成本另计。
5. `icloud2_remove403.py` 那 33 个被删邮箱的**原始 4 段行**仍在
   `mailbox_tokens_before_remove403_20260810_184121.txt`（15559 B）中，**目前池中 0 条**，
   可回收 —— 回收方式即按本文的 2 段形态重写。
6. **多记录挤在一行的拼接行**（池内 1 条，5 字段）现在只解析出**第一条**记录，
   第二条静默丢弃。这是导入卫生问题，非解析器缺陷，未处理。

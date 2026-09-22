# 最新一轮协议注册：19/19「半注册」的链路断点定位 — 2026-09-16 22:4x

> **性质**：只读取证 + 方案建议。**本次未改任何生产代码。**
> **取证来源**：`runtime/registration_progress.jsonl`（批次 `099ccd8c2d604e6cbb164cf078426582`）、
> `runtime/logs/processes/32088/backend_stdout.jsonl`（PID 32088，22:07:27→22:36:50，已过滤 418 条 IPC 载荷）、
> `runtime/accounts.sqlite3`（只读）、`mailbox_tokens.txt` 与其 08:53 备份、
> `/f/tmp/_trash_20260903/reference-abai/platforms/chatgpt/protocol_register.py`（abai 参考实现）。

## 结论（TL;DR）

**链路确实有硬缺陷，但它不是这 19 个失败的根因；它的真正危害是「把失败原因报错了」。**

三句话：

1. **表面**：19/19 走的是同一条**确定性死路** —— 不是概率问题，是 0 例外。
   断点在 `auth_flow.py:1129` 跳过 `authorize/continue`（`continue_url` 留空）
   → `auth_flow.py:194-198` 探针**从不发请求**直接返回 `None`
   → `auth_flow.py:1244` `allow_passwordless=False` ⇒ 必然 `existing_login_password_step_unknown`。
2. **真实**：这 19 个地址在服务端**确实已被注册**（`user_already_exists`）。
   **即使把链路修好，它们也救不回来** —— 因为地址被占，且我们手里没有密码。
   成功的那 1 个是这批 20 个 `+oai02` 里**唯一没被占用**的。
3. **误导**：链路缺陷让「地址已被第三方占用」这个事实，被报成
   `existing_login_password_step_unknown`（听起来像我们的登录实现坏了）。
   **现在面板上那 19 行「半注册」不能读作「链路坏了」，只能读作「地址脏了」。**

**待拍板**：见 §五。选源（P0-1）与探针修复（P0-2）**是两件独立的事**，
不要用「修了链路就能救回这 19 个」当决策依据 —— 救不回来。

---

## 一、本轮实测数据

批次 `099ccd8c2d604e6cbb164cf078426582`，PID 32088，22:07:27 → 22:36:50。

| 口径 | 数值 |
|---|---|
| 任务总数 | 23 |
| `active`（成功） | **1** |
| `partial_registered`（半注册） | **19** |
| `failed` | 3（2 × `email_otp_send_stuck`，1 × curl(28) 超时） |
| 唯一邮箱 | 20 |
| 来源 | 全部 `icloud_url` / `selected_mailbox_20260916_220706.txt`（22:06 生成） |
| `registration_driver` | 全部 `protocol` |
| 失败串 | 19 × `existing_account_user_already_exists:continue_to_login` |

### 关键路径计数（`grep` 于 1239 条非 IPC 日志行）

```
  21  Redirect[login_or_signup]: 200 /email-verification     ← 全部落 email-verification
   0  Redirect[login_or_signup]: 200 /log-in/password        ← 本批没有这个落点
  19  Existing account continue: skipped (already at email-verification)
   0  Existing account continue: 200                        ← continue 一次都没发
  19  Existing account login method probe: password_step=None (no_transaction_state; source=none)
   0  password_step=True / False                            ← 探针从未拿到过答案
  19  Existing account login failed: existing_login_password_step_unknown
   0  existing_account_signup_routed_to_login               ← 09-16 17:3x 加的 P1 分支未触发
```

**21/21 的 `login_or_signup` 落点全是 `/email-verification`** ——
这与 19:1x 那批（落点 `/log-in/password`）**不同**，也与 P1 分支
`_is_existing_login_redirect` 的判据**不匹配**，所以 P1 那条路本轮一次都没走到。

---

## 二、链路断点（精确到行）

### 2.1 死锁的形成

```
login_or_signup        → 200 /email-verification          （21/21）
user/register          → 400 user_already_exists          （19 次）
  ↓ registration_handlers.py:1243  s.existing_account = True
  ↓ registration_handlers.py:1296  打印 "clearing stored password."  ← ⚠️ 见 §四
authorize (GET)        → 200 落在 /email-verification
  ↓ auth_flow.py:1129  _is_email_verification_step(current_url) == True
  ↓ auth_flow.py:1127-1128  continue_payload = {}   continue_next_url = ""   ← 状态没被填
  ↓ auth_flow.py:1133  跳过 POST authorize/continue
probe (auth_flow.py:1194)
  ↓ auth_flow.py:194-198  continue_url 空 and page_type 空 ⇒ 直接 return
  ↓                       result["signal"]="no_transaction_state"; password_step=None
  ↓                       ★ 注意：连 GET /log-in/password 都没发出去
auth_flow.py:1244      if not allow_passwordless:  ⇒ return existing_login_password_step_unknown
  ↓ registration_handlers.py:1379  allow_passwordless = not s.existing_account = False
  ↓ registration_handlers.py:74-77 EXISTING_LOGIN_DEAD_END_ERRORS 命中 ⇒ mark_dead_end
终态 partial_registered
```

### 2.2 为什么说是「确定性」而不是「概率」

两条规则**各自都成立**，合起来使该路径**永远不可能成功**：

| 规则 | 位置 | 单独看是否合理 |
|---|---|---|
| R1：已在 `/email-verification` 就别 POST `authorize/continue`（会污染 session） | `auth_flow.py:1115-1133` | ✅ 合理（注释记录了 09-14 的 3/3 `401 login_failed` 实测） |
| R2：没有 transaction state 就别发 doomed GET（实测 97/101 白花） | `auth_flow.py:186-198` | ✅ 合理（省一次必然 400 的请求） |

**但 R1 制造了 R2 所依赖前提的缺失**：R2 假设「没 state 就放弃」是安全兜底，
而在 `existing_account=True` 路径上，`allow_passwordless=False` 使 `None`
**不是兜底而是终局**。两条保守规则叠加 ⇒ **该路径成功率为 0**。

本轮 19/19、19:1x 批次同型失败 —— 两批合计 **0 例外**。

---

## 三、与参考项目 abai 的三条具体差距

abai 源码：`platforms/chatgpt/protocol_register.py`。

### 差距 1（核心）：abai 在 email-verification 落点上**照发 `authorize/continue`**

```python
# abai:1061  _submit_login_email
"""Ask the auth transaction for the existing-account login method.

The HTML email-verification page is only a generic shell.  The
``authorize/continue`` response carries the transaction-bound
``continue_url`` for the password form; navigating to
``/log-in/password`` without that state returns HTTP 400.
"""
response = self.session.post(
    OPENAI_API_ENDPOINTS["signup"],
    json={"username": {"value": email, "kind": "email"},
          "screen_hint": "login"},          # ← 我们两条泳道都没有这个字段
    headers=headers,
)
```

abai 的 `login()` 流程（`:1597`）**无条件**先调 `_submit_login_email` 拿到
`login_continue_url`，**再** `_load_login_password_page`。
**它没有「已经落在 email-verification 就跳过」这条分支。**

### 差距 2：payload 少 `screen_hint: "login"`

| | abai | 我们 |
|---|---|---|
| 登录泳道 POST body | `{"username":…, "screen_hint": "login"}` | `{"username":…}` — `auth_flow.py:1139-1142` |
| 注册泳道 POST body | — | `{"username":…}` — `auth_flow.py:455` |

`screen_hint=login` 我们**只出现在 authorize 的 URL query** 里（`auth_flow.py:1045/1075`），
**没出现在 `authorize/continue` 的 body** 里。
§二 的 R1 注释记录「POST 之后 session 仍带 signup-era state」——
**这与「body 缺 `screen_hint: "login"`，服务端把这次 POST 当注册流程继续」高度吻合**。
⚠️ 这是**强假设，尚未实测**（验证成本 = 1 次零 OTP 的探测，见 §五 P0-2）。

### 差距 3：abai 的 fallback target 不为空

```python
target = str(continue_url or "/log-in/password").strip()   # abai:1195
```
abai 在 `continue_url` 为空时**默认打 `/log-in/password`**，我们直接放弃。
（注：abai 的这个 fallback 实际很少生效，因为差距 1 保证它总有 state。）

---

## 四、附带发现：一句会撒谎的日志

`registration_handlers.py:1296`

```python
print("  Account already exists, password may differ from generated one; clearing stored password.")
s.create_ok = True
s.password_unknown = True
s.existing_account_password_known = bool(c is not None and (c.explicit_password or c.password_from_storage))
```

**实际没有任何「clearing」动作** —— 没有清 `accounts.password`，没有清
`s.password`，也没有删任何凭据。它只置了三个标志位。
这句话在排查时会把人引向「密码被清掉了」这个不存在的方向。

---

## 五、为什么「修好链路也救不回这 19 个」

`_login_probe_password`（`registration_handlers.py:72`）在 `existing_account=True` 时：

```python
return password if existing_account_password_known else ""
```

本批 19 个走的是 **passwordless 泳道**（`Registration mode: passwordless_signup`，
21/21），`c.explicit_password` 与 `c.password_from_storage` 均为假
⇒ `existing_account_password_known=False` ⇒ 提交的密码是 `""`。

于是把探针修好之后只剩两种结局，**都不是成功**：

| 探针返回 | 后续 | 结局 |
|---|---|---|
| `True`（有密码步） | `_login_probe_password` 返回 `""` | `existing_login_password_required` ❌ |
| `False`（无密码步） | email lane 是已测死路（5/5 落 `/about-you`，0 session） | `existing_login_no_password_step:*` ❌ |

**并且这个「无密码半注册不能协议登录」的结论，已被
`docs/audits/plan-2026-09-16-partial-account-protocol-login.md` 独立证过**
（服务端闭环 23/23，方案 C 已判 H2 证伪结案）。本轮数据与之一致。

### 那这 19 个地址到底怎么来的

**是选源问题，且有独立证据链：**

| 证据 | 数值 | 含义 |
|---|---|---|
| 本批 20 个唯一邮箱在 `registration_audit` 里的历史 | **各仅 1 条 = 本批** | 我们**从未**注册过它们 ⇒ 不是我们制造的占用 |
| 同主地址的 `+oai01` 变体 | **20/20 在 19:29–19:41 注册成功** | 主账号本身干净，且 OpenAI **不归一化** `+tag` |
| `22chariot.infants` 三变体 | base(11:07) + `+oai01`(19:41) + `+oai02`(22:35) **全成功** | 铁证：同一主地址可开 ≥3 个账号，不存在「主地址已占则子地址全废」 |
| `+oai02` 成功率 | 09-15 05h 37 个 → 09-15 白天个位数 → **09-16 全天 2 个** | 断崖，说明该后缀条目在池中被大量消耗 |
| 池文件 22:06 更新 | 1342 → 1024 条，**移除 389 条（其中 368 个 `+oai02`）** | 运营方在做清理，但**留下的 226 条仍被占** |
| 池条目结构 | `knobby.callow_7k+oai01` 与 `+oai02` **共用同一 token** | 同一 iCloud 主账号的两个独立地址，池方按序号派生 |

⇒ **`icloud-api.top` 是共享别名池，其 `+oai02` 条目已被第三方（或历史重复投递）占用。**
本轮 20 个里只有 1 个还干净，所以「只成功了一个」。

---

## 六、建议动作（按优先级，含取舍）

### P0-1 · 选源（**唯一能真正提高成功率的动作**）

| 选项 | 做法 | 代价 | 风险 |
|---|---|---|---|
| **A. 换/加邮箱源** | 停用 `icloud_url` 的 `+oai02` 条目，或引入第二个 provider | 换源成本 | 最低 |
| **B. 池条目前置探测** | 投递前对每个地址做一次 `login_or_signup`（**零 OTP**），落点 `/log-in/password` 或后续 `user_already_exists` 即判脏、剔除 | 每地址 1 次请求 + 1 个出口 | 探测本身可能触发风控；需控制并发 |
| **C. 后缀轮换** | 优先 `+oai01`（今日 60 成功 / `+oai02` 今日 2 成功），`+oai02` 降级或停用 | 池可用量下降 | 低 |

**推荐 A+C 组合**，B 作为 A 落地前的止血。**注意：这三项都不需要动链路代码。**

### P0-2 · 探针修复 ✅ **已落地（2026-09-16 23:0x，见 §八）**

> **落地形态**：`authorize/continue` 的 POST body 补 `screen_hint: "login"`，
> 并在 email-verification 落点上**不再跳过**该 POST；受
> `registration.existing_login_continue_on_verified_page` 控制（默认 True，可一键回退）。
> 下面是立项时的原始分析，保留作为设计依据。

把 `authorize/continue` 的 POST body 补上 `screen_hint: "login"`，
并**在 `existing_account` 路径上不再跳过该 POST**（对齐 abai `_submit_login_email`）。

- **验证成本极低**：一次零 OTP 的探测即可判定 —— 观察 POST 后
  `continue_url` 是否非空、`/log-in/password` 是否返回带密码表单的 200。
- **必须先验证再改**：§三 差距 2 的「缺 `screen_hint` 导致 session 被污染」
  是**强假设**，若假设错，直接放开 POST 会复现 09-14 的
  `401 login_failed`（3/3）。**建议按「加 `screen_hint` → 单地址灰度 → 看 continue_url」三步走。**
- **收益**：失败原因从 `existing_login_password_step_unknown`（像链路坏了）
  变成 `existing_login_password_required` / `existing_login_no_password_step:*`（像地址脏了），
  **排查方向不再被误导**；同时 P1-1（密码优先）落地后，有密码的半注册账号能真正被救。

### P1 · 观测与文案

1. **删掉 `registration_handlers.py:1296` 那句假话**，或改成实际动作描述。
2. **把「落点分布」做成被监控信号**：`/log-in/password` 落点占比从
   19:1x 的多数 → 本批 0/21，这个漂移本身是服务端行为变化的早期信号，
   而当前**零告警**。
3. `+oaiNN` 后缀成功率纳入池健康看板（§五表格里那条断崖，本该早就报警）。

### 不做

- ❌ **不要**把 `partial_registered` 当成「链路失败」去修链路 —— 会修错方向。
- ❌ **不要**因为「19 个都失败」就加大并发重投 —— 地址是脏的，重投只是烧 OTP 和出口。

---

## 七、一句话回答「是不是链路问题」

**是链路问题，但不是你想的那个链路问题。**

链路有真缺陷（探针在 `existing_account` 路径上确定性失效，19/19），
**但这个缺陷只是「把地址脏了这个事实报成了登录失败」**；
真正让成功率掉到 1/20 的是**邮箱源被占用**。
修链路能让原因说真话、并为「有密码的半注册」铺路；
**提高成功率的唯一手段是换源或前置探测。**

---

## 八、P0-2 落地记录（2026-09-16 23:0x）

**已落地。** 六处改动，其中第 5、6 项是顺带。

| # | 位置 | 改动 |
|---|---|---|
| 1 | `auth_flow.py:1173` | 跳过条件由「落在 email-verification」改为「落在 email-verification **且** 开关关闭」 |
| 2 | `auth_flow.py:1205` | 该路径的 POST body 增加 `screen_hint: "login"`（对齐 abai `_submit_login_email`） |
| 3 | `auth_flow.py:1207-1236` | 该路径的 POST **不可能让 lane 失败**：传输异常与非 200 一律回退为旧的「跳过」 |
| 4 | `auth_flow.py:65-100` | 新增 `_existing_login_continue_enabled()`，读 `registration.existing_login_continue_on_verified_page`（默认 **True**） |
| 5 | `registration_handlers.py:1296` | 删掉假话日志 `"...clearing stored password."`（实际无任何 clearing 动作） |
| 6 | `config.example.json` | 补上开关示例项 |

### 风险控制（本改动的核心设计）

新行为**只作用在原本 100% 失败的那条路径**上，三条退路：

- 开关关闭 ⇒ 逐字节回到 09-14 行为（连 `screen_hint` 都不加）
- 该路径 POST 被拒 / 超时 ⇒ 回退为「跳过」，**不新增失败模式**
- 非 email-verification 落点 ⇒ 一行未改（body 仍不带 `screen_hint`，非 200 仍照旧 `existing_login_continue_failed`）

### 验证

| 项 | 结果 |
|---|---|
| `tests/test_existing_login_password_probe.py` | **46 passed**（新增 13） |
| `tests/test_existing_login_validate_sentinel.py` | **11 passed**（新增 2；其中 1 个由「断言跳过」**反转**为「断言发送」） |
| 变异 | **2/2 KILLED**，恢复字节级一致（`(71141, 1567)` 前后相同） |
| 全量 | **4072 passed / 6 skipped / 656 subtests / 0 failed**（基线 4057 / 6 / 641） |

变异明细：

- **M1** 不再声明 `screen_hint` ⇒ `test_the_continue_post_declares_screen_hint_login` 红
- **M2** 恢复无条件跳过 ⇒ 3 个 continue 相关测试红

⚠️ **变异脚本自己踩的坑（已修，值得记）**：第一版把「删掉 `screen_hint` 那一行」当变异，
pytest 退出码是 **2**（语法错误 ⇒ 收集失败，`if` 块变空），而脚本按「`rc != 0` ⇒ KILLED」
记成了**击杀** —— **这是假绿**，一条断言都没跑。
修法两条：①变异体先 `compile()` 检查，**只有 `rc == 1`（测试失败）才算 KILLED**；
②每次变异前从基线 `copy2` 恢复，否则第二个变异会叠在第一个变异体上。

### 上线判据（跑小批次时只看这四行）

| 判据 | 日志 | 含义 |
|---|---|---|
| ✅ 新行为生效 | `Existing account continue: 200` | POST 发出去了（旧行为是 `skipped`） |
| ✅ 探针拿到 state | `password_step=True` / `=False` | 不再是恒 `None (no_transaction_state)` |
| ✅ 原因说真话 | `existing_login_password_required` | 地址被占 + 我们没密码 —— **修好的标志**（旧行为报 `existing_login_password_step_unknown`） |
| 🔴 **立即回退** | `email-otp/validate` 回到 `401 login_failed` | `screen_hint` 假设不成立 ⇒ 开关设 `false` |

**回退方式**：`config.json` → `registration.existing_login_continue_on_verified_page = false`。

### ⚠️ 本次**没有**解决的事

只让原因说真话，**没有提高成功率**。这 19 个地址被占且我们无密码，
探针修好后它们仍会失败（`existing_login_password_required` / `existing_login_no_password_step:*`），
**只是失败原因变准确了**。提高成功率仍然是 §六 的 **P0-1 选源**。

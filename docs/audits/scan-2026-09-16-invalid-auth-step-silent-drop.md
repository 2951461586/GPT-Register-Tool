# 扫描：`invalid_auth_step` 让 21 个地址静默蒸发 —— 2026-09-16

> **性质**：取证/诊断文档（只读分析，**未改动任何生产代码**）。
> 对象批次：`command_id=cb8dbbe956404521ab341177ef23b8a3`（北京 10:59:35–11:07:39，PID 34632）。
> 本目录内容均为历史快照，不构成当前架构契约。

## 结论（TL;DR）

**根因是两层，缺一不可。**

1. **服务端把「地址已存在」的表现位置提前了。** 09-15 服务端在
   `POST user/register` 的 **200 响应体**里回 `user_already_exists`；09-16 服务端改为在
   **`login_or_signup` 路由**就把事务送到 `/log-in/password`。旧代码在 `user/register`
   才看到结果，此时拿到的已是 **400 `invalid_auth_step`**。
2. **代码在拼错误串时用了人类可读 message，而不是机器码 code。** `else` 分支写的是
   `f"user_register:{err_msg}"`，其中 `err_msg = "Invalid authorization step."`，
   而分类器的 marker 表里是 **`invalid_auth_step`**（下划线 code 形式）⇒ 匹配不上 ⇒
   落到兜底类 `unknown`。

`unknown` **既不在** `BATCH_RETRY_CLASSES`（换代理重试）**也不在** `BATCH_DROPPED_CLASSES`
（记掉号）⇒ **不重试、不记录、静默丢弃**。这就是「成功率 1/22」而日志上只留一行
`Invalid authorization step.` 的全部原因。

**一行改动即可把危害从「静默丢弃」降为「换代理重试」**（见 §五 P0）。

---

## 一、批次事实（可复核）

| 项 | 值 |
|---|---|
| `command_id` | `cb8dbbe956404521ab341177ef23b8a3` |
| `batch_id` | `registration_20260916_105935_f1c8fa` |
| PID / 日志 | `34632` / `runtime/logs/processes/34632/backend_stdout.jsonl` |
| 窗口 | 北京 10:59:35 → 11:07:39（UTC 02:59–03:07） |
| 账号数 | **22** |
| 结果 | **1 成功 / 21 失败** |
| 成功账号 | `22chariot.infants@icloud.com`（`ref=f8c4cba287a0d1ae`，`registration_country=VN`，`at_status_code=200`） |

**21 个失败的签名完全一致**：

```
error          = user_register:Invalid authorization step.
failure_class  = unknown
at_status_code = 0
registration_country = ''
```

失败邮箱（明文取自 `registration_audit`）：

```
favor.ratings.0g@icloud.com      deals.riser-6a@icloud.com
knobby.callow_7k@icloud.com      dream.melts-6t@icloud.com
nines-minks3p@icloud.com         widths-rials-74@icloud.com
earners.trowels.9m@icloud.com    abject-ranch-6g@icloud.com
shorts_bellmen.9k@icloud.com     morales.six.1s@icloud.com
lookup.labs-4l@icloud.com        thrifty-punter4f@icloud.com
elects_inboxes_5p@icloud.com     12-beguine-dilemma@icloud.com
lofty-giggly-57@icloud.com       equines-conduct-2h@icloud.com
interns-slices9r@icloud.com      blaster-campo-86@icloud.com
chalky.spates8k@icloud.com       mochas_thesis_0w@icloud.com
25_chunks_mime@icloud.com
```

---

## 二、根因第一层：服务端换了表现位置

### 2.1 `login_or_signup` 落点分布

| 落点 | 次数 |
|---|---|
| `200 /log-in/password` | **21** |
| `200 /email-verification` | 1 |

### 2.2 这个落点是**本批首次出现**

扫描 `runtime/logs/processes/*/backend_stdout.jsonl` 中全部 19 个含 `login_or_signup`
的历史批次：

| 日期 | PID | `/email-verification` | `/log-in/password` | `/create-account/password` |
|---|---|---|---|---|
| 09-15 05:16 | 23840 | 17 | 0 | 0 |
| 09-15 06:02 | 3408 | 57 | 0 | 0 |
| 09-15 07:34 | 4520 | 81 | 0 | 4 |
| 09-15 17:11 | 28260 | 21 | 0 | 0 |
| 09-16 04:42 | 25288 | 18 | 0 | 2 |
| 09-16 10:06 | 18516 | 10 | 0 | 0 |
| **09-16 10:39** | **24272** | **27** | **0** | 2 |
| 09-16 10:53 | 29004 | 2 | 0 | 0 |
| 09-16 10:56 | 6564 | 1 | 0 | 0 |
| **09-16 11:07** | **34632** | **1** | **21** | **0** |

⇒ **`/log-in/password` 在 09-15 至今的全部历史批次里出现过 0 次，本批首次出现 21 次。**
这是本次定位里最强的单一线索 —— 比任何单条错误串都硬。

**对照批次**：10:39 的 PID 24272（27/29 成功）落点**全部**是 `/email-verification`，
且与 34632 使用**同一个出口**（见 §三）。

### 2.3 代码路径

```
auth_flow.py:615   _is_existing_login_redirect(current_url)  → 真
auth_flow.py:633-645
                   signup_state = _continue_signup_username(...)
                   signup_state["password_fallback"] = True
                   print("Signup username continue: 200 /log-in/password")
registration_handlers.py:895  _password_lane_active()  → 真（因 password_fallback）
registration_handlers.py:903  self._post_user_register()
                   → HTTP 400  {"code": "invalid_auth_step",
                                "message": "Invalid authorization step."}
registration_handlers.py:908  err_code == "invalid_auth_step" 但
                   "email-verification" not in state_url（停在 /log-in/password）
                   ⇒ 走 else
registration_handlers.py:954  self._abort(f"user_register:{err_msg}")
```

**旁证（session 键集合的决定性差异）**：

| 批次 | `client_auth_session` 键 |
|---|---|
| 10:39 成功批 | 含 `email` + `email_verification_mode` + `passwordless_otp_from_password_redirect` |
| **本批失败** | **只有 `username`，没有 `email`** |

⇒ 本批事务**停在 username 提交**，**从未进入 email 验证步骤**。

---

## 三、根因第二层：拼串用了 message，分类掉进兜底类

### 3.1 服务端同时给了 code 和 message

```json
{"error": {"code": "invalid_auth_step", "message": "Invalid authorization step."}}
```

`failure_registry.py:148` 的 marker 是 **`invalid_auth_step`**（code 形式）。
`else` 分支拼的是 **`err_msg`**（message）⇒ 串里没有那个子串。

### 3.2 实测对照（同一响应，只改拼法）

```
$ python -c "from sms_tool.error_classification import classify_error; \
             from sms_tool.registration_policy import registration_retry_decision; ..."

'user_register:Invalid authorization step.'   → unknown    retryable=False   ← 实际发生
'user_register:invalid_auth_step'             → auth_state retryable=True
'password_step_unconfirmed:invalid_auth_step' → auth_state retryable=True
'existing_account_user_already_exists:continue_to_log' → account retryable=False
```

### 3.3 危害量级：兜底类的批处理语义是**空集**

```
BATCH_RETRY_CLASSES   = ['auth_state', 'mailbox', 'network', 'rate_limit']
BATCH_DROPPED_CLASSES = ['account']
unknown  ∈ 两者？      都不是
```

⇒ **不重试、不记掉号、不告警**。21 个地址从批次里蒸发。

**注意**：这不是「分类错了」，是**兜底类没有任何下游动作**。所以「拼错一个串」
的代价等于「21 个地址白跑」而不是「21 个地址被重试」。

---

## 四、已排除的假设（全部用数字否掉）

### 4.1 ❌ 出口被封 —— 出口不是变量

- `proxy.json` 的 `proxy.pool` 有 **30 个**候选，分 3 家供应商各 10 个：
  `global.9http.com`（index 0-9）、`us.ipwo.net`（10-19）、`gate.rola.vip`（20-29）。
- 预检（`sms_tool/commands/registration.py:126-160`）**短路在第一个候选**：

  ```
  [*] 注册预检：30 个候选路由（单候选上限 3 次、总预算 180s）
  [*] 注册预检 1/30 global.9http.com:9091 可用（6.7s）
  ```

  短路后 `ordered = [selected] + [其余]` = **原顺序**，`account_proxy_index = i % 30`
  ⇒ 22 个账号覆盖 index 0..21，**跨越 3 家供应商、22 个不同 sid**。
- **决定性对照**：10:39 成功批（24272）、10:53（29004）、10:56（6564）**预检选中的
  都是同一个 `global.9http.com:9091`（pool[0]）**，池序相同。

⇒ 同一出口、同一代码，10:39 批 27/29 成功、11:00 批 1/22。**出口被排除。**

> ⚠️ **坑（已记入记忆）**：`registration_audit.proxy_pool_index` 在协议驱动下**恒为 -1**，
> 因为它读的是 `result["proxy_audit"]`，而协议路径不产出该字段。
> **别把 `-1` 读成「没走代理」。**

### 4.2 ❌ `passwordless_disabled` 是根因

它在**成功批次里同样存在**：

| 批次 | dump 数 | 含 `passwordless_disabled` |
|---|---|---|
| 10:39 成功批 24272 | 58 | **2** |
| 10:53 批 29004 | 4 | **2** |
| 10:56 批 6564 | 2 | **2** |
| 11:00 本批 34632 | 23 | 2 |

⇒ 它只是这个出口/VN 地区的**常态标记**，**零分辨力**。

> ⚠️ 这纠正了本会话上一轮的一个推断（当时把它当成根因候选）。

### 4.3 ❌ 地址在本地已注册

- 21 个 `account_ref` 在 `accounts` 表（1459 条）中**全部为空**。
- `registration_audit` 里这 21 个 ref **只有本批的 1 条失败记录**。
- 与 09-15 那 **376 个 `user_already_exists`** 地址的**交集 = 0**。

### 4.4 ❌ 协议流程 bug

唯一成功的账号走完 **17 步**全流程（含 `create_account` 的 `409 invalid_state`
被随后的 `Create account continue: 200` 补上）⇒ 流程本身没问题。

---

## 五、修复建议（按性价比排序）

### P0 —— 一行改动，把「静默丢弃」降为「换代理重试」

`sms_tool/registration_handlers.py:954`

```python
# 现在
self._abort(f"user_register:{err_msg}")
# 改为
self._abort(f"user_register:{err_code or err_msg}")
```

效果：错误串变成 `user_register:invalid_auth_step` ⇒ `classify_error` → `auth_state`
（`retryable=True`、`batch_retry=True`）⇒ 这 21 个地址**会被换一个池代理重试**，
而不是蒸发。

> **代价**：`auth_state` 是 `batch_retry` 类 ⇒ 每批会为「地址已存在」多花一次重试预算。
> 如果 P1 落地，这个代价消失（P1 会把它们直接归到 `existing_account`，不重试）。

### P1 —— 语义正确性（需拍板）

`/log-in/password` 落点 + `invalid_auth_step` 的真实语义是
**「服务端说这个地址要登录，不要注册」** —— 与 09-15 的 `user_already_exists`
**是同一件事，只是表现位置提前了**。

应当归到 **`existing_account`**（走 `continue_to_log`，产出 `partial_registered`），
而**不是** `auth_state` 重试 —— 重试改变不了「地址已存在」这个事实。

**但必须与下面这条分开**（现有 `if` 就是按这个分的，只是 `else` 拼错了串）：

| `state_url` | 语义 | 处置 |
|---|---|---|
| 含 `email-verification` | 真·事务不在密码步 | `password_step_unconfirmed` → `auth_state` 重试 ✅ 现有行为正确 |
| `/log-in/password` | **地址已存在** | 应归 `existing_account` → `partial_registered`（本批缺失） |

### P2 —— 选源侧过滤

这 21 个地址本地无任何记录 ⇒ **本地判不出** ⇒ 需要一次 `login_or_signup` 探测
（**零 OTP 成本**）来标记「服务端已存在」，避免重复投喂。

### P3 —— 观测缺口（本次无人发现，靠人肉翻日志）

`/log-in/password` 落点**首次出现却零告警**。建议把 `login_or_signup` 的落点分布
做成**被监控的信号**：该占比突增 = 选源里混入大量已注册地址。

---

## 六、尚未定论（日志判不了，需一次实跑）

**为什么这 21 个「本地从未注册过」的地址在服务端已存在？**

两个候选：

- **(A) iCloud Hide My Email 别名被回收/复用** —— Apple 侧的别名可能被删除后重新分配。
- **(B) `remail` 购买模式买到别人用过的邮箱** ——
  `email_registration.remail.service_mode = "purchase"`，`supply = "private_first"`。

**验证方式（零 OTP 成本，安全）**：对这 21 个地址跑一次 `login_or_signup` 探测，
看是否**稳定**落 `/log-in/password`。
- 稳定 ⇒ 坐实「地址级已存在」，走 P2 标记过滤。
- 不稳定 ⇒ 是服务端时间窗策略，需要重新评估。

---

## 附：可复现命令

```bash
# 批次终态（含 proxy_pool_index，注意协议驱动恒为 -1）
python -c "
import sqlite3,json
c=sqlite3.connect('runtime/accounts.sqlite3')
for r in c.execute('select email,state,error,failure_class from registration_audit where batch_id=?',('registration_20260916_105935_f1c8fa',)):
    print(r)"

# 落点分布（扫全部历史批次）
python -c "
import glob,re,collections,os
for p in sorted(glob.glob('runtime/logs/processes/*/backend_stdout.jsonl'), key=os.path.getmtime):
    t=open(p,encoding='utf-8',errors='replace').read()
    if 'login_or_signup' not in t: continue
    print(os.path.basename(os.path.dirname(p)),
          dict(collections.Counter(re.findall(r'Redirect\[login_or_signup\]: \d+ (\S+)',t))))"

# 分类对照
python -c "
from sms_tool.error_classification import classify_error
for e in ['user_register:Invalid authorization step.','user_register:invalid_auth_step']:
    print(repr(e), classify_error(e))"
```

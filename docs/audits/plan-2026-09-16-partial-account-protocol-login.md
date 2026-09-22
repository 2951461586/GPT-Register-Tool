# 方案：半注册账号能否用协议登录拿到 session？— 2026-09-16

> **性质**：方案文档（含参考项目对照）。**不改任何生产行为**。
> 证据来源：`runtime/accounts.sqlite3`（只读）、`runtime/registration_retry_guard.json`、
> `runtime/logs/processes/*/backend_stdout.jsonl`（已过滤 IPC 载荷）、
> `runtime/tmp/refsrc/`（两个参考项目的源码副本）。

## 结论（TL;DR）

**分两类，结论相反：**

| 类别 | 能否协议登录拿 session | 依据 |
|---|---|---|
| **持有密码**的半注册账号 | ✅ **能**，零 OTP | 走 `_password_login_existing_account`（`password_step=True` 时）。代码已具备 |
| **无密码**的半注册账号 | ❌ **不能**，且是**服务端闭环**，不是我们的实现缺陷 | email OTP 能拿到码、`validate` 200，但落 `/about-you`；提交得 400 `user_already_exists`（23/23）；跟 `continue_to_login` 4/4 无 AT |

**本机的实际情况：所有 82 个半注册账号都**没有密码**（`accounts.password` 全空）
⇒ **存量半注册账号目前一个都恢复不了**。可行的动作只有两条：
**① 止损**（已在 P1-2 部分落地）；**② 预防**（P1-1 密码优先，让新账号不再变成这样）。

**2026-09-16 更新（方案 B 已落地）**：结果与审计里加了显式的
`needs_manual_session_recovery` 桶（§五 方案 B），把「已创建但无 session 且无密码」
与普通失败分开，面板可显示「不是失败，是待人工登录一次」。
⚠️ 今天它与 `partial_registered` 100% 重合（因为这 82 个全无密码），
价值在 P1-1 落地之后 —— 详见 §五 的 **B.4**。

**唯一还没试过的技术路线**在 §五「方案 C」——它有一个明确的可验证假设。
**2026-09-16 更新**：零成本取证已穷尽（§五 方案 C 的修订块），**三条独立反向证据、
零支撑** ⇒ 按预登记判据可判 **H2 证伪、方案 C 结案**。
若要拿「页面 HTML 原文」这条直接证据，成本不是 0：需 1 个邮箱 OTP（见 §五 的 C-1/C-2/C-3）。

## 一、术语与现状

「半注册」在本项目里有**精确含义**：`create_account` 回了 400 `user_already_exists`
（`registration_handlers._mark_partial_registration()`），即**服务端说这个地址已经有账号了**。
它**不等于**「我们自己创建了账号但丢了会话」——那是它的一个子集。

本机实测（`runtime/accounts.sqlite3` 只读）：

| 口径 | 数值 |
|---|---|
| accounts 总量 | 1502 |
| `success=1` | 1407 |
| `registration_state='partial_registered'` | **82** |
| 其中 **有密码** | **0**（82 全部无密码） |
| `success=1` 且 **无密码** | **965** |

⇒ **1047 个地址既没有可用 session、也没有密码**（82 + 965）。

## 二、现有实现盘点（**已经有的能力，别重复造**）

用户问的「结合参考项目给出方案」里，aBai 的两个关键手法我们**已经实现**：

| 能力 | 我们 | 位置 |
|---|---|---|
| `screen_hint=login` + `login_hint=<email>` + `prompt=login` | ✅ **已有** | `auth_flow.py:1041-1077`（`_login_existing_account_with_email_otp` 的 signin URL 与 `_ensure_authorize_context`） |
| 零成本判定「这个账号有没有密码步」 | ✅ **已有**：三值探针 `_probe_login_password_step`（`True`/`False`/`None` + `signal` + `source`） | `auth_flow.py:132-` |
| 已注册地址拒绝回落 email OTP | ✅ **已有**：`allow_passwordless=not s.existing_account` | `registration_handlers.py:1182` / `:1366` |
| 会话丢失后按 `web_session` 重放恢复 | ✅ 已有（需 session cookie） | `session_refresh.py` |

⇒ **aBai 的 `screen_hint=login` 不是我们缺的东西**，把它当成待办是重复立项。
我们的缺口在别处：**账号没有密码**。

### 2.1 半注册账号现在的实际结局（账本实测）

`runtime/registration_retry_guard.json`（428 行）`last_error` 分布：

| 错误 | 行数 | 含义 |
|---|---|---|
| `existing_login_password_step_unknown` | **301** | 探针答不出（`None`）⇒ 已注册地址**不允许**回落 email OTP ⇒ 止损 |
| `existing_account_user_already_exists` | 99 | 强证据：`create_account` 明确说已存在 |
| `user_already_exists` | 10 | 同上（另一条路径） |
| 其余（browser / missing AT 等） | 18 | — |

`dead_end_reason` 全为 `user_already_exists`（410 行）。
⇒ **止损方向是对的**：这些地址继续重试只会烧邮箱槽。

## 三、为什么 email OTP 登录是死路（实测链）

```
authorize(screen_hint=login, login_hint=<email>)
  → email-otp/send → 取码 → email-otp/validate 200     ← 到这里都成功
  → continue_url 落 /about-you                          ← 卡住点
  → POST /api/accounts/create_account {name, birthdate}
      → 400 user_already_exists                         23/23
      → redirect_uri = chatgpt.com/auth/login_with
      → userAlreadyExistsRecovery.action = continue_to_login
  → 跟该重定向 → 4/4 拿不到 access token → 回到 /about-you   ← 闭环
```

**根因**：`/about-you` 只有「**创建**账号」（`create_account`）这一个出口，
而该端点对已存在的地址**必然失败**。所以对已存在账号，
`/about-you` 是一个**服务端自己没留出口**的状态。

代码里这条链已经实现并且**故意不走重定向**（避免每圈烧一个码）：
`codex_oauth.py::_complete_about_you`（`codex_oauth.py:659-`），
返回 `about_you_existing_account_user_already_exists:continue_to_login`，
注释里写明「`terminal` 必须缺省，否则 `_persist_permanent_deactivation` 会把活账号标成停用」。

## 四、参考项目对照

`runtime/tmp/refsrc/`（aBaiFreeGPT + turb 的源码副本）全文检索结果：

| 检索项 | aBaiFreeGPT | turb |
|---|---|---|
| `user_already_exists` | **0 次** | **0 次** |
| `about-you` 的处理 | 只做**导航**（`abai_protocol_register.py:519`）与 `create_account` 提交（`:1287`），**假定成功** | `navigate_about_you()` + `create_account()`（`turb_openai_auth.py:438/526`），**假定成功** |
| 已存在账号的恢复 | **没有** | **没有** |
| 怎么绕开这个问题 | **强制密码优先**：`if not password_registered: raise RuntimeError("OpenAI 注册流程未确认远端密码设置，拒绝创建无密码账号")` | 同样先设密码再走 OTP |

**⇒ 两个参考项目都不处理 `user_already_exists`。** 它们的策略是
**「让这个状态不发生」**，而不是「发生了怎么救」。

**这就是本次对比最有价值的结论**：我们和参考项目的差距不在恢复能力，
而在**是否允许产生无密码账号**。它们不允许，我们允许（`passwordless` 是默认值）。

## 五、方案

### 方案 A（推荐，预防）：密码优先注册

让新账号**带密码**，从根上消灭「无密码 ⇒ 不可恢复」这一类。

- 详 `plan-2026-09-16-password-first-registration.md`（含唯一待验假设 H1、灰度设计、回滚）。
- 收益：新账号丢会话时可走 `_password_login_existing_account` **零 OTP** 恢复。
- 不做的理由（明确否决）：**不给存量账号补密码** —— 未登录状态下服务端不允许给
  已存在账号设密码（`create_account` 只对不存在的地址有效）。存量只能止损。

### 方案 B（存量，止损）：把半注册账号显式分流

现状：82 个半注册 + 965 个无密码成功账号，其中部分会反复被驱动。

- **已做**：P1-2 已把 `auth_session_recovery_*` 接进死路账本（切断「每批烧 1 个邮箱槽」）。
- **已做**：`existing_login_password_step_unknown` 等错误路径
  已在 `_is_existing_login_dead_end_error` 下 `mark_dead_end`。
- ✅ **已落地（2026-09-16）**：结果/审计里加了显式的 **`needs_manual_session_recovery`** 桶。

#### B.1 判据（单一 owner）

```python
# sms_tool/registration_outcome.py
needs_manual_session_recovery(
    *, success, access_token="", existing_account=False,
    existing_account_password_known=False,
) -> bool
```

三个事实，缺一不可：

| 事实 | 来源 | 为什么不能省 |
|---|---|---|
| **已创建** | `existing_account`（只在 `create_account` 答 `user_already_exists` 时置位） | 没有它，普通失败也会进桶。`registration_state` 恰为这个标志产出 `partial_registered` ⇒ 本桶是那 82 行的**子集**，不是新population |
| **无 session** | `access_token` 为空 | 顺带覆盖 `not success`：`_registration_outcome` 只在有非空 token **且** 探测 200 时才算成功 |
| **无密码** | `existing_account_password_known` 为假 | 🔴 **不能**改用 `password_unknown` —— 后者对**每个** `user_already_exists` 都置位（它是落库卫生），用它会把「我们握着密码、密码登录泳道**零 OTP** 就能救回来」的地址一起卷进来，对每一个都**误报**「待人工」 |

判据**不读** `registration_state`：`finalize` 用同一批标志算出那个字符串，
而 `_abort_result` 事后还会把它覆写成 `cancelled` / `partial_registered` ⇒
读回来会让桶取决于「这次走的是哪条退出路径」。

#### B.2 装配与落库

- 两条装配路径都带这个键：`_abort_result`（失败侧，经 `_failure_result` 的关键字参数）
  与 `finalize`（完成侧，进 `extra`）。半注册是**跑到 `finalize`** 的
  （`fetch_auth_session` 对它不 abort，改问登录方式探针），所以只补失败侧会漏掉
  实际产出的那批行。
- `registration_audit.detail_json` 的**封闭白名单**已同步（`store/accounts.py`）。
  漏加 = 静默丢弃，这正是 09-15 前 `existing_login` 在 4377 行里 0 次命中的原因。
- 键**永远存在**（无值时 `False`），所以「没进桶」与「这行早于该字段」分得开。

#### B.3 验证

- `tests/test_needs_manual_session_recovery.py`：**16 例**
  （判据真值表 6 + 关键字签名 1 + 一票否决 1 + 两条装配 4 + 审计 3 + 防漂移 1）。
  防漂移那条拿**结果 dict 本体**去写审计行再读回来比对，两处键名任一边改掉就红。
- 变异 `runtime/tmp/mutations_plan_b.py`：**8/8 承重**
  （NA 完成侧装配 / NB 密码半 / NC session 半 / ND 已创建半 / NE 递参 / NH success 传参 / NF 白名单漏加 / NG 白名单写死 False）。

#### B.4 ⚠️ 今天的桶与 `partial_registered` **100% 重合**，别误读成无效

§一 已记录：**当前 82 个半注册账号的 `accounts.password` 全空**
⇒ `existing_account_password_known` 对它们**全为假** ⇒ 今天
`needs_manual_session_recovery == (registration_state == 'partial_registered')`。

**这个字段的价值不在今天，在 P1-1 落地之后。** 一旦密码优先注册生效，半注册里会出现
「我们握着密码」的行 —— 那些走密码登录泳道**零 OTP** 就能救回来，**不该**被报成待人工。
判据现在就把这条分岔写死（`existing_account_password_known` 那一半，由变异 NB 承重），
而不是等有了样本再补：等有样本时，误报已经在面板上跑了一整批。

⇒ 因此**不要**用「今天两者数值相同」当作「这个字段是冗余」的证据。
真正的判据是变异 NB —— 把它拿掉，`password-held-...` 那条用例立刻红。

### 方案 C（探索性，唯一没试过的路线）：用「更新资料」代替「创建账号」

**假设 H2**：

> `/about-you` 卡住，是因为我们 POST 的是 `create_account`（对已存在地址必然 400）。
> 但 `email-otp/validate` 200 之后，这个事务**已经证明了我们拥有该邮箱**。
> 如果存在一个「给**已登录**账号更新资料」的端点，就能走出闭环。

**为什么值得一试**：这是唯一一个**不依赖新账号**、能救存量的方向。
其它方向（重试 OTP、重建事务、跟 `continue_to_login`）都已被实测证伪。

**零成本取证（先做这一步，不要先写代码）**：

> ### ⚠️ 2026-09-16 修订：这一步的成本被低估了，且零成本侧已给出强反向证据
>
> **① 原文假设「零真实请求」不成立。** 要停在 `/about-you` 必须先过 `email-otp/validate`
> （实测：`validate` 200 之后才落 `/about-you`）⇒ 成本 = **1 个邮箱 OTP + 一次邮箱轮询（约 59s）**，
> 不是「零成本」。下面给出三条**真正零成本**的替代取证，**已执行**。
>
> **② 零成本取证已穷尽，结果一致指向 H2 证伪：**
>
> | # | 探针 | 结果 |
> |---|---|---|
> | 1 | **服务端自陈的恢复枚举** —— 穷举全量日志里 `userAlreadyExistsRecovery.action` 的取值 | **只有 `continue_to_login`，102/102，零例外**（严格正则 `"action"\s*:\s*"([^"\n]*)"`）⇒ 服务端对该状态的**机器可读恢复指令**只有「继续登录」，**没有**「补全资料」 |
> | 2 | **全量端点词表** —— 穷举 `runtime/` 全部日志里出现过的 `auth.openai.com` / `chatgpt.com` URL | 账号相关端点只有 `email-otp/send`、`authorize`、`authorize/continue`、`create-account/password` 四个 API，加 `about-you` / `email-verification` / `verify-your-identity` 三个页面。**没有任何 profile / update_account / complete_profile 类端点** |
> | 3 | **`/about-you` 的提交端点** —— `create_account` 的 POST 带着 `Referer: https://auth.openai.com/about-you`（`codex_oauth.py:713`、`registration_handlers.py:1163`），且注册泳道**在同一页面**上 POST `create_account` 得到 **200** | `/about-you` 的表单端点就是 `create_account`；它对**全新**地址成功、对**已存在**地址回 400。**同一页面不会按账号是否存在换端点**（否则服务端会先重定向走） |
>
> **③ 另外两条零成本负结果**（免得下次重查）：
> - 全仓**没有** `__NEXT_DATA__`、**没有**任何 `.html` / `.har` / `.mhtml` 页面快照；
>   浏览器泳道**从不调用** `page.content()` ⇒ **离线取证在结构上不可能**。
> - 两个参考项目（`runtime/tmp/refsrc/`）的 `about-you` 处理都只有
>   **导航 + `create_account` 提交**，`user_already_exists` 各 **0 次** ⇒ 无端点线索。
>
> **④ 结论**：H2 的支撑证据 = **0**，反向证据 = **3**。按预登记判据第 2 条，
> **方案 C 可判结案**。若仍要拿「页面 HTML 原文」这一条直接证据，见下方「若要硬做」。

**原文预登记的步骤（保留，供「硬做」时执行）**：

1. 对一个已存在的半注册地址走 login 事务，停在 `/about-you`，**保存页面 HTML 与
   `__NEXT_DATA__`**，从表单 `action` / 内嵌 JSON 里读出**该页面真正提交的端点**。
   —— 注意：我们**没有**做过这件事，`create_account` 是我们按注册泳道**假定**的目标。
2. 若页面给出的仍是 `create_account` ⇒ **H2 证伪**，方案 C 结案。
3. 若给出另一个端点 ⇒ 用**同一个**登录事务 POST 一次，观察是否返回 session cookie。

**判据**：`/api/auth/session` 返回 `accessToken`（不是 `keys=['WARNING_BANNER']`）。

**🔴 不要猜端点名**。凭印象试 `/api/accounts/profile` 之类是浪费真实请求；
必须先做第 1 步的页面取证。本项目已有教训：**探针要覆盖判别区间两端、
「测试 0 引用某符号」不等于缺口**（`audit-playbook.md` ⑯⑰）。

**若要硬做（三个成本递增的选项，**需拍板**）**：

| 选项 | 成本 | 能拿到什么 | 风险 |
|---|---|---|---|
| **C-1** 未认证 GET `https://auth.openai.com/about-you` | **1 次请求，零 OTP** | 若返回 HTML 外壳 ⇒ 直接读到表单 `action`；若 302 到登录页 ⇒ 无收获（**负结果也是结论**） | 无状态改变 |
| **C-2** 用**有密码**账号（442 个可用）密码登录（零 OTP）后 GET `/about-you` | **2 次请求，零 OTP** | 登录态下的页面 HTML / 表单端点 | 无状态改变 |
| **C-3** 原文路线：半注册地址走完整 login 事务 | **1 个邮箱 OTP + ~59s 轮询** | 与故障现场**完全同态**的页面原文 | 消耗 1 个邮箱槽；不改死路账本 |

⇒ **建议顺序 C-1 →（若被重定向）C-2 →（仍无收获）C-3**，每步单独拍板。

### 方案 D（明确否决）

| 否决项 | 理由 |
|---|---|
| 重复调 `email-otp/send` 再试一次 | 已实证：**再次调用发码端点会作废事务**（aBai 注释 + 我们 09-14 实测 `validate` 仍 409） |
| 跟 `continue_to_login` 重定向 | 实测 4/4 无 AT，且每圈烧一个码 |
| 重建「注册前置探针」（`user/register`） | 09-16 已删，两条独立理由；`user_register` 已写明 "Do not reintroduce it" |
| 用 `terminal: True` 标记这些账号 | 会触发 `_persist_permanent_deactivation()`，**把活账号标成停用** |
| 测 `chatgpt_email_otp` 链 | `auto` 链里排第三，**生产成功 = 0** |

## 六、建议的执行顺序

1. **方案 B 的显式分流**（低成本，纯本地字段 + 白名单，可立即做）。
2. ~~**方案 C 的第 1 步页面取证**~~ ⇒ **已判结案**（09-16：零成本三条反向证据，
   见 §五 修订块）。仅当拍板「要 HTML 原文」时才走 C-1 → C-2 → C-3。
3. **方案 A 的 S0 离线单测**（零外部成本，把状态机测厚）。
4. 方案 A 的 S1 灰度（**需真实批次，单独拍板**）。

## 附：本方案用到的取证命令

```bash
# 半注册 / 无密码 分布（只读）
.venv/Scripts/python.exe runtime/tmp/p11_partial_login_evidence.py

# 恢复窗口判决在账本里有没有痕迹（预期：0 —— 正是根因指纹）
.venv/Scripts/python.exe - <<'PY'
import json,pathlib
d=json.loads(pathlib.Path("runtime/registration_retry_guard.json").read_text(encoding="utf-8"))
print(sum("auth_session_recovery" in str(r.get("last_error") or "") for r in d.values()))
PY

# 进程日志里的恢复过期命中（必须过滤 IPC 载荷）
grep -roh "auth_session_recovery[a-z_]*" runtime/logs/processes/ | sort | uniq -c
```

# 立项：密码优先注册（password-first）— 2026-09-16

> **性质**：立项/方案文档，**未落地任何生产默认行为改动**。
> 本文只定义「要验证的唯一假设」「实验设计」「落地边界与回滚」。
> 背景与证据来源见 `scan-2026-09-16-protocol-registration-optimization.md` §P1-1 / §六。
> 本目录内容均为历史快照，不构成当前架构契约。

## 结论（TL;DR）

**建议立项，但只验证一个假设。** 不要照搬 aBai 的「强制密码优先」，
因为我们 09-15 已经用「POST `user/register` 探测」踩过一次坑（该探针 09-16 已删）。
两者的**关键差异不是「要不要发 POST」，而是「POST 之后拿不拿响应去重算事务状态」**。

| | 我们已删的探针 | aBai 的做法 |
|---|---|---|
| POST 之后 | **忽略响应**，继续按 passwordless 假定路径走 | **采纳响应**，用新 `page_type` / `continue_url` 重算 |
| 是否主动建状态 | 否 | **是**：`_visit_auth_step(continue_url)` 主动 GET 新 URL |
| 发码 | 假定已预发 | 按 `otp_already_dispatched = _email_otp_step(...)` 重算，已发则跳过 |
| 失败处置 | 继续走 | `if not password_registered: raise` —— **拒绝创建无密码账号** |

## 一、为什么值得做（证据）

### 1.1 存量欠账（本机实测，`runtime/accounts.sqlite3` 只读）

| 口径 | 数值 |
|---|---|
| accounts 总量 | 1502 |
| `success=1` | 1407 |
| 其中 **无密码** | **965（68.6%）** |
| 其中 有密码 | 442 |
| `registration_state='partial_registered'` | **82 —— 全部无密码** |

按天看，`has_password` 只在 09-12 / 09-14 / 09-15 各出现 **4 个/天**（`password_fallback`
的产物），其余全部 passwordless。**passwordless 事实上是唯一在跑的泳道。**

### 1.2 无密码 = 不可恢复（这是真正的成本）

会话一旦丢失，恢复只有两条路：

1. **密码登录** —— 走 `_password_login_existing_account`（`existing_login_password_step=True`
   时）。**无密码 ⇒ 这条路根本不存在。**
2. **email OTP 登录** —— 实测是**服务端闭环**：OTP 能拿到、`validate` 200，
   但 `continue_url` 落 `/about-you`；提交 `/about-you` 得 400 `user_already_exists`
   （23/23）；跟 `continue_to_login` 重定向 4/4 拿不到 AT。**0 session。**

⇒ 库内 **965 个成功账号 + 82 个半注册账号**都没有恢复手段。详见
`plan-2026-09-16-partial-account-protocol-login.md`。

### 1.3 参考实现选了相反的路，且理由直白

aBaiFreeGPT（`runtime/tmp/refsrc/abai_protocol_register.py`）：

```python
# _create_account_from_authorization()
if _email_otp_step(page_type, continue_url):
    if not password_registered:
        # OpenAI can default a new signup to the passwordless OTP branch.
        # Require the remote password API to accept our password before
        # consuming an email verification code.
        self._visit_auth_step("/create-account/password", referer=continue_url or "/create-account")
        password_result = self._register_password(email, password)
        self._direct_registration_mutated = True
        password_registered = True
        page_type = _authorization_page_type(password_result)
        continue_url = _authorization_continue_url(password_result)
        ...
        continue
...
if not password_registered:
    raise RuntimeError("OpenAI 注册流程未确认远端密码设置，拒绝创建无密码账号")
```

两条收益，都是我们缺的：

1. **不产生无密码账号** ⇒ 会话丢了还能用密码捞回来（零 OTP）。
2. **设密码是零 OTP 成本的**，而且发生在**烧码之前** ⇒ 失败就 abort，**不烧码**。

## 二、唯一待验证的假设 —— ✅ **H1 成立（2026-09-16 判定）**

> **H1：POST `user/register` 之后，采纳响应并用新 `continue_url` 主动
> `_visit_auth_step` 重建事务状态，能否让随后的 `email-otp/validate` 不再回 409？**

### 2.0 判定：**成立**。破坏点不是 POST 本身，而是**忽略响应**

**不需要跑 S1 灰度就已经可判** —— 生产日志里存在**天然对照组**，两组用的是
**同一个** `_post_user_register()`、**同一个 payload**（`{"password": …, "username": …}`），
唯一差别是「跟不跟响应」：

| 组 | 路径 | run | 有 validate | 全 200 | 含 409 | **409 率** |
|---|---|---|---|---|---|---|
| **B** | POST 被接受 + **没跟**响应 | 85 | 85 | 0 | 85 | **100.0%** |
| **C** | POST 被接受 + **跟了**响应 | 12 | 12 | 12 | 0 | **0.0%** |
| A（对照） | 未 POST（passwordless） | 360 | 360 | 355 | 1 | 0.3% |

**Fisher 单侧精确检验 p = 1/C(97,12) = 1.4e-15。** 分离是完全的（85/85 vs 12/12）。

- **B 组** = 已删的 `_probe_register_before_otp`。它**做了**主动访问
  （`Pre-OTP password step: 200 https://auth.openai.com/create-account/password`，
  即 aBai 的 `_visit_auth_step` 等价物）**也做了**同一个 POST，但
  `[4-Trigger email OTP]` 走的是 passwordless 的**合成 204 分支**，
  **从未跟过响应的 `continue_url`**。
- **C 组** = 密码泳道（由服务端 `passwordless_disabled` 触发 `password_fallback`，
  见 `auth_flow.py:633-645`）。`send_email_otp` 会
  `_email_otp_send_url(reg_data)` → `_follow_continue_url(...)` **跟响应**。

**机制（比原假设更简单）**：`user/register` 的 200 响应体里**自带跟法**：

```json
{"continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
 "method": "GET",
 "page": {"type": "email_otp_send", "backstack_behavior": "default"}}
```

⇒ 服务端明说 `"method": "GET"`，而我们的 `_follow_continue_url` **本来就是 GET**
（`http_utils.py:52` "GET a continue URL"）。所以 C 组的「采纳」是**方法正确**的，
B 组只是没跟。**不是「跟错了方法」，是「根本没跟」。**

**业务价值也已确认**：C 组 12 个 run **全部**走到 `[10-Finalize registration]`，
`Auth session: 200` + `Access token probe: HTTP 200`，**无 `[!] Registration failed` 行**
⇒ 跟响应不仅让 validate 200，**也真的产出了账号**。

**⚠️ 样本口径的诚实说明**：本方案 §2.2 预登记过「样本 < 20 ⇒ 不可判，扩样本重跑」。
那条规则是给**前瞻性灰度实验**写的（8 实验 + 8 对照、随机分组、受出口抖动影响）。
这里的处理臂 n=12 < 20，所以严格表述是：
**「机制已定位 + 生产数据 0%/100% 完全分离（p=1.4e-15），判定为成立；
S1 灰度仍应按原设计跑一遍作为复核」**，而不是「已定案、可跳过灰度」。

**🔴 本次判定修正了一条此前写死的结论。** `registration_handlers.user_register()`
的注释原写「the POST is destructive」，并据此下了「Do not reintroduce it」。
精确版是：**POST 是破坏性的，当且仅当你忽略它的响应**。
「不要重新引入探针」仍然成立 —— 但理由是「它作为存在性判据是无效的」
（168 次探针答 `user_already_exists` **0** 次），**不是**「POST 本身有毒」。

### 2.0.1 由此得到的落地形态：**比原方案小得多**

原方案 §六 估「设密码端点接线：中」。**实际不需要写这个** ——
`registration_mode="password"` **就是** H1 描述的路径，且已端到端实现：

```
authorize
  → passwordless_web = (registration_mode == "passwordless")   registration_handlers.py:790
      ⇒ mode="password" 时走 _continue_signup_username()        auth_flow.py:656
        （POST /api/accounts/authorize/continue 带 username，
          把事务推进到密码步 —— 等价于探针那句 GET /create-account/password）
  → user_register(): mode != "passwordless" ⇒ _post_user_register()   POST 带密码
  → send_email_otp(): else 分支 ⇒ _email_otp_send_url(reg_data)
                                ⇒ _follow_continue_url(...)        **跟响应**
```

⇒ **S1 灰度的开关是一个已存在的配置值**：`email_registration.registration_mode`
（`registration_state.py:225-233`，缺失即 `passwordless`；`runtime.json` 当前**没有**该键）。

**仍然需要单独拍板的两个问题**（未做，别默认决定）：

1. ✅ **密码步失败时 fallback 还是 abort？→ 已拍板：abort，且已落地（2026-09-16）。**
   aBai 也选 abort（`if not password_registered: raise`，宁可不注册也不产生无密码账号）。

   落地形态：`invalid_auth_step` + 事务停在 `email-verification`
   ⇒ `password_step_unconfirmed:<code>`，**在发码之前**停手（不烧邮箱 OTP）。
   归类 `auth_state`（`retryable` + `batch_retry`，**不** `batch_dropped` —— 地址未被消费，
   不该被拉黑）。门禁 `_password_lane_active()` 是**单一 owner**。

   ⚠️ **代价要认**：灰度实验臂会**丢掉地址**（这批地址这一轮拿不到账号）。
   换来的是 S2 的验收判据「0 个无密码账号产生」**真能达到** —— 选 fallback 则达不到。

   ✅ **意外收获：这次止损折叠掉了一条不可达的恢复分支。**
   原 `else` 之后有 `print("...resuming OTP step...")` + `s.resume_email_verification = True`，
   但入口门禁为假即 `return`、而中间不写判据依赖的字段 ⇒ 下游再判同一谓词的 `if` **恒真**、
   该恢复分支**恒不可达**。删除的三条独立理由：
   ① 不可达；② `resume_email_verification` 只改发码端点、**不跳过** `create_account`
   ⇒ 恢复下去照样建无密码账号；③ 即使不建号，对已存在账号也只会落 `/about-you`
   （NextAuth session cookie **从不下发**，09-14 实测 5/5）⇒ 白烧一个 OTP。
   零成本佐证：全量留存日志 **5919 个**文件里 `resuming OTP step` **0 次命中**。

   ✅ **副作用已结案（2026-09-16 收尾，老板拍板「删字段」）**：`resume_email_verification`
   一度**失去唯一写入点**（先按 `phone_result` 先例登记进 `READ_BUT_NEVER_WRITTEN`），
   现已**整体删除** —— 状态字段、两处读点、`_email_otp_send_url` 的回落分支，
   以及只被那个回落分支消费的 `auth_base` 参数。白名单只剩 `phone_result` 一条。
   契约随之升级为「缺 `continue_url` 必须报 `email_otp_send_missing_continue_url`，
   **不许猜端点**」。落地细节见 `scan-2026-09-16-protocol-registration-optimization.md` §六 #10。

   **验证**：`tests/test_user_register_response_contract.py` **27 例**（#8 扩到 28，#10 收窄到 27）；
   变异 `mutations_contract_lock` **19/19 承重**（#10 新增 MI「字段加回来」/ MJ「回落加回来」）。

2. **同批 A/B 怎么分？** `registration_mode` 是**批次级**参数
   （`batch_runner` 整批传入），做不了「同批 8 实验 + 8 对照」。
   要么加一个按 `account_proxy_index` 奇偶分流的运行时开关，
   要么放弃同批对照、改用**逐批交替**（弱一些，会被出口漂移污染）。
   ⇒ **仍未拍板**。

### 2.1 为什么必须先验它（**保留，用于说明判定为何不是空穴来风**）

09-15 的实测（批次 28260，1:1 相关、无例外）：

```
探针 POST user/register → http_200 89 次  ⇒  随后 email-otp/validate 409 85 次
探针 POST user/register → http_400 77 次  ⇒  随后 email-otp/validate 200 75 次
```

当时的结论是「该 POST 本身破坏事务」。但**我们当时没有采纳响应**：拿到 200
（body = `{"continue_url": ".../api/accounts/email-otp/send", "page": {"type": "email_otp_send"}}`）
之后仍然按 passwordless 假定继续，于是后续 `validate` 全跑在**探针之前**的
session/CSRF 上 ⇒ 409 是**我们没跟着响应走**的结果，不一定是 POST 本身的罪。

aBai 的注释也把因果说得更窄（`abai_protocol_register.py`）：

> Calling `email-otp/send` **again** invalidates the transaction and validation
> fails with `invalid_state`.

它归因于**再次调用发码端点**，而不是 `user/register` 本身。这与我们的证据一致
（探针之外的 89 次 200 里，破坏点也可能是后续重复发码，而不是 POST）。

⇒ **H1 不成立才应该结案；不能拿「探针已删」的结论去否决这个方向。**

### 2.2 H1 成立/不成立的判据（**预登记，已按此判**）

| 结果 | 判据（可查） | 结论 |
|---|---|---|
| **成立** | 采纳响应路径下 `email-otp/validate` 的 409 率 **< 10%**（对照：未采纳响应的历史值 ≈ 95%） | 立项落地密码优先 |
| **不成立** | 采纳响应路径下 409 率 **≈ 未采纳**（无显著差异） | 正式结案，写入 `runtime-and-registration.md` |
| **不可判** | 样本 < 20 或出口抖动导致 403/429 混入 | 扩样本重跑，不得下结论 |

**实测落点**：采纳响应组 **0.0%**（0/12）、未采纳组 **100.0%**（85/85）、
对照（未 POST）**0.3%**（1/360）⇒ 落在「**成立**」行，且远优于 10% 阈值。
无 403/429 混入（这些 run 的 validate 行只有 200/409）。
唯一保留项：处理臂 n=12 < 20 ⇒ **S1 灰度按原设计复核**（见 §2.0 的诚实说明）。

🔴 **必须带对照组**：同批次内随机一半走「采纳响应」，一半走「不采纳（沿用旧行为）」。
只跑实验组会把「出口变好」误读成「H1 成立」。
—— 本次判定用的是**历史天然对照**（同一 POST、同一 payload、唯一差别是跟不跟响应），
不是随机分组，所以它**能定性**但不能替代前瞻性复核。

## 三、实验设计（不改生产默认行为）

### 3.1 开关

新增 `registration.password_first`（`registration` 段**无键白名单** ⇒ 加键安全）。
**默认 `false`**，与 `registration.obtain_refresh_token` 同款做法。

🔴 验证开关时必须设成**非默认值**（`true`）—— 否则「没接线」与「已生效」结果一样，
是本项目已有的教训（`code-contracts.md`）。

### 3.2 分阶段

| 阶段 | 范围 | 通过判据 |
|---|---|---|
| S0 离线 | 单测覆盖「采纳响应」的状态机：`page_type`/`continue_url` 重算、`otp_already_dispatched` 判定、设密码失败即 abort | 变异验证每条判据都承重 |
| S1 灰度 | **1 个批次、≤8 个地址、`password_first=true`**，同批留 8 个对照 | 见 §2.2 判据；`validate` 409 率、设密码成功率、`accounts.password` 落库率 |
| S2 小批 | 3 个批次、≤50 地址 | 成功率不低于当前基线；**0 个无密码账号产生** |
| S3 默认 | 改 `runtime.json` 默认值 | 需单独拍板 |

### 3.3 必须采集的指标（每批次）

- `user/register` 的状态码分布（200 / 400 / 其他）
- 设密码端点状态码分布 + 落库的 `accounts.password` 非空率
- `email-otp/validate` 的 409 率（**与对照组逐批对比**）
- 结果里新增一个 `password_registered: true/false` 字段
  ⚠️ 加字段前先看 `_oauth_result_summary` —— 它是 **fail-open**，
  任何分支返回的键都会进日志行与 `finalize` payload。**别把密码或 sentinel 带进去。**

## 四、落地边界（立项后也不做）

| 不做 | 理由 |
|---|---|
| **不重建「注册前置探针」** | `user_register` 的 passwordless 分支已写明 "Do not reintroduce it"，含实测数字 |
| **不改 `registration_mode` 的默认值**（仍 `passwordless`） | 默认值变更需单独拍板；先让开关跑通 |
| **不给存量 965 个无密码账号补密码** | 未登录状态下无法给已存在账号设密码（`create_account` 只对不存在的地址有效）。存量只能按死路止损，见 P1-2 |
| **不做「不采纳响应」的变体** | 那就是已删探针，别绕回来 |

## 五、风险与回滚

| 风险 | 缓解 |
|---|---|
| `user/register` 的 POST 仍会破坏事务（H1 不成立） | 开关默认 `false`；灰度阶段只跑 8 个地址；失败即关开关，零残留 |
| 设密码成功但服务端把它当成「注册完成」，跳过 `/about-you` | 用 S1 的 `accounts.password` + `access_token` 双落库率判定 |
| 把密码写进日志 / `finalize` payload | `_oauth_result_summary` 是 fail-open ⇒ **新增字段必须逐键审查**；密码只落库不进结果 |
| 灰度期间出口抖动污染结论 | 同批次留对照组；403/429 混入时该批作废（判据见 §2.2） |

**回滚**：把 `registration.password_first` 置回 `false`（或删键）即可回到当前行为；
代码路径与现状是**并列分支**，不是替换。

## 六、工作量与优先级（**2026-09-16 按 H1 判定修订**）

原估算把「设密码端点接线」列为**中**。H1 判定后这一项**取消** ——
`registration_mode="password"` 已端到端实现（§2.0.1），
`_post_user_register` + `send_email_otp` 的 else 分支就是「采纳响应」。

| 项 | 原估 | 修订后 | 说明 |
|---|---|---|---|
| 设密码端点接线 | 中 | **取消** | 已有：`_post_user_register()` + `_follow_continue_url`（GET，与响应自带的 `"method": "GET"` 一致） |
| S0 单测（状态机 + 变异验证） | 中 | **小** | 状态机已在生产跑过（C 组 12/12）；补的是**契约锁**，不是新逻辑 |
| 结果字段 + 指标采集 | 小 | 小 | 加 `password_registered` 前先审 `_oauth_result_summary`（fail-open） |
| **拍板两个设计问题**（§2.0.1） | — | **新增，前置** | ✅ fallback vs abort **已拍板（abort）并已落地**；🔴 同批 A/B 怎么分**仍未拍板** |
| S1/S2 灰度（需真实邮箱与出口） | 受外部配额限制 | 同 | 开关已存在，无需新代码 |

**修订后的建议顺序**：
1. ~~先拍板 §2.0.1 的两个设计问题（这是唯一的真实阻塞点）。~~
   ⇒ ✅ **09-16 已拍板第一问（abort）并落地**；🔴 第二问（同批 A/B 怎么分）**仍阻塞 S1 灰度**。
2. ~~补 S0 契约锁：把「`user/register` 的 200 必须被跟、且用响应自带的 `method`」钉成用例~~
   ⇒ ✅ **2026-09-16 已落地**：`tests/test_user_register_response_contract.py`（**27 例**，
   含 09-16 追加的止损段 15 例；13 → 28 → 27，最后一次是 #10 删掉 resume 回落用例）
   + `tests/test_http_utils_pure.py::FollowContinueUrlTests::test_the_follow_verb_is_get_...`（1 例），
   变异 `runtime/tmp/mutations_contract_lock.py` **19/19 承重**。
   全量回归 **4029 passed / 6 skipped / 641 subtests / 0 failed**（基线 3999 → 4014（+15 契约锁）
   → 4030（+16 方案 B）→ 4029（−1，#10 删字段））。
   —— 防止下一个人再写出「POST 完忽略响应」的变体（这正是 B 组 85 个 409 的成因）。
3. 再谈 S1 灰度。

优先级低于 P0/P1-2/P0-3（那四项已落地且零成本），高于 P2-2。

## 六之二、本判定用到的分析脚本

| 脚本 | 用途 |
|---|---|
| `runtime/tmp/h1_lane_crosstab.py` | 泳道 × validate 结果交叉表（**先做了这个，发现 `password_signup` 其实是探针**） |
| `runtime/tmp/h1_adopted_response_runs.py` | 找出「含 `Email OTP send:`」= 采纳响应路径的 run |
| `runtime/tmp/h1_2x2_follow_vs_ignore.py` | **核心**：B/C 两组的 2×2（跟 vs 不跟响应） |

🔴 已删探针的源码在 `/tmp/probe-delete-backup/registration_handlers.py.bak-20260916-035322`
（**注意是 MSYS 的 `/tmp`，不是 `F:\tmp`** —— Python 在 Windows 上解析 `/tmp` 会得到 `F:\tmp`，
Git Bash 不会）。它是本次能排除「带密码 vs 不带密码」这个混杂变量的唯一依据。

## 七、与半注册登录方案的关系

两者是**同一个问题的预防/补救两面**：

- **P1-1（本文）** = 预防：新账号带密码 ⇒ 未来丢会话可零 OTP 恢复。
- **半注册登录方案** = 补救：存量 82 个半注册 + 965 个无密码成功账号，
  实测**不可恢复**（服务端闭环），只能止损。

⇒ **P1-1 不做，存量欠账只会继续增长**；这是本项最主要的立项理由。

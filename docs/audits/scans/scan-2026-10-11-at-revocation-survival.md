# 2026-10-11 新注册账号 AT 数分钟内被撤销:调查与 P1 对照(HEAD `ab6038d` 之上)

> 状态:**测量完成,根因未定**。下列"实测"都有可复算来源;"推断"明确标注。

## 1. 现象(实测)

- 10-11 19:05–19:27 注册的 8 个 Remail iCloud 账号(id 13950–13964),约 19:56 全部 `401 token_revoked`。
  19:16 / 19:31 的桌面检查里 3 个 200、5 个 401,与账号年龄无关。注册时 AT 探测全部 200。
- 同一时间 session cookie 仍有效(`/api/auth/session` 200、free 计划、返回同一个 AT),
  但该 AT 在 `/me`、`accounts/check`、`wham/usage` 上全 401 ⇒ **AT 级别服务端撤销**,不是某个端点的问题。
- JWT 结构新旧一致(240 h、同 client / scopes)。出口 / 指纹 / `oai-did` 与存活无相关。
- 重登 13962:Codex PKCE 通道在第一个 `GET oauth/authorize` 即 403 边缘挑战(换 sid 同样);
  chatgpt 邮箱通道(绕过"无密码"探针)send-otp 200、验证码到达,
  `email-otp/validate` → **403 "account has been deleted or deactivated"** ⇒ 该账号已被**停用**,不只是 AT 被撤。

## 2. P1 对照(实测)

- 方案:Remail iCloud 14 个新地址(唯一幂等键,单价 60,与此前地址零重叠),出口 IN,
  `passwordless` 泳道、显式 send-otp(默认)、**两臂都做 TOTP 注册**,A/C 交替:
  - **A**:仅 2FA。
  - **C**:2FA + `registration_warmup`(`chatgpt_bootstrap.enabled=true`,14 个只读 GET)+ 停留 `18,45` s。
- 注册成功 A 6/7、C 7/7(A 的 1 个失败是 `bootstrap_transport` curl 35,与臂无关)。C 臂预热 14/14 全 200。
- 每个成功账号挂 `scripts/at_survival_watch.py`(1/5/15/30/60 分钟,`HTTP 200`=活,`401`=撤销,`0`=传输错误不计)。

| 检查点 | A(2FA) | C(2FA+预热+停留) |
|---|---|---|
| 1 min | 5/5 | 6/6 |
| 5 min | 3/5 | 4/6 |
| 15 min | 0/4 | 1/5 |
| 30 min | 0/4 | 0/4 |
| 60 min | 0/3(+13966 401) | 0/6(+13976 401) |

(分母只含有结论的探测;`0` 传输错误未计入,13966 / 13976 的短检查点 watcher 没有出数,只补了 60 min 单点。)

**结论(实测)**:
- 1 分钟内全活;5 分钟 7/11;15 分钟 1/9;30 分钟起 0。**撤销发生在 5–15 分钟内,两臂无可分辨差异**。
- 由此 **"加 2FA""加 bootstrap 预热 + 停留"都不是充分的修复**(两臂都有 2FA;C 臂再加预热仍同样死)。
- n 小(各 ≤7),不能排除预热有小幅效应(C 15 分钟有 1 个活,A 为 0),但量级远不足以挽救。

## 3. 没能验证的 / 推断

- 旧账号(09-16/19/24)"可用"的对照**无法复测**:AT 有效期 240 h,今天探测 20/20 旧账号全 401 只是**到期**,不含撤销信息。旧"56/56 可用"只在创建后 0–15 分钟探测,与本次 1 分钟 100% 相符,**不构成对照**。
- 剩余假设(**未验证**):(a)出口 IP 信誉(单一住宅网关 `us.lajiaohttp.net:2000`,IN 出口);(b)iCloud 隐私转发地址本身被风控;(c)注册指纹/行为整体(与 08-29「注册后数小时被批量停用」同型)。
  下一步最便宜的区分变量:**换出口(US / 另一家)**、**换邮箱源(Microsoft)**,各 ≥8 个、同样挂存活曲线。

## 4. 本轮落地(默认全关,不改生产行为)

- `sms_tool/registration_warmup.py`:协议通道注册后预热 + 停留,复用 `registration.chatgpt_bootstrap.{enabled,authenticated}` 与 `post_registration_dwell_seconds_range`(无新增配置键)。
- `sms_tool/session_builder.py`:从分片 `__Secure-next-auth.session-token.N` 回填 `session_token`。
- `scripts/at_survival_watch.py`:只读存活曲线(`--ids --label --schedule-minutes --t0 --out`,`--summary` 汇总)。
- `tests/conftest.py`:`isolated_runtime` 把真实 `runtime/accounts.sqlite3` 重定向到沙箱。此前每次跑测试向生产库
  `environment_ledger` 灌假行(自 10-05 起每天约 200–350 行);修复后全量测试前后四张表行数不变。

## 5. 勘误

- 早先把"新账号 `session_token` 列为空"当作缺陷:这些行由**临时 launcher** 经 `registration.run_batch` 写入,
  只留下 `at_probe_pending` 快照(无最终 upsert,因此也无 `password` / `totp_secret`),CLI 正式批次(如 13947)的行是完整的。
  **launcher 产物,不是生产缺陷**;回填 `session_token` 仍无害,但不是修复手段。
- P1 的 TOTP 注册在日志里显示成功,但 launcher 路径没把密钥存库(同上)。实验账号本身已死,不影响结论。
## 6. 复用已购 iCloud 的全链路复测(10-12,协议注册 → 查优惠)

- 样本:此前买过、DB 里**没有账号行**的 17 个 Remail iCloud 地址(pickup 全部可用,死号登记表 0 命中)。
  临时 retry-guard 副本去掉这些目标,不写生产 guard;出口为新的 rola.vip(IN)池;`--registration-mode passwordless`、生产默认(2FA 开、预热关)、`--check-promotion-after-registration`。
- 注册:**16/17 成功**,1 个 `missing_auth_session_access_token`。CLI 路径的行是完整的(2FA 已入库、`session_token` 已入库、状态 `registered`),印证 §5:此前为空是 launcher 产物。
- 存活曲线(分母为有结论的探测,传输错误 `0` 不计):1 分钟 12/12,5 分钟 15/16,15 分钟 2/12,30 分钟 0/13,60 分钟 0/9。**换成 rola.vip 出口后曲线形状与辣椒网关一致**(5–15 分钟内撤销)。
- 查优惠:批次结束后统一探 16 个,**4/16 有结论**(全是 Free、无优惠,`trial_eligible=0`),**12 个 `AT失效`**。这 12 个不是"没有优惠",是**没能判定**。
- 推断(未单独验证):批次末统一查优惠与 AT 约 5–15 分钟的寿命冲突。17 个账号串行注册约 50 分钟,先注册的早已过期;仅最后几个还活着。
  需要的是**每个账号注册完立刻查**(1 分钟内 12/12 为活)。
- 新增排除项:换出口供应商(lajiao → rola)、换进 CLI 完整落库路径,都**没有**改变撤销曲线 ⇒ 出口供应商本身不是主因(同为 IN 出口,仍未测 US)。
## 7. 落地:查优惠改为每个账号落库后立即查(10-12)

- `sms_tool/commands/registration.py`:`--check-promotion-after-registration` 现在在 `_persist_registration_result_core` 里、该账号 DB 落库后、下一个账号开始注册前立即探测(`probe_promotion_now`,走账号健康通道、`proxy=None`、单账号)。
  结果存进持久化标记(`promotion_report`);探测抛错不影响落库,回落到批末补查。
- 批末 `save_registration_results` 用 `merge_promotion_reports` 合并逐账号报告,**只补查没有即时结果的账号**(延迟 AT 探测、重放落库)。此前批末统一重探会把早先账号覆盖成 `AT失效`。
- 对照参考仓 `myfanhua/turb-gpt-free-register`:它也是落库后立刻入队查询(`AUTO_PLAN_CHECK_AFTER_REGISTER`)。**未移植**它对「Free 且无试用资格」账号 2 秒后复查一次:本仓每次探测都会落库,复查失败会盖掉首次成功结果;也没有数据表明资格会在注册后滞后出现。
- 测试:`tests/test_registration_immediate_promotion.py`(顺序、无开关不探、批末不重探、仅补查缺失、探测抛错回落、延迟 AT 不即时探、合并);`tests/test_account_promotion.py` 的一条断言从整体相等改为核心计数相等。
- **未做**:新代码尚无线上实测(iCloud 库存为 0)。预期:每账号注册后约 1 分钟内探测,AT 此时应为活。
## 8. 重登 AT 失效的 iCloud 账号(10-12,生产路径 auto 模式)

- 范围:id>=13950 的 37 个 iCloud 账号(P1 launcher 14 个 + 13950–13964 + 复用批 16 个),`refresh_local_quota_statuses(relogin_on_401=True, relogin_mode="auto", workers=2)`,出口为 rola.vip(IN)。
- 结果:**0/37 恢复**(中止前逐一复测:36 个 401,1 个传输错误)。批次约 30 分钟后手动中止:邮件 OTP 失败后回落到浏览器通道(下载 camoufox/GeoLite2),整体过慢。
- 观察到的失败形态(实测):
  1. Codex PKCE 通道:首个 `GET oauth/authorize` 即 403 边缘挑战(单账号复测 14037 同样,`session_circuit_open ... retry_after=899s`)。
  2. 邮件 OTP 通道:`send-otp` 200、验证码到达,`email-otp/validate` → **401 `login_failed`**(泛化错误,不是 13962 那种明确的 403 停用);随后 resend 返回 200,但邮箱里**没有再来新邮件**。
  3. 部分账号(如 13950、13956、13960)在重登窗口内邮箱没有任何新邮件,说明根本没走到发码。
- 推断(未验证):这些账号的**登录本身被服务端拒绝**(与 AT 被撤、账号受限同源),不是重登实现的问题;13962 的 403 `deleted or deactivated` 是同一现象的明示版本。若要确认,需要对一个**刚注册 1–2 分钟内**的账号做重登,看 AT 撤销之前登录是否被允许。
- 已购且无账号行的 iCloud 地址只剩 1 个,服务端判定已注册,被注册前置过滤跳过;**可复用的未注册库存已耗尽**。Remail iCloud 现货 443,单价 60。
## 9. 逐账号立即查优惠的线上验证(10-12,新买 5 个 iCloud)

- 样本:新买 5 个 Remail iCloud(300,唯一幂等键,与 DB 零重叠),出口 rola.vip(IN),passwordless 泳道,生产默认(2FA 开、预热关),`--check-promotion-after-registration`(`fbaf1f7` 之后的逐账号立即查)。
- 注册:**5/5 成功**。
- 查优惠:**5/5 有结论**(全部 `Free · 无优惠`,`trial_eligible=0`)。对比此前批末统一查:4/16 有结论、12 个 `AT失效`。**即时查把"没能判定"变成了"有结论"**。
- 累计有结论的账号(上批 4 + 本批 5)共 9 个,试用资格为 0:**9/9 均为 Free、无优惠**(推断:iCloud 隐私转发新号目前没有 Plus 试用/折扣资格,样本小,未与其他邮箱源对照)。
- AT 存活曲线(本批 5 个):1 分钟 5/5,5 分钟 4/5,15 分钟 0/5,30 分钟 0/5;REUSE 标签累计(含上批)1 分钟 17/17、5 分钟 19/21、15 分钟 2/17、30 分钟 0/18。**曲线形状不变**,即时查只是赶在撤销之前拿到结论,没有延长 AT 寿命。
## 10. 参考项目协议对齐 + SDK 同步(10-12)

- 对照:参考项目 `SENTINEL_SV=20260810913b`(sdk.js 33820 B),本仓此前 `20260219f9f6`。两个版本线上都还在服务。
- **实测发现(线上,rola IN)**:服务端 challenge 对 `authorize_continue` / `oauth_create_account` 报 `so.required=true`,旧 runner 的 `so` 长度为 0,即**旧栈从未发送 `openai-sentinel-so-token`**。新 runner 调 `sessionObserverToken`,`authorize_continue` SO 约 3272 字符、`oauth_create_account` 约 3162 字符;`username_password_create` 的 challenge 不要求 SO。推断(未验证):缺 SO 是否与 AT 撤销有关,需要新建号对照。
- 落地(提交 `2b5a937`、`343f541`):
  - Sentinel 同步到 `20260810913b`,runner 取参考版并保留本仓 Firefox 画像分支;`_so` 由 `issue_sentinel_token` 剥离后包成 SO 头。
  - 新增 `registration.reference_lane`(默认关):按参考 `main.py` 顺序跑密码泳道,强制打开所需的既有开关(覆盖配置里显式的 `false`),并补上此前没有的两段——登录页匿名预热(accounts/check、CES、me、定价)+ `providers`,以及密码页 Sentinel iframe 包(`frame.html` + 三个 flow 共用一个 `p`)。`user/register` 现在也会带 SO(challenge 要求时)。
  - `chatgpt` 族请求的 `oai-client-build-number/version` 从数月前的值更新为线上页面当前值。
  - 全量 pytest 通过(含各类棘轮)。
- 复跑(上轮 5 个 iCloud 别名,`skip_registered_mailboxes=false`,password 模式 + lane):**没有产生新注册**。这 5 个地址在上一轮已全部注册成功(DB id 14040–14052,`registered`),所以没有变量可测。第 1 个账号走完 lane 全部前置阶段(匿名预热 4×200、providers 200、signin 带 `login_or_signup`+`prompt=login`、`/create-account/password` 落点 200),`user/register` 返回 400 `invalid_auth_step`,`redirect_uri=.../auth/login_with`,即服务端把已存在账号送去登录。第 2 个账号在邮箱快照阶段挂住 8 分钟,已手动终止。
- 结论:lane 的**请求序列在线上可达密码页**;"新建号后 AT 是否更长命"**仍未验证**,需要 5 个未注册的新别名(Remail iCloud 现货 443,单价 60)。
## 11. reference lane 线上全链路 + AT 存活(10-12,新买 5 个 iCloud)

- 样本:新买 5 个 Remail iCloud(300,唯一幂等键,与 DB 零重叠),出口 rola.vip(IN),`--registration-mode password` + `registration.reference_lane`(仅内存开启)、生产默认 2FA、`--check-promotion-after-registration`。
- 注册结果(**实测**):**1/5 成功**(DB id 14055)。
  - 账号 1:第一次启动的进程在 `[4-Trigger email OTP]` 前后被我自己的 shell 回收杀掉(`user/register` 已 200),重启后同地址 `invalid_auth_step`(服务端已把它当已存在账号送去登录)。这是**操作失误,不是 lane 缺陷**,不计入 lane 成功率分母时为 1/4。
  - 账号 3/4/5:`user/register` 200 → `Email OTP send` 200,但 `client_auth_session` 仍挂着 `passwordless_email_otp_send_pending=true`、`email_verification_mode=passwordless_signup`,判 `email_otp_send_stuck`(未进入邮箱轮询)。对这三个地址重试(`lane_icloud_20261012_retry.txt`)全部 `invalid_auth_step`,**地址已被首次尝试消耗**。
  - 账号 2:`after_signup_state` 时 pending 标记已清除,走完 OTP 校验 → create_account → 落库。
- lane 前置阶段(匿名预热 4×200、providers 200、`login_or_signup`+`prompt=login`、`/create-account/password` 200、`user/register` 200 带 SO)在新地址上全部走通;此前"password 页后 400"只是复用地址造成的。
- 对比:passwordless 泳道同一批货源此前 5/5 注册成功。**lane 的注册成功率明显更低(1/4),但样本极小。**
- 推断(未验证):`login_or_signup` 的 authorize 本身会让服务端先挂一个 passwordless 派发;派发是否在 `user/register` 之前落完决定了 OTP 是否可达。账号 2 的 pending 在 `after_signup_state` 已清除,3/4/5 没有。可能是时序竞争(参考项目是否在此处等待/轮询未核),需要单变量实验确认。
- 即时查优惠:1/1 有结论,`Free · 无优惠`(`trial_eligible=0`);累计有结论 10/10 均为 Free、无优惠。
- **AT 存活(实测,n=1)**:账号 14055 在 1 / 5 / 15 / 30 / 60 分钟探测均 `active`(HTTP 200)。旧栈同口径:5 分钟 4/5,15 分钟 0/5,30 分钟 0/N(passwordless,无 SO,旧 SDK)。
- 解读:**单个账号 60 分钟仍活是首次出现的反例,与旧曲线(5–15 分钟撤销)方向相反**,但 n=1,不能下结论。可能的原因(均未隔离):新 SDK、`user/register` 与 authorize_continue/create_account 的 SO token、lane 的匿名预热与 password 页 Sentinel bundle、password 臂本身(而非 passwordless 臂)。需要在能稳定注册的前提下积累样本再对照。
- 下一步:(1)先解决 lane 下 OTP 派发 pending 的竞争(例如在 `user/register` 后/OTP send 前等待并重读 dump),把 lane 成功率拉到可用;(2)再用 ≥5 个新地址重复存活测量;(3)若存活优势成立,拆分变量(SDK/SO vs 预热 vs password 臂)。

## 12. 修复 lane 的 OTP pending 误报 + 存活对照(10-12,新 SDK/SO)

### 12.1 pending 不是「码不会来」(实测)
- §11 里 3 个被 `email_otp_send_stuck` 止损的地址(me/pi/el),**邮箱里都收到了 ChatGPT 验证码邮件**(04:41–04:42,与各自尝试时间吻合)。止损时 `user/register` 已 200,地址因此被白白消耗。
- 参考项目 `main.py` 发完 `email-otp/send` 后**不读该标记**,直接轮询邮箱。该止损判据是在 passwordless 泳道上标定的(2026-09-16:6/6 超时都带 pending),不适用于 lane。
- 修复(`b6b8408`):`reference_lane` 下 pending 只打 `Email OTP send still shows pending; reference lane polls the mailbox anyway`,照常轮询;其它泳道行为不变。没有加「等待并重读 dump」:证据显示码已经发出,等待既无必要,也会拉长已 `user/register` 的窗口。
- 线上验证(lane6,5 个新地址):出现该标记的账号 **全部**拿到码并注册完成(4/4);注册 **4/5**,失败 1 个是 `create_account` 三次 `curl 28`(出口超时 20 s)后换代理重试,地址已被消耗,重试必然 `invalid_auth_step`。这是**出口超时**,不是 lane 逻辑。

### 12.2 存活曲线(实测,探测按创建时刻起算)
| 臂 | 栈 | 注册 | 1m | 5m | 15m | 30m | 60m |
|---|---|---|---|---|---|---|---|
| 旧栈(§6/§9) | passwordless,旧 SDK,无 SO | 21/22 | 17/17 | 19/21 | 2/17 | 0/18 | 0/9 |
| LANE5(§11) | lane(password)+新 SDK/SO | 1/5(pending 误止损) | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| LANE6 | lane(password)+新 SDK/SO+预热 | 4/5 | 3/3 | 4/4 | 4/4 | 4/4 | 4/4 |
| ARMD | **passwordless**+新 SDK/SO,无预热 | 4/4 | 4/4 | 4/4 | 3/3 | 4/4 | 4/4 |

- 晚探测(创建后 61–121 分钟,另有 14055 在 720 分钟):除 14058 一次传输错误(`unknown`,非 401)外 **8/8 仍 200**。
- 对比旧栈同口径:15 分钟 2/17、30 分钟 0/18、60 分钟 0/9。新栈 9 个账号在 60 分钟全部存活(含 14055 在 12 小时仍存活)。**曲线形状变了,不再是 5–15 分钟撤销。**
- 即时查优惠:新栈 9 个有结论账号全部 `Free · 无优惠`,与旧栈一致(累计 18/18),**优惠资格未因此改变**。

### 12.3 变量拆分(尚未完全隔离)
- **password 臂不是原因**:ARMD(passwordless)与 LANE6(password)都存活。
- **预热/匿名 bootstrap/password 页 bundle 不是必要条件**:ARMD 没有这些(`reference_lane=false`,预热关)仍存活。
- **ARME = passwordless + 新 SDK 但在内存里去掉 SO 头**:`create_account` 5/5 `registration_disallowed`(10 个 SO token 被剥掉)。也就是说**新 SDK 下 SO 是必需的**,这条臂没法产出可测账号;5 个别名(300)被消耗,**无存活数据**。
- 因此「SDK」与「SO」目前只能作为**一对**与旧栈比较:旧栈(旧 SDK,从不发 SO)15 分钟内被撤销,新栈(新 SDK + SO)60 分钟不撤销。单独归因需要「旧 SDK + SO」的臂,而旧 runner 不产出 SO,需要先实现才能测。
- 推断(未验证):旧栈缺 SO 使会话被风控标记,AT 在 5–15 分钟被撤销;补上 SO 后风控信号恢复正常。也可能是新 SDK 的其它指纹字段。n=9,单一出口供应商(rola IN),单一邮箱源(iCloud 隐私转发),**不排除**出口/时间窗口的混杂,需要日后同批复测。

### 12.4 成本与副作用
- 本节购买:LANE6 5 个(300)、ARMD 4 个(240,库存不足只买到 4)、ARME 5 个(300),合计 840;可用注册:LANE6 4 + ARMD 4。LANE5 的 3 个 pending 地址与 ARME 的 5 个地址已被消耗,不可再用。
- 已知缺口:`user/register` 200 之后任何失败(OTP 误止损、`create_account` 超时)都会让同地址变成 `invalid_auth_step`,**换代理重试在该阶段无意义**。重试策略应区分 `user/register` 之前/之后(未改)。

## 13. 401 重登:参考项目策略、本项目失败原因、实测(10-12)

### 13.1 参考项目(`core/account_liveness.py` / `account_export.py`)
- 本地有 AT 且**无 TOTP**:用旧 AT 做 `authenticated_bootstrap` 预热 → `signin/openai?connection=password&reauth=password&max_age=0&login_hint=…`
  → `GET authorize` → 服务端自动发邮箱 OTP → `email-otp/validate`(无 Sentinel)→ 跟随 `continue_url` → `/api/auth/session`。
- 有 TOTP 或无 AT:完整 web 流(预热 → providers → csrf → signin → authorize → `password/verify`(Sentinel+SO)→ `mfa/issue_challenge` + `mfa/verify`(无 Sentinel)→ callback → session)。
- 参考项目的账号全是**密码注册**,所以 reauth/password 两条路都成立。网络失败回落到干净 session 的完整流程。

### 13.2 本项目的重登路径与两个独立问题
- `auto` 链:refresh_token → web_session cookie 重放 → `chatgpt_email_otp` → codex PKCE → browser。passwordless 注册的账号没有密码也没有 RT,实际只剩 `chatgpt_email_otp`。
- **问题 A(本项目缺陷,已修)**:`_existing_login_probe` 在无密码时遇到 `password_step=True` 就直接 `existing_login_password_required`,**不发 OTP**。
  该探针对 passwordless 账号恒为 True(`/log-in/password` 兄弟页面对任何地址都带表单)。
  对照(**实测**):存活账号 14073 绕开探针后 OTP send 200 → validate 200 → `mfa/issue_challenge` 200 → `mfa/verify` 200 → session 200 → 新 AT 可用。
  即探针把一条可用链路在第一步就挡掉。修复:无密码且 `allow_passwordless=True` 时不拒绝,继续走邮箱 OTP;注册 lane 的两个调用方仍传 `allow_passwordless=False`,保持原拒绝。
- **问题 B(账号状态,不是代码缺陷)**:AT 401 的账号绕开探针后,OTP validate 与 `issue_challenge` 都是 200,**只有 `mfa/verify` 返回 403 `account_deactivated`**
  ("You do not have an account because it has been deleted or deactivated")。此前这个 403 的响应体被丢弃,只剩 `existing_login_totp_verify_failed:403`,
  所以看起来像"重登一直失败",其实是账号已被服务端停用。修复:把 `account_deactivated` 保留进错误串,恢复链据此落终态(不再重复尝试)。

### 13.3 对近期 AT-401 iCloud 账号的重登测试(**实测**)
- 目标:DEADSCAN 中 43 个 401 账号(id 13947–14052),使用新 SDK + SO,`chatgpt_email_otp`,默认探针(修复后)。
- 结果:**0/43 恢复**。
  - 28 个 `account_deactivated`(含单账号实验 14052/14049/14046/14043/14040/14013;其余在批处理中由 `mfa/verify` 或更早步骤返回 deactivated)。
  - 12 个 `existing_login_totp_secret_missing`(13966–13990 偶数号,launcher 写入的行没有 `totp_secret`,走到了 MFA 挑战但无法完成;**无法判定是否已停用**)。
  - 2 个 `auth_session_missing_access_token`(13947、13972,登录完成但 session 无 AT)。
  - 1 个 `existing_login_password_verify_failed:403`(13954,有密码)。
- 对照:存活账号 14073 重登成功(见上)。

### 13.4 对 §6/§9/§12 的修正
- 旧栈"AT 5–15 分钟被撤销"的解释应改为:**账号被服务端停用**(deactivated),AT 随之 401;不是单纯的 token 撤销,所以重登/刷新必然失败,
  之前 0/37 的重登失败同理(当时探针与响应体丢弃都让原因不可见)。
- 新栈账号 60 分钟/12 小时仍存活,与"新栈账号没有被停用"一致。
- **推断(未验证)**:停用发生在旧栈缺 SO/旧 SDK 的会话被风控之后;DEADSCAN 的停用时间点无法从现有数据还原。样本:单一出口、单一邮箱源。

### 13.5 变更与测试
- `sms_tool/auth_flow/login.py`:无密码 + `allow_passwordless=True` 时跳过 `existing_login_password_required`。
- `sms_tool/auth_flow/totp.py`:`mfa/verify` 非 200 且响应含 `account_deactivated` 时错误串追加 `:account_deactivated`。
- 测试:`tests/test_existing_login_totp_deactivated.py`(4)、`tests/test_existing_login_password_probe.py`(+1、3 处显式 `allow_passwordless=False`)。全量 6403 passed。
- 副作用:批处理经恢复链把可判定的账号写成 `account_deactivated`(产品既有行为)。

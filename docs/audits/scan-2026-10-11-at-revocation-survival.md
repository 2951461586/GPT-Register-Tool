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
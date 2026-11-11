# v2026.11.11

自 v2026.11.02 以来的收口：协议注册的**每账号 TLS 指纹**、指纹档位与平台对齐、
账号恢复链的三个假成功，以及 ReMail 取件与批量下单的两处实测缺陷。

## 新增功能

- **协议注册每个账号一套 TLS 指纹** —— 此前 4 个 Firefox profile 共用同一个
  `ja3_hash`（占指纹池一半权重），一个值被拉黑整批同时死。现在按账号的 device id
  打乱其自身扩展顺序，同账号固定、跨账号必不同；Chrome 档位保留自带的逐握手随机，
  HTTP/2 指纹不动。配置项 `registration.tls_fingerprint`（默认开）。
- **指纹档位随平台走** —— iOS 档位不再报 `2560x1080` 桌面屏 + 触点 5 的组合，
  macOS 不再套用 Windows 屏表；新增一致性校验，`--doctor` 会给出矛盾项。
- **Sentinel 启动自检** —— `--doctor` 新增 `sentinel_runtime`：bundle 哈希、Node 版本
  与一次**离线**铸 token 三项一起过。此前 runner 能加载但铸不出 token 时，要到
  `create_account` 才炸，邮箱和验证码已经花掉。
- **UPI 通道记住账号身份与出口结论** —— 同一账号的重试不再换设备、换账单姓名和地址；
  0 元可用性、出口风险在重试与后续运行之间被记住，不再每次重新踩。
- **动态代理生成的结果进入审计** —— `used` / `count` / `reason` 随账号结果落盘，
  「开关开了但一条也没生成」与「开关没开」不再长得一样。

## 修复问题

- **账号「优惠」列把促销线索当成就绪** —— `accounts/check` 的线索被直接当成
  「可试用Plus」。实测 12 个「可试用」账号用 9 个 IN 出口 + 3 个 US 出口重探，
  全部 `not_eligible`。现在只有 coupon 接口的肯定答复才置为可试用，其余记录为线索。
- **恢复链把「铸出过期 token」当成功** —— 会话 cookie 已死时重放仍返回 200，附带的
  access token 是 35～50 天前就已过期的。恢复链因此报「刷新成功」，跳过了本可生效的
  后续手段。现在 `exp` 不在未来（60s 容差）即判失败为 `session_cookie_expired`；
  读不出 `exp` 的 token 仍给通融。
- **带密码的老号登录卡在多因素挑战** —— 登录走到 `/mfa-challenge` 后落到 chatgpt.com，
  `/api/auth/session` 回 200 但没有 access token，界面报
  `auth_session_missing_access_token`，而 TOTP 步骤被静默跳过。现在挑战地址被带进
  登录状态、factor id 从挑战页取得，TOTP 真正完成。
- **轻探活把「跳过的深探」写成账号错误** —— 只做轻探活时，内部跳过标记
  `deep_probe_disabled` 被写进账号的 error 列，健康账号在列表里长期显示「失败」。
  现在视为「无结论」，且账号恢复后自愈。
- **UPI 主 flow 铸不出 token 时直接下单被拒** —— 落到备用 flow 的 token 会被风控在
  紧接着的 Checkout 上拒绝（`400` "unusual activity"）。现在只在即将使用备用 token
  的那条路径上再尝试一次主 flow，仍为空才用备用。
- **ReMail 取件轮询恒定降速** —— 轮询把取件状态与 `succeeded` 比较，而接口发的是
  `normal`，于是每次轮询都进「未就绪」分支，1 秒的轮询间隔被恒定拖到 2 秒。现在
  `normal` 视为健康，`abnormal` 才降速并打出服务端错误摘要。
- **ReMail 批量下单不重试** —— 只有单笔下单会重试，批量路径遇到网关 502 直接中断整批。
  现在两条路都带同一个幂等键重试；批量子项的
  `insufficient_balance` / `insufficient_inventory` / `idempotency_conflict` /
  `temporarily_unavailable` 也分别给出不同的失败原因，而不是统一一句「没有可用邮箱」。

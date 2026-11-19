# FAQ / 已知限制

反复出现的疑问集中在这里，一条一个 issue。每条都给出**实测数据或上游契约**作为依据，
能自己验证的命令也写出来——没有依据的"应该"不进这份文档。

## 1. 新账号没有 Plus 试用 / 优惠

**现象**：注册成功，账号页 `promotion_status=Free·无优惠`，`--check-promotion` 的
`plus_trial_eligible=false`。

**实测**（2026-11-19）：ReMail iCloud 邮箱 10 个（两批 5+5），US 住宅出口池 10 个、每账号
独立出口 → 注册 **10/10 `at_http_200`**，`ReMail quality: deactivated=0/10`；
`--check-promotion` = `success 10/10`、**`trial_eligible=0`**、`coupon_state=not_eligible`。

**结论**：这是上游按来源/地区放量的策略，不是指纹或代码可修项。2026-09 还能看到 10–20 出 1，
10 月之后基本为 0。把它当正常结果；要跟踪就记录 `promotion_status` 随时间的变化，不要靠改
UA/时区去刷。

## 2. iCloud 邮箱收不到验证码 / "秒封"

两个真实原因，都不是玄学：

1. **ReMail 换域名**：`remail.aishop6.com` 于 **2026-11-16 作废**（DoH 现在返回 NXDOMAIN），
   仍指向旧域名的部署会在取码阶段失败/超时。配置项 `email_registration.remail.base_url`
   必须是 `https://remail.best`。
2. **iCloud 订单的 `serviceToken` 只在订单详情里给**：`POST /v1/open/orders` 的响应没有它，
   `GET /v1/open/orders/{orderNo}` 才有，而 `/v1/pickup?email&token` 需要它。当前版本会回读订单
   详情补齐；旧版本直接判"无法取码"，积分扣了、号也没注册成。

**怎么办**：升级到含上述修复的提交后重试：

```bash
python -m sms_tool --count 5 --workers 3 --registration-driver protocol \
  --buy-remail-mailbox --remail-email-suffix icloud.com
```

自检域名：`curl -sS -o /dev/null -w '%{http_code}\n' https://remail.best/docs` 应为 `200`
（同样的 curl 打旧域名会失败）。

## 3. 注册后的账号没有密码

**原因**：默认 **passwordless** 模式与浏览器流程一致，本来就不设密码。只有
`--registration-mode password`（桌面端「注册」页的注册模式）才会设置，并写入
`accounts.password` 与 session JSON 的 `extra_json.registration_password`。

**怎么办**：需要密码就显式选 password 模式。已注册的 passwordless 账号**不能事后补密码**，
要走改密/换绑流程。

## 4. 2FA 登记了，但找不到 / 想复制 TOTP

**现状**：2FA 默认登记（日志 `9-Enroll TOTP`，`--no-2fa` 关闭），secret 存在
`accounts.totp_secret`。桌面端账号表只有 2FA **状态**列（`TwoFactorStatus`），暂无查看/复制
入口（issue #14 已标 `enhancement`）。

**取法**：

```bash
sqlite3 runtime/accounts.sqlite3 \
  "select email, password, totp_secret from accounts where email='x@icloud.com'"
python -c "import pyotp; print(pyotp.TOTP('<totp_secret>').now())"
```

## 5. 提链报 `Our systems have detected unusual activity`

**原因**：这是 **checkout 侧风控**，不是注册失败。它取决于账号 AT 是否有效、出口质量、
支付方法与出口国家是否匹配。

**怎么办**：换出口或冷却后重试；用 `--payment-method-proxy` / `--promotion-proxy` 把 Checkout
与 Approve 的出口分开；看结果里的 `error_stage`（Checkout / Promotion / PM / Confirm / Poll /
Provider Redirect）与 `retryable` 决定"重试还是换方法"。更多见
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md)。

## 6. 导出的 `account-cpa-*.json` 怎么导回本地

**用 `--import-account-json`**（可重复传入多个文件）：

```bash
python -m sms_tool --import-account-json runtime/account_json/account-cpa-20261119_101500.json
```

它把每个账号写成 `sessions/session_<邮箱>_<时间戳>.json` 并 upsert 进 `runtime/accounts.sqlite3`，
**全程离线**（不请求 OpenAI/CPA/SUB2API）。三条规则值得知道：

- **新 token 不会被旧文件覆盖**：按 JWT `exp` 比较，同邮箱保留更晚的一份（文件内去重 + 与库里现有 token 比较）；
- **合成的 `id_token` 不会入库**：导出里标了 `id_token_synthetic` 的会被丢掉，避免把伪造 JWT 存成凭证；
- **单个账号坏掉不中断整批**：跳过的项带稳定原因（`missing_email` / `missing_token` / `disabled` /
  `older_token` / `newer_token_in_store`），失败项单独列出；有失败时退出码为 3。

注意 `--import-cpa` 方向相反：它把本地 AT-only session 推到 CPA/SUB2API，不是读导出文件。

**文件位置与格式**：`runtime/account_json/account-cpa-<时间戳>.json`，内容是
`session_converter` 的 CPA 输出（`email` / `access_token` / `refresh_token` /
`session_token` / `id_token` / `plan_type` / `expired`），单账号是对象，多账号是数组。

## 7. 注册后 10 分钟就被封

看三件事，按顺序排查：

1. **邮箱来源质量**：同批 iCloud 里混入已被用过的号；
2. **出口质量**：住宅 vs 机房、ASN 是否被标记、出口国家与账号资料是否一致；
3. **出口复用**：每账号一条出口亲和，多号共用一个 IP 会连坐。

2026-11-19 的实测（10 个账号、US 住宅、每号独立出口）两小时内无掉号，说明链路本身可用；
出现秒封先查上面三项，而不是先改指纹。

## 8. Sentinel 校验被拦 / 版本对不上

- SDK 版本必须与内置构建一致：`email_registration.sentinel_version` 默认 `20260810913b`。
  这个字符串同时决定 `frame.html?sv=`、Sentinel `Referer` 和证明里的 `p[5]`，填错就是
  "执行 8 月的字节、声称 2 月的版本"。
- 需要跟上游轮换时打开 `email_registration.sentinel_sdk_auto_update`（默认关）：会按配置版本
  下载、校验（状态码 / 5 MiB / `SentinelSDK` 标记 / 非 HTML）并缓存到
  `runtime/sentinel-sdk/<version>/`。
- 传输层是 `curl_cffi 0.17.0`（可用 `PROTOCOL_HTTP_CLIENT=wreq` 切换）。先跑
  `python -m sms_tool --doctor`：它会校验安装版本、指纹档位和 Sentinel 自检。

# GPT-Register-Tool

通过邮箱 OTP 完成 ChatGPT 注册，注册成功后访问 `https://chatgpt.com/api/auth/session` 获取 `accessToken`，再调用 `sms_tool/gen_pp_link.py` 生成 ChatGPT Plus PayPal 支付链接，并把结果保存到 session JSON。

## 环境准备

```bash
pip install curl_cffi cloakbrowser requests
```

## 一键注册链路和错误边界

桌面端“一键注册+支付链接”按明确边界执行：

1. 选中邮箱行时，只注册当前选中邮箱，并写入无 BOM UTF-8 临时单行文件。
2. 没有可用选中行时，才读取 `hotmail.txt` / Chatai 邮箱池并使用 UI 中的 count。
3. 显式传入 `--chatai-mailbox-file` 或 `--mailbox-file` 但解析不到邮箱时，CLI 直接 `EXIT 2`，不会 fallback 创建 LuckMail 新邮箱。
4. 注册链路里的 TLS、代理、超时等瞬时传输错误由 `sms_tool/http_client.py` 重试；最终失败会写入 SQLite 为结构化失败账号，而不是 traceback 崩掉 WPF 批次。
5. 注册成功但 PayPal 链接没有生成时，账号仍保存 session，SQLite 标记 `status=paypal_failed`，CLI 以 `EXIT 3` 结束，WPF 批次显示失败。

可调参数：

```json
"timeouts": {
  "request": 20,
  "http_retries": 3,
  "retry_delay": 2,
  "token_cache_ttl": 300
}
```

更多职责边界见 `docs/architecture.md`。

CloakBrowser 首次运行时会自动下载 Chromium 二进制文件（约200MB），无需手动安装。

## 项目结构

```text
chatgpt_phone_reg.py      # 兼容入口，调用 sms_tool.cli
sms_tool/
  cli.py                  # 命令行参数和输出保存
  config.py               # config.json 加载
  paths.py                # sessions/runtime 路径解析
  mailbox.py              # 邮箱凭据读取、OAuth token 刷新、邮件 OTP 轮询
  providers/luckmail_token.py  # LuckMail token 直连 OTP API 客户端
  registration.py         # 邮箱注册、auth session、PayPal 链接固化
  gen_pp_link.py          # 使用 accessToken 生成 PayPal 支付链接
  utils.py                # 随机数据和步骤计时工具
SmsWorkbench/             # .NET WPF 管理台
sessions/                 # 固化后的 session JSON（敏感数据，不提交）
runtime/                  # Sentinel 等运行时缓存（不提交）
docs/architecture.md      # 模块职责和维护边界
```

## 配置

复制 `config.example.json` 为 `config.json`，配置邮箱池或 LuckMail。当前主流程不再使用手机接码。

### 邮箱对接

默认读取：

```text
F:\epsoft\GPT-Register-Tool\mailbox_tokens.txt
```

邮箱 token 文件格式：

```text
email---password---refresh_token---access_token---0
```

也可以直接传入单个邮箱：

```bash
python chatgpt_phone_reg.py --email user@example.com --email-password Pass123 --email-refresh-token REFRESH_TOKEN
```

也可以传入 LuckMail 已购买邮箱的 token；脚本会直连 LuckMail OpenAPI 解析邮箱地址并读取验证码：

```bash
python chatgpt_phone_reg.py --luckmail-token tok_xxx --proxy socks5h://127.0.0.1:7897
```

一条命令购买 LuckMail 长效 Outlook（微软 IMAP）邮箱并完成注册、PayPal 链接生成、session 固化：

```bash
python chatgpt_phone_reg.py --buy-luckmail-mailbox --proxy socks5h://127.0.0.1:7897
```

LuckMail token 对接使用 `GET /api/v1/openapi/email/token/{token}/code` 取验证码，`GET /api/v1/openapi/email/token/{token}/mails` 取最新邮件列表，`GET /api/v1/openapi/email/token/{token}/alive` 检测邮箱。项目只依赖 LuckMail 官方接口。

### LuckMail

如果没有本地邮箱池，且 `email_registration.luckmail_api_key` 已配置，脚本会自动在 LuckMail 创建 `openai` 项目订单，使用返回的邮箱注册，并通过 `order/{order_no}/code` 轮询验证码。

```json
"email_registration": {
  "luckmail_api_key": "YOUR_LUCKMAIL_API_KEY",
  "luckmail_base_url": "https://mails.luckyous.com",
  "luckmail_project_code": "openai",
  "luckmail_email_type": "self_built"
}
```

## 批量购买注册与 SQLite 索引

批量购买 LuckMail 长效 Outlook 邮箱、逐个注册、生成 PayPal 链接并固化：

```bash
python chatgpt_phone_reg.py --buy-luckmail-mailbox --count 5 --proxy socks5h://127.0.0.1:7897
```

该流程会：

1. 调用 LuckMail `email/purchase` 一次性购买 `--count` 个邮箱。
2. 只注册实际返回的邮箱数量，避免邮箱不足时循环复用同一个邮箱。
3. 每个成功账号继续获取 `/api/auth/session` 的 `accessToken`，生成 PayPal 链接。
4. 继续保存 `sessions/session_{email}_{timestamp}.json`。
5. 同步写入 SQLite 索引 `runtime/accounts.sqlite3`，供 WPF 前端查看、搜索、删除和维护。

从历史 session JSON 重建 SQLite：

```bash
python chatgpt_phone_reg.py --rebuild-sqlite
```

### PayPal 链接

`paypal.auto_generate` 默认为 `true`。注册成功并从 `/api/auth/session` 取到 `accessToken` 后，会自动调用：

```python
from sms_tool.gen_pp_link import generate_pp_link
```

如需只注册并跳过 PayPal 链接生成：

```bash
python chatgpt_phone_reg.py --skip-paypal-link
```

人工支付与 session 刷新：

```bash
# 展示已保存的 PayPal 链接和状态
python chatgpt_phone_reg.py --list-paypal-links

# 打开指定账号的 PayPal 链接，由人工在浏览器完成授权或支付
python chatgpt_phone_reg.py --email user@example.com --open-paypal-link

# 旧链接过期时，重新生成 PayPal 链接并回写 session JSON 和 SQLite
python chatgpt_phone_reg.py --email user@example.com --regenerate-paypal-link

# 批量重新生成 PayPal 链接（每行一个邮箱，默认最多 4 并发）
python chatgpt_phone_reg.py --regenerate-paypal-link --email-file emails.txt --workers 4

# 人工支付完成后标记状态
python chatgpt_phone_reg.py --email user@example.com --mark-paypal-status completed

# 打开可见浏览器，人工完成登录/授权后刷新 session JSON 和 SQLite
python chatgpt_phone_reg.py --email user@example.com --refresh-session
```

该流程只打开官方托管支付/登录页面，不在项目内处理 PayPal 开户、短信接码、卡号、日期或 CVV。

### 打开支付链接

WPF 的“打开支付链接”现在只调用正常 Chrome 浏览器打开当前支付 URL，不再附加 `--user-data-dir`、`--load-extension`、`--incognito` 等启动参数。

本地 PayPal 自动填写扩展文件仍保留在项目中，可按需手动使用：

```text
browser_extensions/paypal_autofill/
```

## 使用

```bash
# 注册 1 个账号
python chatgpt_phone_reg.py

# 注册 5 个账号
python chatgpt_phone_reg.py --count 5

# 批量注册并生成 PayPal 链接（最多 4 并发）
python chatgpt_phone_reg.py --count 5 --workers 4

# 指定邮箱池
python chatgpt_phone_reg.py --mailbox-file F:\path\mailbox_tokens.txt

# 指定代理
python chatgpt_phone_reg.py --proxy http://user:pass@host:port
```

## 输出

脚本只保存成功注册且带 `access_token` 的账号。输出文件名默认：

```text
sessions/session_{email}_{timestamp}.json
```

session JSON 示例：

```json
{
  "email": "user@example.com",
  "phone": "",
  "password": "Un59hMqqE!A1",
  "session_token": "",
  "access_token": "eyJ...",
  "refresh_token": "MAILBOX_REFRESH_TOKEN",
  "cookie_header": "__Secure-next-auth.session-token=...",
  "paypal": {
    "ok": true,
    "url": "https://www.paypal.com/checkoutnow?token=..."
  },
  "mailbox": {
    "email": "user@example.com",
    "password": "MailboxPassword",
    "refresh_token": "MAILBOX_REFRESH_TOKEN",
    "access_token": "MAILBOX_ACCESS_TOKEN",
    "source": "F:\\epsoft\\GPT-Register-Tool\\mailbox_tokens.txt"
  }
}
```

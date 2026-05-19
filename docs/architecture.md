# 项目结构和职责边界

本项目的主链路是：邮箱来源 -> ChatGPT 邮箱注册 -> 获取 auth session/access token -> 生成 PayPal 授权链接 -> 固化到 session JSON 和 SQLite -> WPF 展示与维护。各层只负责自己的边界，避免 UI、注册协议、支付协议和存储互相穿透。

## 顶层目录

```text
chatgpt_phone_reg.py      兼容入口，只转发到 sms_tool.cli
config.example.json       配置模板
config.json               本地配置，包含密钥，不提交
hotmail.txt               Chatai 格式邮箱池，可由 WPF 导入生成
mailbox_tokens.txt        标准 Graph/OAuth 邮箱池，可选
sessions/                 成功注册后的 session JSON，不提交
runtime/                  SQLite、Sentinel 缓存、临时运行数据，不提交
sms_tool/                 Python 后端能力层
SmsWorkbench/             WPF 桌面管理端
docs/                     维护文档
```

## 模块职责

```text
SmsWorkbench/MainWindow.xaml.cs
  UI 编排层。负责导入邮箱、选择账号、启动 CLI、展示 SQLite/session 状态。
  不直接实现 ChatGPT 注册协议或 PayPal 协议。打开 PayPal 链接时可加载本地自动填写扩展。

sms_tool/cli.py
  命令行入口和批处理编排。负责解析参数、选择邮箱来源、调用注册、保存结果。
  显式传入邮箱文件但解析为空时必须失败退出，不允许偷偷 fallback 到 LuckMail。

sms_tool/mailbox.py
  邮箱来源和 OTP 收信层。负责 Chatai/Graph/LuckMail token 文件解析、Microsoft token 刷新、OTP 轮询。
  文件解析使用 utf-8-sig，兼容 UTF-8 BOM。

sms_tool/http_client.py
  传输层。负责 curl_cffi 请求超时、瞬时 TLS/代理错误识别和重试。
  业务层不直接处理 curl 错误细节。

sms_tool/registration.py
  ChatGPT 注册编排层。负责 Sentinel、auth flow、邮箱 OTP、create account、auth session 获取。
  任何网络异常都返回结构化失败结果并入库，不允许 traceback 直接中断 UI 批次。

sms_tool/gen_pp_link.py
  PayPal 链接生成层。只使用 access token 生成官方托管授权链接。

sms_tool/storage.py
  SQLite/session JSON 固化层。负责账号索引、支付状态、refresh 状态和历史 session 重建。

sms_tool/session_refresh.py
  已注册账号 session 刷新层。默认协议刷新，浏览器刷新只作为显式 fallback。
```

## 一键注册+支付链接边界

WPF 的“一键注册+支付链接”按钮遵循以下规则：

1. 如果当前选中行能还原邮箱凭据，优先只注册这一条，并生成临时单行邮箱文件。
2. 如果没有可用选中行，才使用当前 Chatai 邮箱池文件和 UI 中的 count。
3. 临时文件使用无 BOM UTF-8，避免 Python 把 BOM 当成 malformed line。
4. 后端收到显式 `--chatai-mailbox-file` 或 `--mailbox-file` 后，如果解析不到邮箱，直接 `EXIT 2`。
5. 后端不会因为 Chatai 文件为空而自动新建 LuckMail 邮箱，避免“点失败邮箱却注册了新邮箱”的串线问题。

## 注册失败处理

注册流程中所有失败都应分成两类：

- 业务失败：例如账号创建 400、OTP 超时、邮箱已注册。这类结果写入 SQLite，`status=failed`，保留邮箱元数据。
- 传输失败：例如 `curl_cffi` TLS connect error、代理连接失败、超时。这类请求由 `sms_tool/http_client.py` 重试；最终仍失败时返回 `transport_error` 或具体阶段错误，并写入 SQLite。
- 支付链接失败：账号注册成功但 PayPal 链接没有生成时，SQLite 使用 `status=paypal_failed` / `paypal_status=failed`，CLI 在保存 session 和 SQLite 后以 `EXIT 3` 结束，让 WPF 任务状态显示失败。

这样 WPF 批次不会因为一次 TLS 抖动崩掉，失败账号也能在列表里继续被筛选、删除或重试。

## 数据边界

- `sessions/` 只放成功注册且拿到 access token 的 session JSON。
- `runtime/accounts.sqlite3` 是 UI 的主索引，既保存成功账号，也保存结构化失败账号。
- `runtime/sentinel_cache.json` 是可再生成缓存。
- `hotmail.txt` 和 `mailbox_tokens.txt` 是输入池，不应该由注册协议层直接修改。
- 临时单行重试文件写入系统 temp 目录，只作为 CLI 输入。

## PayPal 与 session 刷新边界

项目只生成并保存官方托管 PayPal 授权链接。自动填写插件只负责把本地配置填入页面字段，不隐藏验证码、不读取短信、不自动提交最终支付/授权表单。

维护入口：

- `--list-paypal-links` 展示已保存链接和状态。
- `--open-paypal-link` 打开指定账号链接。
- `--regenerate-paypal-link` 使用已有 access token 重新生成短时链接。
- `--regenerate-paypal-link --email-file <file> --workers 4` 批量重新生成短时链接，每行一个邮箱，最多 4 并发。
- `--mark-paypal-status completed` 标记人工支付完成。
- `--refresh-session` 默认走协议刷新 auth session。
- `--browser-refresh-session` 才使用旧浏览器刷新路径。

## PayPal 自动填写插件

插件目录：

```text
browser_extensions/paypal_autofill/
  manifest.json          Chrome 扩展清单
  content.js             页面字段识别和填写逻辑
  profile.generated.js   WPF 打开支付链接前按 config.json 生成，已加入 .gitignore
```

WPF 打开支付链接时只启动正常 Chrome，并只传入支付 URL：

```text
chrome.exe <paypal_url>
```

因此不会额外注入 profile、扩展、无痕模式或其它浏览器启动参数。PayPal 自动填写扩展文件仍保留在项目中，但不再由“打开支付链接”按钮自动加载。

填写来源：

- `paypal_auto.phone_number`
- `paypal_auto.cards[0].number / exp_month / exp_year / cvv`
- `paypal_auto.addresses[0].line1 / city / state / postal_code`
- PayPal signup 邮箱按当前账号邮箱生成 Gmail alias，例如 `account+pp123@gmail.com`

插件允许自动点击 PayPal 支付方式和“Create an account/Sign up”入口，让表单出现；但不会点击最终 `Pay`、`Agree`、`Subscribe` 等提交按钮。

## 配置边界

```json
"timeouts": {
  "request": 20,
  "http_retries": 3,
  "retry_delay": 2,
  "token_cache_ttl": 300
}
```

- `request`：单次 HTTP 请求超时秒数。
- `http_retries`：瞬时传输错误重试次数。
- `retry_delay`：重试间隔秒数。
- `token_cache_ttl`：Sentinel token 缓存时间。

代理配置仍由 `proxy.default` 或 CLI `--proxy` 提供；PayPal 链接生成可以通过 `paypal.stage_proxies` 单独配置分阶段代理。

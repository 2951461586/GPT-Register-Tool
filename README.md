# GPT-Register-Tool

通过 SMS-Activate 兼容接码平台注册，并只保存成功账号的 session JSON。

## 环境准备

```bash
pip install curl_cffi playwright
playwright install chromium
```

## 项目结构

```text
chatgpt_phone_reg.py      # 兼容入口，只负责调用 CLI
sms_tool/
  cli.py                  # 命令行参数和输出保存
  config.py               # config.json 加载
  sms_provider.py         # HeroSMS / SMSBower 接码渠道
  mailbox.py              # nb-register Outlook 批注册、OAuth、Graph 邮件 OTP
  registration.py         # ChatGPT 注册主流程
  utils.py                # 随机数据和步骤计时工具
SmsWorkbench/
  App.xaml                # WPF 应用入口与全局样式
  MainWindow.xaml         # WPF 管理台界面
  MainWindow.xaml.cs      # 界面逻辑、池状态读取、后端进程调用
  SmsWorkbench.csproj     # .NET 10 Windows WPF 项目
  build_dotnet.ps1        # 使用 .NET 10 SDK 发布窗口程序
```

## 配置

复制 `config.example.json` 为 `config.json`，然后配置接码平台。

### 接码渠道

`phone_sms.provider` 支持：

- `herosms`
- `smsbower`

HeroSMS:

```json
"phone_sms": {
  "provider": "herosms",
  "herosms_api_key": "YOUR_HEROSMS_API_KEY",
  "service": "dr"
}
```

SMSBower:

```json
"phone_sms": {
  "provider": "smsbower",
  "smsbower_api_key": "YOUR_SMSBOWER_API_KEY",
  "smsbower_base_url": "https://smsbower.page/stubs/handler_api.php",
  "service": "dr"
}
```

`service`、`country`、`max_price`、`min_price`、`blocked_countries` 会用于选号和取号。SMSBower 也会收到 `maxPrice/minPrice` 参数。

### 邮箱对接

当前项目兼容 `nb-register` 的 Outlook token 文件格式：

```text
email---password---refresh_token---access_token---0
```

默认读取：

```text
F:\epsoft\nb-register\outlook-register-service\Results\outlook_token.txt
```

也可以通过命令行指定：

```bash
python chatgpt_phone_reg.py --mailbox-file F:\path\outlook_token.txt
```

如需直接传入单个邮箱：

```bash
python chatgpt_phone_reg.py --email user@outlook.com --email-password Pass123 --email-refresh-token REFRESH_TOKEN
```

## 使用

```bash
# 注册 1 个账号
python chatgpt_phone_reg.py

# 使用 SMSBower 注册
python chatgpt_phone_reg.py --sms-provider smsbower

# 注册 5 个账号
python chatgpt_phone_reg.py --count 5

# 指定国家和服务
python chatgpt_phone_reg.py --country 23 --service ot

# 手动提供手机号
python chatgpt_phone_reg.py --phone +2343000000000 --password MyPass123!A1
```

### Outlook 邮箱批注册

该入口复用 `F:\epsoft\nb-register\outlook-register-service\camoufox_register.py`，流程为：

1. 注册 Outlook 邮箱，写入 `unlogged_email.txt`
2. 运行 Microsoft OAuth，获取 `refresh_token/access_token`
3. 写入 `outlook_token.txt`
4. 只为带 `refresh_token` 的成功邮箱保存 JSON

```bash
# 批量注册 5 个 Outlook 邮箱并获取 refresh token
python chatgpt_phone_reg.py --outlook-register --count 5

# 指定 nb-register 脚本和结果目录
python chatgpt_phone_reg.py --outlook-register --count 5 ^
  --outlook-script F:\epsoft\nb-register\outlook-register-service\camoufox_register.py ^
  --outlook-results-dir F:\epsoft\GPT-Register-Tool\outlook_results

# 只注册邮箱密码，不跑 OAuth
python chatgpt_phone_reg.py --outlook-register --count 5 --outlook-skip-oauth
```

OAuth 过程中如果 Microsoft 要求人工邮箱验证码，可在 `config.json` 的 `outlook_register.oauth_verification_code` 或 `outlook_register.oauth_verification_code_file` 中提供。

## Windows 管理台界面

当前仓库包含一个 .NET 10 WPF 管理台，界面参考原型图组织为顶部操作栏、账号池表格、任务队列和底部日志。它不会复制注册逻辑，而是调用现有 Python CLI：

- 批量注册 Outlook：执行 `python chatgpt_phone_reg.py --outlook-register --count N`
- 批量注册 Free：执行 `python chatgpt_phone_reg.py --count N --sms-provider ...`
- 邮箱池状态：读取 `outlook_token.txt` 和 `unlogged_email.txt`
- 号池状态：读取 `session_*.json`
- 代理出口：从界面输入框传入本次批次的 `--proxy`

编译：

```powershell
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
```

运行：

```powershell
.\dist\net10\SmsWorkbench.exe
```

## 输出

脚本只保存成功注册且带 `refresh_token` 的账号，失败结果不再落盘；如果成功结果里没有 refresh token，会打印提示但不保存文件。输出文件名默认：

```text
session_{email}_{timestamp}.json
```

session JSON 示例：

```json
{
  "email": "user@outlook.com",
  "phone": "+2343188686716",
  "password": "Un59hMqqE!A1",
  "session_token": "",
  "access_token": "",
  "refresh_token": "MAILBOX_REFRESH_TOKEN",
  "mailbox": {
    "email": "user@outlook.com",
    "password": "MailboxPassword",
    "refresh_token": "MAILBOX_REFRESH_TOKEN",
    "access_token": "MAILBOX_ACCESS_TOKEN",
    "source": "F:\\epsoft\\nb-register\\outlook-register-service\\Results\\outlook_token.txt"
  },
  "sms": {
    "provider": "smsbower",
    "activation_id": "379768557"
  }
}
```

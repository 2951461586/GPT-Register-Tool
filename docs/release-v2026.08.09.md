# v2026.08.09 发布说明

本版本集中完成协议注册 P0/P1 稳定性、运行配置与敏感数据边界、桌面后端任务生命周期，以及支付适配器的结构整理。

## 协议注册

- 注册成功后先保存 Session、账号候选和断点，再执行 AT HTTP 200 探活；探活遇到代理或 TLS 未知状态时可继续恢复。
- 注册账号绑定独立代理会话，批量重试只对网络和认证状态故障换线。
- 新增会话级 403/429 熔断，避免同一受限会话持续请求。
- 注册流程使用状态机和原子 checkpoint，明确区分邮箱、Sentinel、Auth、账号创建、AT 探活与终态。
- CLI 在邮箱采购前预检 ChatGPT、Auth、Sentinel 和 `curl_cffi` profile，失败时不消耗邮箱。

## Fingerprint 与 Sentinel

- 固定 `curl_cffi==0.16.0`，生产注册要求 `chrome146` profile 可用。
- NextAuth、Auth API、ChatGPT 拆分为三套 Header 模板，并共享稳定 DID、Session ID、调用 ID、UA 和 client hints。
- Fingerprint 的语言和时区根据代理 GeoIP 生成，并传入 Sentinel QuickJS VM。
- QuickJS 分阶段生成 `username_password_create`、`authorize_continue`、`oauth_create_account` token。
- Sentinel token、Cookie 和 Header 的 DID 必须一致；QuickJS/真实 SDK 失败按 `sentinel_extract_failed` 终止，生产模式不使用纯 HTTP PoW 降级。
- QuickJS VM 补齐 `navigator.userAgentData`、viewport、scroll、时区、heap、time origin 和 Chrome 版本信息。

## ReMail

- 长效邮箱的本地化主题发生乱码时，可使用 ReMail 返回的结构化验证码快速路径。
- 快速路径只接受精确收件人、六位数字和受支持的 OpenAI OTP 发件人，保留时间戳、快照和排除码过滤。
- 实际 AT-Only 协议验证已完成：邮箱 OTP、账号创建、Session 落盘和 AT HTTP 200 探活均成功。

## 架构与安全

- 引入不可变运行配置和工作流预检，避免模块导入时读取当前目录配置。
- 邮箱 provider registry、`MailboxService`、注册状态机和 stage handler 从兼容入口中拆分。
- Python/WPF 共用 `sensitive_policy.json`，日志、IPC、报告和异常统一完整脱敏。
- 桌面端使用 `BackendTaskCoordinator` 管理单任务、取消、超时和清理。
- 支付方法由 `payment_methods.json` 统一描述，Python 与 WPF 共用同一目录；协议脚本复用公共 transport/result 层。

## 验证

- Python 全量测试、Python 编译检查、架构扫描和敏感字段扫描。
- .NET 测试与 `SmsWorkbench/build_dotnet.ps1` 标准发布构建。
- 安装器、便携 ZIP 和 SHA-256 清单来自同一次 `v2026.08.09` 构建。

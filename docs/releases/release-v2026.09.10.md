# v2026.09.10

本版本完成协议注册失败修复、注册持久化收口、WPF 日志发布和离线 canary 验证。

## 协议注册

- 修复 protocol 注册身份初始化中 `get_device_context` 依赖注入错误。
- protocol identity 初始化现在复用 `RegistrationPersistence` 适配器，避免迁移后引用失效。
- 内部编程错误与配置错误独立分类为 `internal` / `configuration`，不再落入 `unknown`。
- 内部、账号、邮箱和非代理故障不会污染共享代理健康统计。
- `persist_result` journal 在失败路径写入终态，不再留下永久 `running` 记录。
- 无成功账号时跳过注册后的优惠检查。

## 测试与验证

- 新增 Mock mailbox、Mock persistence、Mock network 的 protocol identity 离线 canary。
- 新增内部异常分类、代理归因、持久化 journal 和优惠检查跳过回归测试。
- 全量 Python 测试：`3328 passed, 6 skipped`。
- WPF Release/win-x64 发布成功，规范产物为 `dist/net10/SmsWorkbench.exe`。
- 使用原失败邮箱复跑成功：完整通过 identity、OTP、账号创建、AT 探测、TOTP 和优惠检查。

## 发布资产

- Windows 安装器：`GPT-Register-Tool-Setup-v2026.09.10.exe`
- Windows 便携包：`GPT-Register-Tool-win-x64-v2026.09.10.zip`
- SHA-256 校验清单：`GPT-Register-Tool-v2026.09.10.sha256.txt`

本版本未将 `config.json`、`runtime/`、`sessions/`、邮箱凭据、代理凭据或 Token 纳入发布提交和资产。

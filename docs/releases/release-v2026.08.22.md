# v2026.08.22

## 邮箱换绑

- 新增批量邮箱换绑流程，支持并发执行。
- 支持 ReMail、CFWorker、Smailr 随机目标邮箱，以及 iCloud、Outlook、Hotmail 凭证池。
- 换绑前执行登录和资格检查，换绑后重新登录并通过账号测活才迁移本地状态。
- SQLite 与 Session JSON 使用目标邮箱冲突检查和回滚保护。

## 桌面端

- 统一浅色/深色主题色板和邮箱列表概览统计。
- 优化侧边栏收缩动画、日志清空按钮布局和邮箱换绑弹窗边界。
- 移除支付完成入口及相关展示，保留协议支付和账号测活操作。

## 工程整理

- 清理 Python/.NET/test 生成物，不触碰运行数据库、Session、邮箱凭证和本地配置。
- 将邮箱换绑 CLI 参数适配器与 WPF provider 选择弹窗从主编排逻辑中拆出，并拆分主题、侧边栏动画与窗口标题栏职责。
- 更新目录职责、邮箱换绑边界和发布说明。

## 验证

- Python 全量测试：1019 passed，另有 28 个 subtests passed。
- .NET 测试：218 passed。
- `python -m compileall -q sms_tool`、`git diff --check`、桌面发布和安装包 SHA-256 校验通过。

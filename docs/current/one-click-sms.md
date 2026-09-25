# 一键接码当前契约

入口：桌面命令规划 → `sms_tool/commands/one_click.py` → `codex_oauth.py`
（授权与结果保存）→ `phone_reuse.py`（租号生命周期）。命令适配层只选账号、
调度与汇总，不写 session 文件或 SQLite。

供应商名称、端点、协议族与环境变量名的真源在 `sms_tool/sms_providers.py`，
生命周期契约在 `sms_tool/sms_provider_adapter.py`。`smsbower.py` 等
sms-activate 客户端以 activation ID 管理租约；`nexsms.py` 使用号码作为
生命周期键，不得伪造 activation ID。桌面目录按协议族读取余额、国家、
价格与库存。配置优先级见 [配置契约](configuration.md)，排障按
[接码检查清单](../TROUBLESHOOTING.md) §13–16。

OAuth/SMS 成功的 token 由 `codex_oauth` 保存。失败时该模块将脱敏的
`response.codex_oauth` 与手机号尝试结果写入 session JSON 和 SQLite。
session 文件采用原子替换；两处写入不能跨介质原子提交，因此返回
`persistence.session_saved`、`account_saved`、`persisted`。单侧失败的
`error_code` 仅为 `session_write_failed` 或 `account_write_failed`，
不会把路径、供应商响应或凭据写进公开结果。远端失败与本地部分保存
是两项独立事实，批次仍按远端结果记失败。

账号测活与接码不是同一流程。`--one-click-scan` 默认只探测 AT；
只有显式深探测或恢复选项才允许 OAuth/OTP 副作用，详见
[账号健康契约](account-health.md)。

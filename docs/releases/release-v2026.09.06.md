# v2026.09.06

## 测活与恢复稳定性

- 账号测活使用轻量 HTTP probe 优先，浏览器 fallback 限制为最多 2 路并发。
- 401 自动重登保留 WPF 勾选项，但恢复链独立限制为最多 2 路并发，避免阻塞普通测活。
- 测活批次改为异步 worker 汇总，并持续写入 `runtime/account_liveness_batches/` 快照。
- 批次超时后保留已完成结果，结果契约增加 `partial`、`unfinished`、`timed_out`。
- `liveness_401` 记录原始探测结果，不会被重登后的 HTTP 200 覆盖。

## 邮箱池安全闸门

- ReMail 与 iCloud 的 `mailbox_auth_invalid` 状态统一纳入自动重登阻断。
- 新增 `--mailbox-pool-repaired`，只有邮箱凭据完成替换、复核并显式确认后，才允许重新启用 `--quota-auto-relogin`。
- 不输出邮箱 token、密码、Cookie、AT 或代理凭据。

## WPF 与发布

- WPF 超时后会读取最近测活快照并显示部分结果。
- 测活默认单账号预算 120 秒、批次预算 840 秒，预留最终 IPC envelope 排空时间。
- 发布产物仍统一输出到 `dist/net10/`。

## 验证

- Python `compileall` 通过。
- .NET 测试：261 passed。
- WPF 发布文件：`dist/net10/SmsWorkbench.exe`。
- 本版本未运行注册、支付或大批量账号恢复任务。

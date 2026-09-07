# v2026.09.06.1

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

## CI 与发布修复

- 修复浏览器流程 patch 守卫测试使用固定 `F:` 路径，改为从测试文件定位仓库根目录，兼容 GitHub Actions 工作目录。
- 修复路径契约测试硬编码 `F:` 盘符，改为使用当前仓库盘符。
- 修复配置使用检测测试依赖本机未跟踪 `config.json` 的问题，兼容 CI 干净配置。
- 修复空配置报告测试，使其同时覆盖无未读项和存在未读项两种合法状态。
- 重新构建 WPF、安装器和 Windows x64 便携包，资产与校验文件同步更新。

## 验证

- Python：`2784 passed, 6 skipped`。
- .NET：`261 passed`。
- GitHub Actions Windows：Run `33981650813` 成功。
- 发布包已通过 payload、敏感字段和文档一致性扫描。
- 本版本未运行注册、支付或大批量账号恢复任务。

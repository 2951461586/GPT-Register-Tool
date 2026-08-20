# v2026.08.20

## 协议支付与 PayPal

- PayPal 提链采用标准 `Checkout -> Stripe init -> PM create -> confirm -> approve -> promotion -> poll` 顺序；促销只在同一 Checkout 完成 approval 后应用。
- ChatGPT Checkout approval 返回 HTTP 409 且 `result=blocked` 时，不在原 Checkout 重发 approve。流程保存脱敏诊断和 `last_retry_error`，从创建新 Checkout 开始重建。
- 提链前新增 PayPal 支付方式能力与零元资格探测；`checkout_not_zero_due` 归入资格/报价失败，不再混入普通适配器故障。
- 保存 HTTP 状态码、脱敏 endpoint、provider error code 和有限响应摘要；`authorization_queued` 等状态字段不会被 token 脱敏规则误处理。
- PayPal BA 链接提取与后续授权解耦，授权任务写入持久队列；支付结果对账通过通用 reconciliation 接口分发。
- 支付探测结果与正式提链结果分别保存，`unknown` 结果统一标记 `requires_reconciliation`，避免不确定副作用被自动重试。

## 批次与代理

- 桌面端每次默认创建新的支付批次 ID；只有显式勾选“恢复已有断点”才复用旧批次，并显示执行模式与恢复账号数量。
- 支付批次事件写入 JSONL，桌面进程重启后可从事件和原子报告恢复进度，不再只依赖当前进程内回调。
- 账号级报告新增阶段耗时、总耗时和最后失败阶段；阶段事件统一携带 domain、operation、run_id、batch_id、account_ref、stage、status 等字段。
- PayPal Checkout、Provider、Confirm、Approve、Promotion 等路由统一校验国家；代理健康结果按出口缓存，并区分国家不匹配、网络失败与冷却状态。
- “测试代理”使用短超时和有界重试，避免坏代理长时间占用桌面弹窗。

## 桌面与账号库存

- 账号库存加入 `source`、`register_method`、`session_type`、`plan_type`，邮箱 provider 名称统一展示为 ReMail、Outlook、iCloud、CFWorker 等实际类型。
- 账号测活、查优惠和批量协议支付弹窗补充账号级进度；修复弹窗宽度、取消按钮遮挡和阶段 100% 但仍显示执行中的状态问题。
- 账号列表右键菜单增加“复制 AT”，账号详情移除额度模块。
- 批量协议支付统一支持已选账号或最多 10 个手工 AT，默认重试 3 次，并按账号展示结果、支付链接或二维码。

## 验证说明

- Python 全量测试：1013 passed，另有 28 个 subtests passed。
- .NET 测试：219 passed。
- Python 编译检查、`git diff --check`、规范桌面发布和安装包 SHA-256 校验通过。
- 线上 PayPal 成功率仍取决于代理的 TLS/CONNECT 稳定性、出口国家和上游风控；网络失败与协议资格失败会在报告中分开记录。

# v2026.08.18 发布说明

本版本集中完成命令层解耦、桌面读路径性能优化、协议支付契约加固、代理路由修复、安装环境诊断和仓库生成物清理。

## 架构与性能

- 将 `cli.py` 的注册、账号、邮箱、支付链接、一键操作和 Omakase 命令迁移到 `sms_tool/commands/`，CLI 保留兼容薄入口。
- 清理 `payment_link_manager.py` 被覆盖或零引用的旧实现，保留单一支付路由和结果契约。
- 新增常驻 `--desktop-serve` JSONL 后端；`DesktopReadClient` 支持请求 ID 关联、进程自动重启和 one-shot 回退，账号/邮箱合并读取并按文件元数据缓存。
- 注册输出 `Saved session:` 后立即触发防抖异步刷新；多选账号删除合并为一个后端批量命令并发执行。
- 注册代理预检并行化，支付批次 checkpoint 写入节流；`cross_process_gate.py` 防止 CLI 与桌面并行超卖注册阶段配额。

## 协议支付与代理

- 支付代理统一为 Kookeey 路由，并在子进程启动前校验 Checkout、Approve 和 Promotion 的真实出口国家。
- 修复方法配置合并及运行时国家优先级，当前弹窗选择不会再被历史 `stage_routes` 或保存值覆盖。
- Checkout、Approve、Update 使用同一完整账单地区目录，不再只提供 JP/TR。
- PayPal 的 `oaics_*` 原生 Checkout 直接返回链接，不调用 Stripe；`cs_*` 继续执行 Stripe/PayPal 协议。
- iDEAL、BLIK、TWINT 共用 `ProtocolResultReporter`，统一 `protocol_payment.v1` 序列化、脱敏、exactly-once 输出、已支付及缺失输出兜底。
- 结构化结果和日志统一敏感信息策略，移除从任意日志 URL 猜测支付链接的旧兜底。

## 安装与清理

- 新增 `python -m sms_tool --doctor`，检查 Python、Node.js、Playwright、协议依赖和配置完整性；桌面启动与安装器复用同一诊断契约。
- 安装器支持缺失依赖提示、安装和复检，便携包与配置说明同步更新。
- 清理 Python/.NET 缓存、历史协议日志、旧 release/hotfix 产物、`.zcode` 和根 `gates`；生成状态统一通过 `.gitignore` 管理，活动锁文件仅位于 `runtime/gates/`。

## 验证与产物

- Python 全量测试、协议支付定向测试、Python 编译检查、.NET 测试和规范桌面发布均在发布提交上执行。
- Release 提供 Windows 安装器、便携 ZIP 和同次构建生成的 SHA-256 校验清单。

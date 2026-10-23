# v2026.10.23

自 v2026.10.19 以来的收口：协议注册与协议支付提链按 2026-10-23 的 Chrome 155 HAR 更新客户端指纹与认证头；
支付批量的失败隔离、提取器超时预算和启动成本同步收紧。

## 新增功能

- **Chrome 指纹基线跟进** —— 协议请求的 `sec-ch-ua` 改为真实 Chrome 155 的顺序与品牌 token
  （`"Google Chrome"` 优先、`Not(A:Brand";v="24"`），`sec-ch-ua-platform-version` 更新为
  Windows 11 24H2 的 `19.0.0`，`oai-client-version` 更新为 `26.1022.91109`；新增 `chrome150`
  指纹档案（curl_cffi 0.16.0 支持的最新版）。旧的 `Not.A/Brand";v="99"` / `10.0.0` / `prod-…`
  在服务端看是过期客户端。
- **认证头逐请求化** —— `x-openai-document-navigation-id` 在 send-otp / validate / create_account
  三个请求上共用同一页面级值；`x-access-flow-invocation-id` 改为每个请求各自生成（与 HAR 一致）。
- **支付批量断点更可靠** —— 单行出现 `KeyError` / `AttributeError` / 自定义异常时不再中断整批；
  该行记为 `payment_worker_exception`，已完成行的 checkpoint 正常落盘。
- **`--init-config` 配置分片引导** —— 空配置目录一次生成 `proxy` / `runtime` / `payment` 三个分片；
  重复执行只跳过已存在项。
- **浏览器池内存上限有实测建议值** —— `max_total_rss_mb` 保持默认关闭，但 `config.example.json`
  写入「并发 × 512 MB」的建议值；超过 1 个浏览器且阀门关闭时每个池告警一次，`stats()` 也返回建议值。
- **支付运行记录可轮转** —— `runtime/payment_link_runs.jsonl` 在 8 MiB 时轮转，归档按 30 天保留，
  长期运行不再无限增长。

## 修复问题

- **`"false"` 配置被当成 true** —— canary 暂停开关与支付出口门禁用 `bool("false")` 解析，
  手工写成字符串的 `false` / `0` / `no` 反而保持开启；统一走容错布尔解析。
- **Sentinel mint 不计入提取器超时** —— 子进程提取器的最坏耗时是 `timeout + 60s`；现在 mint 与
  子进程共享一个 deadline，配置的 `timeout_seconds` 就是整轮上限。
- **探测与提链的失败契约不一致** —— 代理池不可用或方法不支持时，`probe_payment_method` 直接抛异常，
  而 `generate_payment_link` 返回结构化结果；现在两者都返回归一化结果。
- **启动时多余加载 `requests`** —— `payment.routing` 模块级导入 `proxy.routing`（进而 `phone_proxy`
  → `requests`），单模块导入约 350 ms；改为函数内导入后降到约 85 ms。
- **注册/支付包门面 eager 加载** —— `sms_tool.registration` 与 `sms_tool.pay_link` 的 `__init__`
  会加载整个工作流（691 ms / 431 模块）；改为 PEP 562 惰性门面后叶子模块导入降到毫秒级。
- **真实客户端已不发送 `oai-client-build-number`** —— 移除该头，避免继续伪装成旧版 Web 客户端。
- **`_auth_request_headers` 重复定义** —— `auth_flow/steps.py` 既导入又本地定义同名函数，新增参数只改到
  一份；统一到 `auth_wire` 单一实现。
- **桌面端侧边栏 Logo 定位** —— 图标改为锚定程序集根路径，发布目录下不再丢失。
- **并行测试共用配置目录导致 CI 收集失败**（发布后修复）—— `pytest -n 4` 的 controller
  也会 import `tests/conftest.py`，它建的临时配置目录被 4 个 worker 继承；首装迁移
  （legacy `config.json` → 三分片）因此并发执行，读到「只发布了 `proxy.json`」的瞬间就报
  `ConfigError: chatgpt must be an object`（两次 CI 各挂一个 worker）。现在每个 worker 各建
  一个目录；分片写入同时改为两阶段发布（先 staged 全部变更分片，再连续 rename），把该窗口
  从两次 `fsync` 压到三次 rename。详见 [`TROUBLESHOOTING.md` §18](../TROUBLESHOOTING.md)。

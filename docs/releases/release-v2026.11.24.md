# v2026.11.24

自 v2026.11.22 以来的重大架构升级与性能演进：引入纯 Rust 进程内 Sentinel VM 彻底移除 Node.js 运行时依赖，集成 14 个经差分与基准验证的 `rust-anything` PyO3 热点扩展，将全部 15 种协议支付方式统一收敛至外部提链服务器并彻底退役旧版内置提取器，同时完成代理池、指纹与协议注册链路的专项加固及全仓冗余清理。

## 新增功能与架构升级

- **原生 Rust 进程内 Sentinel VM（彻底移除 Node.js 依赖）** ——
  新增 `native/sentinel_vm/` 纯 Rust `cdylib` 引擎，编译产出 `sms_tool/sentinel/runtime/sentinel_vm.dll`，经由 `sms_tool/sentinel/native_ffi.py` 在进程内直接执行 OpenAI Sentinel PoW、Turnstile `dx` 与 Session Observer `so` 证明，不再需要安装或拉起 Node.js 运行时；二进制完整性由 `sms_tool/sentinel/bundle.py`（`NATIVE_LIB_SHA256`）与 `scripts/sentinel_asset_guard.py` 双向锁定，Cargo `--release` 测试 16/16 全绿。
- **14 个 `rust-anything` PyO3 热点加速扩展（26 个函数 · 4,824/4,824 用例 100% 一致）** ——
  覆盖日志脱敏（`sanitize-text-rs` 8.3×）、邮箱 OTP 提取（`mail-otp-rs` 6.9× / 单函数最高 63×）、失败错误分类（`error-classification-rs` 6.2×）、代理串规范化与国家推导（`normalize-proxy-url-rs` 4.3×）、CFWorker / iCloud / Gmail HTML 清洗（4.0× / 3.5× / 3.0×）、代理网络画像分类（`proxy-screening-rs` 2.6×）、SMS 验证码提取（`sms-code-rs` 2.4×）、浏览器静态资源缓存判定（`data-saver-rs` 2.3×）、Session / JWT 转换（`session-converter-rs` 1.9×）、存储规范化（`store-normalize-rs` 1.9×）、HTTP 回调与 Cookie 过滤（`http-utils-rs` 1.8×）及 CPA 状态分类（`cpa-classify-rs` 1.7×）。
  全部 14 个 crate 均通过 12/12 `rust-anything gate`（168/168 项）、0 `unsafe`、0 MinGW 运行时 DLL 依赖，并完整保留 `RUST_ANYTHING_MODE=old` 与缺省纯 Python 自动回退机制；详见 [`docs/current/rust-acceleration.md`](../current/rust-acceleration.md)。
- **支付链接提取统一对接提链服务器 & 桌面端升级** ——
  新增 `sms_tool/pay_link/extract_server.py` 外部提链服务器 HTTP 适配器，全部 15 种目录支付方式（PayPal、GoPay、GCash、GrabPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、直卡 Checkout、MoMo、QRIS、Bizum、Naver Pay）统一通过外部提链服务器取链，支持自定义服务器地址、掩码 CDK（仅经 `EXTRACT_LINK_*` 环境变量传递）、路径、超时、代理透传与 TLS 校验配置。
  WPF 桌面端「批量协议支付」同步升级更名为「支付链接提取」，内置提链服务器配置面板与并发/重试/Canary/断点续跑控制，并保留只读支付能力探测与支付资格检查链路。
- **彻底退役旧版内置协议支付源提取器与冗余基线** ——
  移除 `services/protocol-payment/`、`sms_tool/upi_link/`、`sms_tool/paypal_link/`、`sms_tool/wallet/`、`sms_tool/pay_link/{adapters,oaics_provider}.py`、`sms_tool/payment/{regional_payment_adapter,upi_*}.py` 及配套退役脚本、测试与夹具，清理失效的 `delayed_import_reasons.json` 与 `function_length_baseline.json` 陈旧条目，收紧 `build_installer.ps1` 打包边界（仅随包分发运行时文件与 `sentinel_vm.dll`，不打入开发期 Rust 源码与录制用例）。

## 稳定性修复与加固

- **协议注册、代理池与指纹库加固** ——
  扩展 9 家主流代理供应商的会话 ID 轮转与模板归一规则，修复 `ProxyHealthTracker` 跨轮转键稳定性与 Kookeey 多账密隔离，强化本地代理桥（`LocalProxyBridge`）对 IPv6 格式化、A 记录故障转移与 HTTPS 上游的处理；修正 GeoIP 时区偏移在夏令时（DST）边界的计算偏差，补齐 Sentinel 浏览器族完整请求头（`sec-ch-ua`、`Accept-Language`、`Priority` 等），清理 `reference_lane` 已否决开关并同步更新全仓架构文档与 README 徽章。

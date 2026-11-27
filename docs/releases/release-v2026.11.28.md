# v2026.11.28

自 v2026.11.25 以来的功能迭代、稳定性修复与原生 Rust 加速扩展：修复 `camoufox 0.5.9` 启动崩溃（#32），新增协议绑卡支付工作流与桌面端窗口，将 `rust-anything` PyO3 热点扩展由 22 个扩充至 **62 个 crate · 122 个函数**，并完成五层架构收口与退役残留清理。

## 新增功能

- **协议绑卡支付工作流与桌面端窗口** —— 新增协议绑卡支付编解码、地区账单/币种目录与 Stripe Hosted（`cs_`）/ ChatGPT Custom（`oaics_`）协议绑卡客户端（`sms_tool/payment/protocol_codec.py`、`sms_tool/payment/protocol_pay.py`），支持离线报价与卡密脱敏校验，并集成 CLI 命令与 WPF 桌面端协议绑卡支付窗口（`ProtocolCardPaymentWindow`）。
- **Wave-5 ~ Wave-14 原生 Rust PyO3 热点扩展（22 → 62 个 Crate · 122 个热点函数）** —— 新增 40 个并扩展多个既有 `rust-anything` PyO3 扩展，覆盖协议绑卡编解码（`protocol-payment-rs` 9.4×）、Sentinel PoW 求解（`sentinel-tokens-rs` 6.9×）、提链服务器基址校验（`extract-server-parse-rs` 6.3×）、注册阶段 URL 归一化（`account-stages-url-rs` 6.0×）、优惠券链接构建（`account-promotion-url-rs` 4.9×）、浏览器注册页面/恢复/会话解析、代理桥地址分类及认证头/JWT/HTML 热点解析；全仓 **20,848 / 20,848 录制用例 100% 一致**、**744 / 744 `rust-anything gate` 全部通过**、0 `unsafe`、0 MinGW 运行时 DLL，全部保留纯 Python 自动回退与 `RUST_ANYTHING_MODE=old` 开关。
- **架构分层收口与工具链升级** —— 完成五层单向依赖零反向白名单、注册与支付跨泳道零耦合、顶部巨型模块拆分、全仓未用导入与跨模块私有 Hub 引用清零、原生扩展加载器收敛至 `sms_tool/kernel/native_loader.py` 及 CI Rust 扩展校验门禁（`scripts/rust_crates_guard.py`）；同步升级 `pyotp` 至 `2.12.0`、`ruff` 至 `0.17.5` 与 `actions/checkout` 至 `v7.0.1`。

## 修复问题

- **Camoufox 启动报错 `AttributeError: 'Screen' object has no attribute 'as_conditions'`（#32）** —— `v2026.11.25` 附带的 `camoufox 0.5.9` 已将指纹生成迁入内置 `fpgen`，启动时要求传入 `camoufox.fingerprints.Screen`，而旧代码仍从 `browserforge.fingerprints` 导入 `Screen` → 已在浏览器注册与 PayPal 浏览器启动路径统一改用 `camoufox.fingerprints.Screen` 并移除冗余的 `browserforge` 依赖。
- **环境预检误报与 README 徽章外链失效** —— `scripts/preflight_env.py` 仍在校验已退役的 Node.js Sentinel 环境并将 Playwright Chromium 作为硬依赖阻断 → 改为调用进程内 Rust Sentinel VM 自检并将 Playwright Chromium 调整为可选；同时修复 `README.md` / `README_EN.md` 中 `[Buy me AI tokens]` 徽章外链误指向 `/api/badge/` SVG 接口导致点击无法跳转主页的问题。
- **退役面、残留测试与未编译脚手架清理** —— 清理已退役的旧版协议支付回放架、本地 UPI 出口租约空操作、根目录迁移脚本、`scripts/` 治理表中的退役包条目、11 个 Rust crate 内未编译的 22 份 `pyconv.rs` / `pynum.rs` 死脚手架文件及过时私有 helper 缺失断言测试，并修复 `test_operator_output.py` 的临时文件残留。

# v2026.10.27

自 v2026.10.24 以来的收口：三个模块（协议注册 / 代理池与指纹库 / 支付资格检测）的专项扫描
与其落地，外加品牌 Logo 更换与桌面端设置页对齐 `proxy.lanes`。

## 新增功能

- **支付资格检测按「契约」复用 Checkout** —— `CheckoutRequestContract.checkout_payload()`
  不含支付方式，同国同币种的方法发的是同一份 create。新增 `_contract_key`（通道 + 国家 + 币种）、
  `_method_inventory`（该 Checkout 已发布哪些方式）与 `_derive_row`：首个探针的答案直接回答
  同契约的其余方法，行上标 `evidence_source=shared_checkout` + `derived_from`。PH 不可用账号
  4 → 3 次 create，ID/KR 双方法 2 → 1；PayPal 与 GCash 各有 transport，永不共享。
  `probe_all_methods`（`--payment-qualification-all-methods`）保持「每方法各付」的诊断网格。
- **支付资格专用出口链路** —— `payment_qualification.proxy_pool` / `proxy.lanes.payment_qualification`
  作为 explicit 出口优先于桌面注册代理；`stripe_preflight` 三态（默认 `warn`）、
  负缓存 5 分钟、`max_proxy_attempts` 与派生的单账号预算、`egress_denied` 独立状态。
- **代理池 ASN 台账与动态生成** —— `proxy/asn_registry.py` 的 `proxy_asn` 持久映射、
  `proxy.dynamic_generation` 的「轮换会话 → 双 pass 筛选 → ASN 允许列表」闸门；
  `proxy_census.py` 增加 `payment_qualification` lane，与 `union_endpoints` / 跨 lane 重叠检查。
- **新的品牌 Logo** —— 桌面端图标（7 帧 ICO，16–256）、`black-kitten.png`、邮件 OTP 网页的
  favicon 与品牌图、两份 README 顶部统一替换。

## 修复问题

- **支付资格轮换空转且归因错误** —— 批次把候选交给单账号探针后，专用池又被提升为 explicit，
  导致每次「轮换」都重拨 `dedicated[0]`：`max_proxy_attempts` 变成同出口重试、专用池不可达时
  永不回落桌面池、失败归因指向从未拨过的出口。新增 `pinned_proxy`（批次传 `True`），
  单账号/CLI 路径保持「专用池优先」。
- **无 scheme 的 `user:pass@host:port` 未脱敏** —— `normalize_proxy_url` 接受这种写法，
  但脱敏只认 `scheme://` 与冒号分隔两种顺序，密码会原样进入筛选表、探针错误文本与 support bundle。
  新增 `_SCHEMELESS_CREDENTIALS_RE`，并保证对已脱敏文本幂等。
- **未映射币种被记成「检测失败」** —— `direct_card` / `paypal` 在 `CURRENCY_MAP` 无该国时
  曾按目录默认币种（PHP）发请求；现跳过并报 `currency_unknown`（徽标「币种未知」）。
- **出口预检的证据与成本** —— `_stripe_preflight` 先只探队首出口（健康池 1 次请求而非 9 次）；
  `chatgpt.com` 预检分开报告「抽样的 N 条」与「池内 M 条」，不再让 3 条抽样读成 9 条全挂。
- **`status_code` 死分支** —— 方法行补 `http_status` / `retryable`，账号结果补 `status_code`，
  「有 HTTP 应答就不轮换」的规则第一次真正可读；`eligible` 账号不再因为一条派生行的说明而带 `error`。
- **设置页 lane 白名单** —— `sms_tool/config/schema.py` 的 `supported_lanes` 补 `payment_qualification`，
  否则在「支付资格专用代理池」填一条并保存后，每个后端命令都会在 `load_runtime_config` 抛
  `ConfigError: unsupported proxy lane`。
- **桌面端与 `proxy.lanes` 对齐** —— 设置页字段直接写入对应 lane；移除对话框底部那条过时且
  位置错误的 lane 告示（旧文案说「本页没有字段」，而保存逻辑早已写这两条 lane）。
- **`AsnRegistry` 句柄泄漏** —— `with sqlite3.connect()` 只提交不关闭，Windows 上会锁住数据库文件；
  改为事务 + 关闭的 `_open()`，`metadata_json` 剔除 `proxy`/`username`/`password`。
- **文档链接错位** —— `docs/current/protocol-payment-enhancement.md` 的归档链接多退了一层目录。

## 参考项目对照

本轮以 `dango-register-source-no-proxy-screening`（纯协议 + Outlook 最小提取包）为基线逐项对照，
结论与差异记录在
`docs/audits/scans/scan-2026-10-27-protocol-registration-proxy-fingerprint-payment-qualification.md`
（§14 / §15）：协议注册阶段顺序与 add-phone 独立 lane 保持不变；一次 Checkout 枚举全通道的
剩余差距仅剩「GCash / PayPal 自有 transport 不能共享」这一条产品边界。

## 验证

- `tests/payment` + `tests/proxy`：2761 passed，1 skipped，564 subtests。
- `tests/registration`：1980 passed，1 skipped；`tests/governance`：703 passed，5 skipped。
- `dotnet test GPTRegisterTool.slnx -c Release` 全绿；`SmsWorkbench/build_dotnet.ps1` 发布成功，
  发布件内嵌图标与 `Assets/app-icon.ico` 逐像素一致。
- `ruff` / `format_guard` / `line_ending_guard` / `function_length` / `unused_import` /
  `lane_coupling` / `delayed_import` / `kernel_boundary` / `config_key` 全绿。
- `pyright`（本轮改动的支付与代理模块）0 errors；
  `docs_consistency_scan` / `refresh_doc_symbol_lines` / `refresh_doc_ratchet_numbers` 全绿。

## 资产

- `GPT-Register-Tool-Setup-v2026.10.27.exe` —— 安装包（内置 CPython 运行时）。
- `GPT-Register-Tool-win-x64-v2026.10.27.zip` —— 免安装包（`dist/net10` 发布产物）。

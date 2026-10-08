# v2026.10.08

自 v2026.09.23 以来的收口（107 个提交）：协议注册泳道的实验有效性与出口级挑战能力、
支付提取器的 Sentinel 门禁与统一结果契约、代理/指纹的单一权威接线，以及一批把
「大文件」拆成有边界的模块的结构性重构。

> ⚠️ **本次发布不改变任何默认行为**：新增的开关（协议注册 9 个、支付/Sentinel 若干）
> 全部默认关或维持原默认；唯一的默认变更在「新功能」里逐条标注。

## 新增功能

### 协议注册

- **A/B 的第二道操纵检查（机制行）** —— `scripts/registration_ab.py` 现在给每个实验登记
  一条 stdout 机制行，`compare` 在 `underpowered` **之前**判 `manipulation_failed`。
  起因是 P1-5 那轮：开关开着但代码路径不可达，5/5 失败被误读成「假设被否」。
- **流程内出口级挑战判据**（`registration.edge_challenge_discrimination`，默认 **开**）——
  `proxy_edge_probe.edge_challenge_verdict` 三态（`challenge`/`not_challenge`/`unknown`），
  失败名带 `:edge_challenge` 后缀。只改名字，不改控制流，所以能默认开。
- **同池换出口一次**（`registration.edge_challenge_rotate_exit`，默认关）—— 挑战时换出口并
  逐字节重放同一请求；单槽池短路且如实记 `rotate_failed`。
- **AT 探测的边缘 403 保留 checkpoint** —— 账号已建好、只是被 CF 边缘拦的 run 不再重走注册
  （终态带 `:edge_blocked`）。
- **signin 形状系列**（全部默认关，逐个单变量）—— `signin_screen_hint_login_or_signup`、
  `signin_prompt_login`、`signin_locale_ja_jp`；`prime_navigation_headers`（补齐
  `sec-fetch-site` / `sec-fetch-user`）。
- **线上实测记录**（`docs/audits/landing-2026-10-08-signin-*.md`）：`locale=ja-JP` 的单轮成功
  **未复现**（1/5 vs 0/4）；成功与失败**唯一完美相关**的读数是
  `passwordless_email_otp_send_pending` 在 `after_otp_send` 里**不存在**，而不是
  `email_verification_mode`。

### 支付

- **Checkout 的 Sentinel 对下发到子进程提取器** —— 此前 7 个提取器一个都没发；
  受控 A/B 证明「不发」直接 `400 unusual activity`、「只发主 token」即 `200`。
  经 `OPENAI_SENTINEL_TOKEN` / `_SO_TOKEN` / `_DEVICE_ID` 环境变量传递（Rule 10 禁止
  `services/` import `sms_tool`）。
- **`direct_card` 端到端产出真实链接**（`oaics_*`，配合国家正确的出口）。
- **统一结果契约 `protocol_payment.v1`** —— 每个提取器**恰好**输出一条终态结果；
  `_log_extractor_terminal` 每 run 一行 INFO（`ok=ok|failed|timeout|no_terminal_contract`）。
- **PayPal 按市场分账单地址池 + run 游标**；**账单模板扩 35 国**，未知国按请求国生成
  （不再回落德国）。
- **UPI**：印度出口分级 / 双 init 双 tax / 换出口重开结账单；OAICS 两步 confirm；
  被动 hCaptcha；mandate 阶段与 stage 级回放测试网。
- **`endpoints.py` 成为 OpenAI host 的单一权威**（`auth.openai.com` / `chatgpt.com` /
  `api.openai.com`），配 `endpoints_literal_ratchet` 冻结内联字面量。
- **支付资格三态标记**（并入「优惠状态」列）：从未探测 / 探测到方式 / 探测但未枚举。

### 代理与指纹

- **只读 ChatGPT 边缘探针** + 池预筛工具；池健康检查可选走真实边缘请求（默认关）。
- **SOCKS5 按认证用户名粘性会话**；`proxy.lanes` 单一声明点 + 配置校验。
- **指纹池改用公开 `geo_profile` 单一源**，并把池选中的 profile 的地理真正绑到账号上
  （此前测量了却丢掉一半）。

### 桌面端

- **协议支付命令规划器下沉 `SmsWorkbench.Contracts`**（`BackendCommandPlanner.cs`）——
  参数组装与结果 kind 不再需要 WPF。
- 主题偏好持久化；一键接码接通在线目录（余额/国家/档位/库存四项真实 API）。

## 修复问题

- **冻结配置段被 `dict` 判据读空** —— `config._freeze` 把段冻成 `mappingproxy`，而
  `isinstance(x, dict)` 恒假 ⇒ `mailbox_smailr` 整个段、`sentinel_version`、
  注册探测的 base URL 在**生产**被静默丢弃（测试传普通 dict 所以全绿）。改为 `Mapping`，
  并把契约写进 `_freeze` 的 docstring + 新增冻结配置回归测试。
- **代理池把同一出口分给两个活任务** —— 环境账本加带 TTL 的活租约；释放改为单条原子
  UPDATE（此前的「先读后写」让僵尸租约白占出口整个 TTL）。
- **`sentinel-runner.js` 的 CRLF→LF 让哈希守卫每次抛错并静默降级** —— 摘要改为按内容
  （行尾归一化）计算，并新增 `sentinel-asset-guard` 第 4 道门禁。
- **接码供应商密钥跨 section 写串** —— 设置界面下拉框与输入框是两个独立控件，改选后没有
  任何东西重新解析；改为订阅 provider 字段，下拉一变就重载。
- **`refresh_doc_symbol_lines.py` 缺 `newline="\n"`** —— 在 Windows 上把被改写文档整体翻成
  CRLF，而四道门禁全绿（行尾守卫只拒绝「混用」）。唯一判据是 `git ls-files --eol`。
- **`scan_hardcoded_secrets.py` 三类整类漏检**；文档里 16 处第三方真实明文密码脱敏。
- **测试夹具写入现役真实凭据** —— 环境账本夹具的代理出口账号/口令已改为合成占位值。
  ⚠️ 旧值仍在公开 git 历史里，**需在供应商侧轮换该出口凭据**（沿用 v2026.09.23 的提醒）。
- **UPI**：approve 重试预算 60 → 5；被丢掉的被动 hCaptcha token；被丢掉的 SetupIntent；
  `link_unverified` 细分为 `mandate_not_signed` / `payment_chain`。
- **`registration_disallowed` 的有界退避**（`create_account_disallowed_backoff`，默认关）。

## 结构性重构（无行为变更）

- **`registration_handlers.py` 1552 → 1335 行**：in-flow 挑战 hook、checkpoint/恢复、
  Sentinel 发放分别落到 `registration_edge_challenge.py` / `registration_resume.py` /
  `registration_sentinel_stages.py`，类上保留薄委托（沿用 `registration_otp_stages` 先例）。
- **`auth_flow` 拆为包**（`steps` / `signup` / `login` / `otp` / `totp` / `sentinel_flow` /
  `deps`），并新增协议注册契约文档。
- **`upi_link` 按依赖 DAG 拆为分层包**（`pipeline` 1824 → 585 行；`stages` 承载阶段函数）。
- **`pay_link` 删 138 处死导入**；适配器共用 `_prepare` / `_finish_extractor`。
- **身份收敛 / `phone_reuse` 拆分**（config / pool / lifecycle），身份只分配一次。
- `docs/audits/plan-2026-10-08-paypal-family-boundaries.md` 记录 PayPal 家族的边界设计
  （P0：`paypal_extract.py` 已成 UPI 也依赖的事实共享库）。**仅设计，未实现。**

## 门禁与工具

- 新增 ratchet：`import_layer_ratchet`（逐对冻结跨目录模块级边）、`provider_decoupling_ratchet`、
  `endpoints_literal_ratchet`、`delayed_import` / `unused_import` / `bare_print` / `config_key` /
  `mailbox_private_import`，以及 `sentinel-asset-guard`、`precommit-guard`、`line-ending-guard`。
- `docs_consistency_scan` 补文档指针与 `.cs` 存在性检查；`test_audits_readme_index` 钉住索引计数。
- pi-lens MCP 改为项目级 `.pi/mcp.json`。

## 验证

- `pytest -q`：**6133 passed / 8 skipped**（1155 subtests）。
- `ruff check` + `ruff format --check`（改动文件全绿）。
- 6 个 ratchet + `docs_consistency_scan` + `test_audits_readme_index` 全绿。
- WPF 由 `SmsWorkbench/build_dotnet.ps1` 编译到 `dist/net10/`（唯一编译入口）。

## 已知事项

- **协议注册的成功率仍未建立**：本轮的线上执行（同一出口/窗口/形状）证明
  `email_verification_mode` 不是判据，真正相关的是服务端有没有挂
  `passwordless_email_otp_send_pending`；任何结论都需要 ≥ 30/臂的受控样本。
- **ReMail iCloud 库存极紧**（单笔约 30% 成功、批量常常全部 `insufficient_inventory`）。
- 大量测试桩使用 `SimpleNamespace` / `Mock`，pyright 在这些文件上报既存的
  `reportAttributeAccessIssue`（仓库无 `pyrightconfig.json`，属历史状态）。

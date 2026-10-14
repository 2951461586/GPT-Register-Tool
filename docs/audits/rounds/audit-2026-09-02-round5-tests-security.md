# 第五轮审计 · 测试质量 + 安全卫生 + 依赖与仓库工程

审计日期：2026-09-02 · 范围：Python + C#/WPF · 方法：AST 静态分析 + 实测验证
排除：dist/ runtime/ scripts/installer/ tests/**/bin tests/**/obj .venv/ __pycache__/（排除后 348 个 .py：sms_tool 181 / tests 130 / scripts 19 / services 15 / 根 3）

**基线（先说好消息）**：`pytest` 收集 1372 个用例，**1372 passed / 0 failed / 93.5s**。测试卫生指标良好——0 个空测试、0 个无理由 skip、0 个裸 `pytest.raises`、0 个 try/except 吞测试体、sleep 仅 4 处（0.02–0.4s）、无真实网络、无未设种随机。本轮问题集中在**覆盖率盲区**和**脱敏覆盖度**，不是测试写法。

---

## A. 测试质量

### A1 [高] 支付抽取器只测了「导入面契约」，1.3 万行解析逻辑行为零测试
`tests/test_extractors_contract.py:1-152`（仅 152 行 / 12 用例）的 docstring 自述：*"only pure helpers and the shared reporter are exercised"*。它覆盖的 6 个模块共 **12,981 行**：blik 3792、ideal 3197、twint 3184、direct_card 1174、pix_core 839、pix_extract 795。
前几轮报的「5 个抽取器零测试」**已补契约测试但非行为测试**。后果：QR/金额/状态解析回归静默进生产。
改法：每渠道补 golden-fixture（HTML/QR 样本 → 期望字段）解析测试。

### A2 [高] PayPal 资金主干零测试——比抽取器更严重的盲区
以下模块**0 个测试文件引用**：

| 文件 | 行数 |
|---|---|
| `sms_tool/paypal_link/gen_link.py` | 1255 |
| `sms_tool/paypal_reverse.py` | 1145 |
| `sms_tool/captcha_solver.py` | 755 |
| `sms_tool/paypal/orchestrator.py` | 477 |
| `sms_tool/paypal/form_steps.py` | 432 |
| `sms_tool/paypal/dom_fields.py` | 429 |
| `sms_tool/paypal/flow_steps.py` | 355 |
| `sms_tool/pp_link_helpers.py` | 330 |
| `sms_tool/nodriver_paypal.py` / `nodriver_captcha.py` | 493 / 240 |

合计约 **6,361 行**花钱路径无测试。改法：先给 `gen_link` / `orchestrator` / `paypal/form_steps` 补状态机级测试。

### A3 [中] 安全闸门脚本自身零测试
`scripts/scan_release_payload.py`（191 行，前几轮的发布包凭据闸门）、`scripts/scan_hardcoded_secrets.py`（138）、`scripts/sensitive_field_scan.py`（117）——全部 0 测试。闸门自己漏检无人知晓。

### A4 [中] protocol-payment 三个入口零测试
`services/protocol-payment/momo/ac_paylink_core.py` 851 + `momo/run_momo.py` 156 + `pix/run_pix.py` 82 = **1,089 行**。

### A5 [低] 3 个无断言测试
- `tests/test_proxy_routing_config.py:15` — 调 `validate_config(config)` 不断言；函数退化为 no-op 也照样过。
- `tests/test_payment_proxy_health.py:85` — 连调 4 次不断言（"不抛异常"即通过）。
- `tests/test_config_runtime.py:15` — 靠 `subprocess.run(check=True)` 隐式断言，且起子进程拖慢套件。

### A6 [低] 16 处弱断言
`self.assertTrue(ok)` 裸真值 12 处（`test_protocol_payment_contract.py:36,74,75`、`test_registration_concurrency.py:68,80`、`test_store_modules.py:132` 等）；`is not None` 型 4 处（`test_registration_stage_concurrency.py:61,177`、`test_browser_pool.py:337`、`test_account_recovery.py:653`）。

### A7 [低] 重度 mock 造成假信心
`tests/test_external_registration_drivers.py:1046`（31 个 patch）、`:1153`（30）、`:1104`（30）、`:1239`（15）、`:1187`（15）；全仓 **103 个测试 patch ≥4**。这些用例质量其实不差（断言了 `poll_otp.call_count`、状态机 history），但验证的是**编排状态机**，不是真实 Playwright 交互——选择器/浏览器 API 改了不会红。

### A8 [信息] 小瑕疵
3 个测试文件带 UTF-8 BOM：`tests/test_mail_otp_web.py`、`tests/test_phone_proxy.py`、`tests/test_workspace_scan.py`。

**已闭环核实**：`store/` 持久化层**已有** `tests/test_store_modules.py`（前几轮"零测试"已修）；测试不再往 `runtime/` 落文件（已改 `tmp_path`，见 `tests/test_precommit_guard.py:28` 注释）。

---

## B. 安全卫生

### B1 [高] sanitizer 不脱敏 cookie——本项目最核心的凭据载体
`sensitive_policy.json:6-11` 的 42 个 `sensitive_keys` 无 `cookie`/`cookies`，`sensitive_key_fragments` 也无 `cookie`。**实测（假值）`sanitize({"cookie": ...})` 原样输出**。代码里 cookie 相关标识 60+ 处：`_cookie_header`、`cookie_str`、`import_cookie_header`、`add_cookies`、`missing_session_cookie`、`add_device_cookie`、`_set_oai_did_cookie`、`cookie_name`、`find_session_cookie`。
后果：session cookie 进日志/JSONL 报告/桌面 IPC 即泄露。

### B2 [高] `session_id` 族不脱敏
`sensitive_keys` 只有 `session_token`/`sessionToken`。实测 `session_id`、`sessionId`、`elements_session_id`、`client_session_id` 全部泄漏。

### B3 [中高] fragments 缺 `key`/`api_key` → 厂商密钥名逃逸
`sensitive_keys` 有精确 `api_key`，但代码用的是带前缀的：

| 标识符 | 位置 |
|---|---|
| `smsbower_api_key` | `sms_tool/phone_registration.py`、`phone_reuse.py`、`registration.py` |
| `smailr_api_key` | `sms_tool/account_email_change.py`、`mailbox_smailr.py` |
| `remail_api_key` | `sms_tool/account_email_change.py`、`mailbox_remail.py` |

精确匹配不上、fragment 又没有 `key` → 实测三者全部泄漏。另有 `private_key`、`oauth_code`、`webhook_url` 同样泄漏。

### B4 [中] URL query 里的凭据不脱敏
实测 `?token=XXX` 不脱敏——`named_secret` 正则只列了 `access_token`/`refresh_token`/`id_token`/`session_token` 等具名形式，**没有裸 `token`**。`?api_key=` 能中（靠具名），`?token=` 漏。

### B5 [中] `set-cookie` 响应头行 / `mailbox_line` 不脱敏
`set-cookie: sid=...` 实测泄漏（请求头 `Cookie:` 只是碰巧因含 `session-token` 字样被抓到）。`mailbox_line` 在 `sensitive_options` 里但不在 `sensitive_keys` → 作为 dict key 泄漏。

### B6 [中] `proxy` 字段非 URL 形态不脱敏
`{"proxy": "口令"}` 实测泄漏；`proxy_credentials` 正则只认 `://user:pass@` 形态。

> B1–B6 合计实测 **17 处泄漏**（全部使用假值验证）。改法见文末守卫 G1/G2。

### B7 [低] C# `Process.Start(UseShellExecute=true)` 路径来自配置
`SmsWorkbench/FileLauncher.cs:16`、`SmsWorkbench/MainWindow.Helpers.cs:241,261,295`。`OpenUrl` 有 scheme 白名单（正确），但 `Open(path)` 无根目录校验；同文件 `:250 File.WriteAllText` / `:259 Directory.CreateDirectory` 直接吃 `path`。改法：`Open` 内校验 `Path.GetFullPath` 落在允许根下。

### B8 [信息] 危险 API 与路径处理——干净
- `eval` 0、`exec` 0、`os.system` 0、`pickle.load` 0、`yaml.load` 0、`subprocess(shell=True)` 0。
- `__import__` 4 处全为字面量/白名单：`sms_tool/doctor.py:112`（依赖探测）、`scripts/preflight_env.py:43`、`cross_process_gate.py:57`（`"threading"`）、`paypal_link/gen_link.py:19`。
- `chmod` 仅 2 处且正确：`agent_identity.py:566`（0o600）、`scripts/install_git_hooks.py:57`（0o755）。
- 生产代码 **0 处**动态拼接路径进 `open()`；临时文件全部 `mkstemp`/`mkdtemp`（随机名，无可预测名问题）；无写入系统目录。

---

## C. 依赖与仓库工程

### C1 [中] `nodriver` 未进 constraints.txt
`requirements.txt:27` 声明 `nodriver>=0.45`，但 `constraints.txt` 无对应 pin。且它是**函数内延迟 import**（`sms_tool/nodriver_captcha.py:31`），装完不报错，只在 PayPal 走 nodriver 兜底时才炸——正是 requirements 注释里承认过的那类坑。改法：`constraints.txt` 补 `nodriver==<版本>`。

### C2 [中] 僵尸依赖：`selenium` 全仓 0 引用、`httpx` 0 实际使用
`requirements.txt:17` `selenium>=4.20.0`——全仓 grep **0 命中**（连注释都没有）。`httpx` 亦仅 2 处字面提及、无真实调用。后果：白增安装体积与 CVE 订阅面，无人使用却要跟安全公告。改法：删除；若确需保留则补测试使其名副其实。
（反向检查：代码 import 但未声明的依赖 **0 个**——此前报的 17 条全是 `sys.path.insert` + 裸导入的第一方模块，误报。）

### C3 [中] `config.example.json` 与实际 config 漂移
- **56 个键**在 example 里、本地 config 里没有：含 `proxy.browser_registration_pool`、`proxy.health`、`proxy.protocol_registration_pool`、`registration.humanize`、`timeouts.http_retries`、`account_health.workers` 等。
- **61 个键**本地 config 有、example 未文档化：`kakao.stage_proxies`、`momo.stage_proxies`、`omakse.*`、`account_health.use_registration_affinity`。

后果：新人照抄 example 拿不到代理泳道配置，且 `validate_config` 不报缺失，静默走默认。

### C4 [低] 无 `tests/conftest.py`
130 个测试文件各自 `sys.path.insert(0, ROOT)` 或 `parents[1]`，路径注入逻辑重复 130 份，新增子包要改多处。

### C5 [信息] 仓库体积与凭据入库——健康
`git ls-files` 共 **539** 个文件。`sessions/` **0** 入库、`runtime/` **0** 入库、`dist/` **0** 入库（`scripts/installer` 仅 4 个源文件）。二进制仅 4 个图标资源。

根目录凭据载体状态（**内容未读取**）：

| 文件 | tracked | ignored | 结论 |
|---|---|---|---|
| `config.json` `proxy.json` `runtime.json` `payment.json` | no | YES | 正确 |
| `session.json` `mailbox_tokens.txt` `skills-lock.json` | no | YES | 正确 |
| `payment_methods.json` | **YES** | no | 已核实**无**敏感键、无凭据形态命中，可保留 |
| `sensitive_policy.json` `config.example.json` | **YES** | no | 应入库，正确 |

---

## 能自动化的守卫（全部写成 pytest 用例，不改 workflow）

CI 的 gh token 缺 workflow scope，**以下全部落地为 `tests/` 下的用例**，随现有 1372 个一起跑：

| # | 建议用例 | 防什么复发 |
|---|---|---|
| G1 | `test_sanitizer_coverage.py`：用假值喂 `sanitize()`/`sanitize_text()`，断言 cookie / session_id / `*_api_key` / mailbox_line / `?token=` / set-cookie 全部命中 `[REDACTED]` | B1–B6 |
| G2 | `test_sensitive_policy_completeness.py`：AST 扫源码提取 dict 键与属性名，断言每个凭据形态标识符都落在 `sensitive_keys` 或 `sensitive_key_fragments` 内 | B1–B3（新增字段漏配） |
| G3 | `test_dependency_declaration.py`：双向断言——第三方 import 全在 requirements.txt；requirements 里的包至少有 1 处真 import | C1 / C2（selenium、httpx 立刻红） |
| G4 | `test_config_example_parity.py`：断言 `config.example.json` 键集 ⊇ `validate_config` 读取的键，且与 `config.json` 结构差 ≤ 阈值 | C3 |
| G5 | `test_repo_hygiene.py`：断言 `git ls-files` 中无 `sessions/`、`runtime/`、`dist/` 条目，且无 >1MB 二进制 | C5（回归护栏） |
| G6 | `test_assertion_density.py`：AST 扫 `tests/`，断言每个 test 函数至少 1 个真实断言，且 patch 数 >8 的用例占比不超阈值 | A5 / A7 |
| G7 | `test_security_gates_selfcheck.py`：给三个 scanner 喂含已知假凭据的 fixture，断言命中 | A3 |

优先级：**G1/G2 最高**（脱敏是本项目唯一已发生过真实事故的领域，且当前实测 17 处泄漏）。

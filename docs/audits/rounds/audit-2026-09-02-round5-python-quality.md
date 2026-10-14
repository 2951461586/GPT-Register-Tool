# 第五轮 · Python 侧代码级质量审计

- **范围**：生产代码 = `sms_tool/`(181) + `services/`(15) + 根目录(3) = **199 个 .py / 80048 行**（注：上一稿写 197，系 181+15+3 算错）；`tests/`(130)、`scripts/`(25) 单列，不混入生产结论。
- **方法**：全部结论由 `ast` + `tokenize` + `difflib` 产出（非 grep）。复核脚本 `F:/tmp/audit5/v1_core.py`、`v2_size_dup.py`、`v3_clone_verify.py`、`v4_hardcode.py`、`v5_refine.py`；原始清单 `out_v1_silent.txt`、`out_v1_typing.txt`、`out_v2_clones.txt`、`out_v2_funcs.txt`、`out_v1_sens.txt`。
- **已排除源码副本**：`dist/`、`runtime/`、`.venv/`、`__pycache__/`、`sessions/`、`scripts/installer/`、`**/bin`、`**/obj`、`site-packages`。
- **环境**：`.venv` Python 3.11.8；全项目 354 个 .py 解析 100% 成功。

## 对上一稿的修订（本轮新增价值集中在纠错）

| 项 | 上一稿 | 本轮实测 | 判定 |
|---|---|---|---|
| 生产文件数 | 197 | **199 / 80048 行** | 上一稿算错 |
| ideal↔twint 相似度 | Jaccard **1.00**；diff 521/3190（84% 相同） | token Jaccard **0.936** / difflib **0.918**；归一化渠道名后 diff **175/3197 ≈ 94.5% 相同** | **严重低估**，实际更接近全等 |
| blik↔ideal 相似度 | 0.81 | token **0.602** / difflib **0.664** | **高估**；blik 只是「部分克隆」 |
| momo/kakao 计入冗余 | 与前三文件合计 11692 行 | Jaccard **0.02–0.10**，非克隆 | **错误归类**，应剔除 |
| 完全相同的克隆函数 | "至少 45 组 Jaccard=1.00"（含 172L 大函数） | **53 组真·token 全等，最大仅 47L**；大函数为 0.62–0.999 相似而非全等 | 上一稿把「高相似」写成「全等」 |
| raise-from | "18 处（15 需改）" | **14 处有 `as exc` 绑定**（4 处 SystemExit 可豁免→实为 10）；另 **5 处无绑定** | 拆分错误，见 P1-1 |
| 静默 except | 584（pass 268 + return 316） | **595**，应拆三类（见 P0-1） | 分类过粗 |
| `except BaseException` | 正文称 7 处 | **6 处**（正文只列了 6 个） | 计数错 |
| 「不该拆」名单 | 含 `run_phone_register`、`build_session_file` | 二者是**纯逻辑函数**（CC=49 / CC=71，数据占比 0.10 / 0.08） | **错误豁免** |
| 漏报 | — | `momo/momo_qr_extract.py`(2046L，含 CC=82 函数) | 本轮补上 |

---

## P0

### P0-1 静默吞噬异常：595 处，其中 242 处连异常对象一起丢弃

按语义拆分（这是上一稿缺失的关键区分）：

| 类别 | 处数 | 说明 |
|---|---|---|
| A. `except: pass` | 216 | 仅少数是 finally 清理（`captcha_solver.py` 13、`playwright.py` 17） |
| B. `return/continue/break` 且**完全丢弃异常** | **242** | 最危险：异常对象无任何留存 |
| C. `return` 但 `str(e)` 进了返回值 | 82 | 信息未丢，但**零日志**，事后不可追溯 |

捕获裸 `Exception`（或含 `Exception` 的元组）**461 处**。

**文件:行号**（B 类典型，逐条实测）：

- `services/protocol-payment/direct_card/direct_card_extract.py:568` `except (json.JSONDecodeError, TypeError, ValueError): return {}` —— 支付页 JSON 解析失败返回空 dict，下游按「解析成功但无字段」继续跑，可能生成错误订单。
- `services/protocol-payment/kakao/kakao_extract.py:213` `return ''`、`:757` `return ''`、`:689` `return (False, str(exc)[:180])`。
- `services/protocol-payment/momo/momo_qr_extract.py:228` `return (None, None)`、`:316` `return False`、`:1044` `return ('', 'pm_bad_json')`。
- `sms_tool/paypal/dom_fields.py:56, 61, 72, 85` —— `_set_field_value` / `_click_with_fallback` / `_fill_with_fallback` 全部 `except Exception: pass|continue`。PayPal DOM 一改版，所有选择器静默失配并统一返回 `False`，上层无法区分「字段不存在」与「页面改版」，注册在支付环节无声失败。
- `services/mail-otp-web/app.py:83` `return {}`。

TOP 文件：`registration_drivers/playwright.py`(40)、`agent_identity.py`(18)、`paypal/dom_fields.py`(18)、`codex_oauth.py`(16)、`captcha_solver.py`(14)、`external_sessions.py`(14)、`nodriver_paypal.py`(13)、`sentinel_tokens.py`(13)、`momo_qr_extract.py`(12)。

**建议**：分三类处置，禁止一刀切。① finally 清理（`captcha_solver.py:450/455/460`）→ `contextlib.suppress(Exception)`；② 探测/回退循环（`dom_fields.py` 系列）→ 至少 `logger.debug("selector %s failed: %s", selector, exc)`；③ 关键路径（B 类 242 处）→ `logger.exception()` + 返回带 `error` 原因的结果对象，禁止裸 `False`/`{}`。

### P0-2 ideal / twint 是同一文件的两份副本（约 3000 行冗余）

**文件**：`services/protocol-payment/ideal/ideal_qr_extract.py`(3197L)、`twint/twint_extract.py`(3184L)

把渠道名归一化后 `diff` 仅 **175 行不同（94.5% 相同）**。同名函数相似度（difflib / token Jaccard）：

| 函数 | ideal↔twint | LOC |
|---|---|---|
| `create_checkout` | 0.999 / 0.997 | 102 |
| `remove_failed_proxies` | 0.998 / 0.993 | 38 |
| `poll_payment_page` | 0.997 / 0.992 | 65 |
| `approve_with_retry` | 0.996 / 0.987 | 83 |
| `update_checkout_promotion` | 0.996 / 0.985 | 44 |
| `run_attempt` | 0.996 / 0.989 | 65 |
| `run_single_link_attempt` | 0.981 / 0.950 | 115 |
| `run_single_link_mode` | 0.980 / 0.925 | 150 |
| `run_provider_flow` | 0.979 / 0.936 | 172 |

已出现漂移：`ideal_qr_extract.py:1797 stripe_confirm_ideal` → `twint_extract.py:1784 stripe_confirm_twint` 是同一份 50L 代码改名。

**blik 是部分克隆，不要同等对待**：`blik/blik_qr_extract.py`(3792L) 与 ideal 的 Jaccard 仅 0.602。工具层与 ideal/twint 全等（`create_checkout` 1.000、`stripe_update_customer_data` 47L×3 份全等、`stripe_update_tax_region` 46L×3 份全等），但编排层已独立演化：`run_provider_flow` ideal/blik 仅 **0.196**、`remove_failed_proxies` 0.638、`run_once` 0.643。

**新发现（上一稿未报）**：`blik_qr_extract.py:2195` 定义了 `stripe_confirm_ideal`（ideal 渠道实现副本）并被 `:2928` 调用；blik 自己的 `stripe_confirm_blik` 在 `:2239`、被 `:2858` 调用。同一文件并存两个渠道的确认实现，是渠道串味的温床。

**momo / kakao 不是克隆**（Jaccard 0.02–0.10），上一稿把它们计入冗余行数是错的。

**建议**：`services/protocol-payment/common/` 已有 `extractor_helpers.py`、`protocol_core.py`。新建 `common/stripe_checkout_flow.py`，先合并**已 token 全等的 53 组小工具函数**（见 `out_v2_clones.txt`，最大 47L，纯机械上提、零风险），再合并 ideal↔twint 的 9 个大函数（差异仅是渠道钩子）。blik 只上提工具层，编排层保持独立。

---

## P1

### P1-1 `raise ... from` 缺失，堆栈链断：19 处（上一稿拆分有误）

必须区分两种，修法不同：

**A. 已绑定 `as exc`，可机械加 `from exc` —— 14 处**（4 处是 CLI `SystemExit`，可豁免 → **实为 10 处**）：

`sms_tool/k12_client.py:44, 89`（→ `RuntimeError("session refresh/fetch transport: ...")`）、`sms_tool/paypal_reverse.py:315, 1097`（→ `_NeedBrowserFallback`）、`services/protocol-payment/blik/blik_qr_extract.py:2601, 2618`、`ideal/ideal_qr_extract.py:2237, 2253`、`twint/twint_extract.py:2224, 2240`（→ `RuntimeError("checkout_not_active_session")`）。

**B. 未绑定，需先加 `as exc` —— 5 处**（上一稿误列为「缺 from」，实际连异常对象都没拿到）：

`sms_tool/captcha_solver.py:324, 490, 606`、`sms_tool/registration_drivers/playwright.py:552`、`sms_tool/cross_process_gate.py:141`。

**后果**：`_NeedBrowserFallback` 是控制流信号，断链后无法判断是哪一步触发浏览器回退；`k12_client` 断链后 `is_transient_transport_error` 的判定依据不可追溯。

**豁免**：`commands/mailbox_ops.py:86, 144`、`commands/payment.py:676`、`momo/run_momo.py:156` 是 CLI `SystemExit` 出口。

### P1-2 端点 URL 硬编码：88 个字面量 / 402 次出现，**且没有 endpoints 模块**

| URL | 文件数 | 次数 | 内联 |
|---|---|---|---|
| `chatgpt.com` | 30 | 78 | 70 |
| `chatgpt.com/checkout` | 12 | 23 | 23 |
| `auth.openai.com` | 12 | 16 | 16 |
| `chatgpt.com/backend-api/payments/checkout` | 11 | 23 | 19 |
| `api.stripe.com/v1/payment_pages` | 10 | 34 | 34 |
| `api.openai.com/auth` | 9 | 18 | 18 |
| `checkout.stripe.com` | 8 | 15 | 15 |
| `pay.openai.com/c/pay` | 6 | 7 | 6 |
| `chatgpt.com/backend-api/sentinel/ping` | 5 | 8 | 8 |

跨 ≥2 文件 35 个，跨 ≥5 文件 16 个。**「压根没建」型确证**：全仓库无 `endpoints`/`urls`/`routes` 模块，`sms_tool/` 下仅 `store/constants.py`。

超时魔法值同样散落：`timeout=30`×26、`timeout=5000`×22、`timeout=3000`×16、`timeout=10`×11；`DEFAULT_TIMEOUT=30` 在 **10 个文件**各定义一遍、`CHATGPT_TIMEOUT=45` 在 4 个文件重复。

**建议**：新建 `sms_tool/endpoints.py` + `services/protocol-payment/common/endpoints.py`；超时收敛为 `TimeoutPolicy` dataclass（**注意单位**：Playwright 是 ms，requests 是 s，不可统一数值）。

### P1-3 渠道字符串硬编码 —— 属「已有 catalog 但没用满」

`sms_tool/payment_catalog.py` 存在且被 **16 个文件 import**（不是没人用），但字面量仍散落：`paypal` 29 文件/66 次、`upi` 6/28、`blik` 7/26、`momo` 8/22、`ideal` 5/15、`kakao` 7/13、`gcash` 5/12、`pix` 4/11、`twint` 4/9、`k12` 2/12。主散落点：`pay_link/adapters.py`、`store/normalize.py`、`commands/payment.py`、`payment_flow.py`。**建议**：在 `payment_catalog.py` 内导出 `PaymentMethodKey` 常量，其余文件改 import。

### P1-4 超大函数：LOC≥80 且 CC≥15 且非数据表 = **118 个**

| 函数 | 位置 | LOC | CC | 数据占比 |
|---|---|---|---|---|
| `run_payment_batch` | `sms_tool/payment_batch.py:80` | 492 | **155** | 0.15 |
| `run_browser_registration` | `sms_tool/registration_drivers/playwright.py:1559` | 457 | **117** | 0.15 |
| `generate_upi_qr_link` | `sms_tool/upi_link.py:365` | 367 | 99 | 0.19 |
| `main` | `sms_tool/cli.py:397` | 357 | 104 | 0.05 |
| `run_one` | `sms_tool/payment_batch.py:235` | 269 | 90 | 0.17 |
| `plan` | `sms_tool/payment_routing.py:246` | 210 | 89 | 0.10 |
| `run_phone_register` | `sms_tool/phone_registration.py:31` | 317 | 49 | 0.10 |
| `generate_pp_link` | `sms_tool/paypal_link/gen_link.py:525` | 313 | 79 | 0.05 |
| `validate_config` | `sms_tool/config.py:366` | 171 | 76 | 0.08 |
| `build_session_file` | `sms_tool/session_builder.py:20` | 141 | **71** | 0.08 |
| `extract_momo_qr_payload` | `services/.../momo/momo_qr_extract.py:836` | 149 | 82 | 0.06 |
| `probe_account` | `services/.../momo/momo_qr_extract.py:1642` | 297 | 53 | 0.29 |

**确属数据表、不该拆**（已实测其体积是大 ClassDef）：`paypal_reverse.py`（`PayPalReverseClient` 1008L / 占 88%）、`registration_handlers.py`（`RegistrationEmailWorkflow` 961L / 86%）、`wallet_transport.py`（289L / 85%）、`mailbox_types.py`（24L / 86%）、`mailbox_service.py`（71L / 86%）。

**但上一稿错误豁免了两个**：`run_phone_register`（CC=49）与 `build_session_file`（CC=71）是纯逻辑函数，数据占比仅 0.10 / 0.08，同样该拆。

### P1-5 `print()` 混在库代码：生产 591 处 / 67 文件

TOP：`paypal_reverse.py`(38)、`nodriver_paypal.py`(31)、`phone_reuse.py`(31)、`registration_handlers.py`(31)、`commands/payment_links.py`(30)、`sentinel_tokens.py`(27)、`nodriver_captcha.py`(25)、`mailbox_parsers.py`(21)、`account_scan.py`(20)、`paypal/form_steps.py`(20)。

**后果**：绕过 `logging_setup.py`，不受 level/rotation/redaction 控制，无时间戳、无级别、无模块名。

**建议**：按文件分批替换为 `logger = logging.getLogger(__name__)`；`cli.py`、`__main__.py`、`commands/*` 保留 `print` 合理。

---

## P2

### P2-1 logger 定义不一致：全项目仅 16 处

- 11 处正确用 `__name__`：`account_2fa.py:23`、`browser_fingerprint_pool.py:46`、`browser_pool.py:27`、`captcha_solver.py:25`、`chatgpt_bootstrap.py:38`、`humanize.py:32`、`mailbox_poll.py:14`、`mailbox_remail.py:21`、`paypal_protocol.py:23`、`registration_drivers/stealth.py:16`、`sentinel_quickjs.py:43`
- 5 处硬编码名：`proxy_bridge.py:52`(`"proxy_bridge"`)、`proxy_pool.py:25`(`"proxy_pool"`)、`start_proxy_pool.py:23`(`"proxy_pool"`，与前者**同名会串扰配置**)、`pay_link/base.py:170`(`_LOGGER`，变量名也不一致)、`logging_setup.py:54`（取 root，刻意保留）

### P2-2 f-string 进 logger：3 处

`sms_tool/account_2fa.py:261, 346, 353`。前两处把**邮箱地址（PII）**写进 INFO 日志。改为 `logger.info("[2FA] TOTP enrolled for %s", email)` 并考虑掩码。

> **敏感信息结论（健康项）**：全量扫描 logging 调用参数，**未发现** token/password/cookie/Authorization 头/含 query 完整 URL 进日志。初筛 15 条经复核均为 `authenticated`、`re-auth` 等词的子串误报。与前四轮「磁盘与凭据泄漏」结论一致。

### P2-3 `typing` 旧式泛型：57 文件 / 81 处

涉及 `Callable/Mapping/Iterable/Sequence/Generator` → `collections.abc`；`Optional[X]` → `X | None`。完整清单见 `out_v1_typing.txt`。

### P2-4 UTF-8 BOM：5 文件

`sms_tool/codex_oauth.py:1`、`sms_tool/phone_proxy.py:1`、`tests/test_mail_otp_web.py:1`、`tests/test_phone_proxy.py:1`、`tests/test_workspace_scan.py:1`。CPython 可编译，但以 `encoding="utf-8"` 读取的工具（含本轮审计脚本、部分 linter）会报 `SyntaxError: invalid non-printable character U+FEFF`。

### P2-5 unittest 与 pytest 并存

`tests/` 下 **139 个 `unittest.TestCase` 子类** + 2655 处 unittest 风格断言（`assertEqual`×1759、`assertTrue`×407、`assertFalse`×202、`assertIn`×164、`assertIsNone`×51、`assertRaises`×50），跑在 pytest 下。这是既定风格而非残留，**不建议批量改造**（高风险低收益）；新测试用 pytest 函数式 + 裸 `assert`。

---

## 前四轮结论澄清（不重复报，仅纠偏）

- **`except BaseException` 不是缺陷，共 6 处**（上一稿称 7）：`config.py:108`、`phone_reuse.py:55`、`pay_link/core.py:180`、`payment_operation.py:168`、`registration_concurrency.py:197`、`proxy_bridge.py:170`。全部是「清理后重新抛出」的正确惯用法，**不应改成 `except Exception`**，否则 `KeyboardInterrupt`/`SystemExit` 会漏掉锁或残留临时文件。
- **现代化项已清理干净**（实测均 0 处）：`datetime.utcnow()`、`datetime.utcfromtimestamp()`、`asyncio.get_event_loop()`、`imp`/`distutils`。
- `os.path` 仅 40 处（生产），不值得强迁 `pathlib`。

---

## 可批量机械修复清单（低风险，可脚本化）

| # | 修复 | 规模 | 做法 | 风险 |
|---|---|---|---|---|
| 1 | **上提 53 组 token 全等克隆函数** | 53 组 / ~1705 行冗余 | 按 `out_v2_clones.txt` 直接上提到 `common/stripe_checkout_flow.py`，最大仅 47L，纯搬运 | 极低（**建议优先做**） |
| 2 | `typing` → `collections.abc` / PEP 604 | 57 文件 / 81 处 | `from typing import Callable, Mapping` → `from collections.abc import ...`；`Optional[X]` → `X \| None`（3.11 无需 `__future__`） | 低（纯 import 行替换） |
| 3 | 剥离 UTF-8 BOM | 5 文件 | 按 `utf-8-sig` 读入、`utf-8` 写回 | 极低 |
| 4 | 硬编码 logger 名 → `__name__` | 4 处 | `proxy_bridge.py:52`、`proxy_pool.py:25`、`start_proxy_pool.py:23`、`pay_link/base.py:170`（`_LOGGER`→`logger`） | 低（需同步改 handler/filter 名） |
| 5 | f-string → `%s` 惰性格式化 | 3 处 | `account_2fa.py:261/346/353` | 极低 |
| 6 | `raise ... from exc`（A 类） | 10 处 | 在已绑定 `as exc` 的 `raise X(...)` 末尾加 `from exc` | 低 |
| 7 | `raise ... from exc`（B 类） | 5 处 | 先 `except X as exc:` 再加 `from exc`；`captcha_solver.py:324/490/606`、`playwright.py:552`、`cross_process_gate.py:141` | 低 |
| 8 | URL 字面量 → 常量 | 88 字面量 / 402 次 | **先**建 `sms_tool/endpoints.py` + `services/.../common/endpoints.py`，再按上表分批替换 | 中（按 URL 分批，每批跑 `test_paypal_*` + `test_extractors_contract.py`） |
| 9 | `print` → `logger` | 591 处 | 按文件分批；先加 `logger = logging.getLogger(__name__)`，再 `print(` → `logger.info(` | 中（需人工定级 info/debug/warning，确认无下游解析 stdout） |
| 10 | 超时魔法值 → `TimeoutPolicy` | ≥130 处 | 收敛 `timeout=30/5000/3000/10` 及 10 份重复的 `DEFAULT_TIMEOUT=30` | 中（先确认单位：Playwright ms vs requests s） |

**不建议机械处理**：P0-2 的 ideal↔twint 大函数合并（需重新设计渠道钩子）、P0-1 的 595 处异常吞噬（必须按 pass / 丢弃 / 携带三类人工分类）、P1-4 的超大函数拆分（需先补测试）、P2-5 的 unittest 风格迁移。

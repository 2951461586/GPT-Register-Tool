# 第五轮架构审计：耦合与解耦机会

**范围**：Python 196 模块 / 79,795 行（`sms_tool/` 181 + `services/` 15）；C# 75 文件 / 13,812 行（`SmsWorkbench/` + `SmsWorkbench.Contracts/`）。
已排除 `dist/`、`runtime/`、`.venv/`、`__pycache__/`、`sessions/`、`logs/`、`scripts/installer/`、`bin/`、`obj/`（未排除会膨胀到 1088 个 .py）。
**方法**：全部结论由 AST 导出，脚本在 `F:\tmp\audit5\r5\`，未修改仓库任何文件。
**注**：`sms_tool/phone_proxy.py`、`sms_tool/codex_oauth.py` 带 UTF-8 BOM，用默认 `utf-8` 解析会静默失败（`ast.parse` 抛 `invalid non-printable character U+FEFF`），本轮用 `utf-8-sig` 修复——此前基于默认编码的工具链对这两个模块的分析结论（扇入 23 / 8）可能是错的。

---

## 1. 反向依赖与分层倒置（最高收益）

**一句话**：7 个 import 环、33 个模块卷入、319 处函数级延迟内部导入（分布在 80 个模块）——环是靠「把 import 挪进函数体」糊住的，不是解开 的。

### 1.1 环清单（Tarjan SCC，含延迟导入）

| # | 规模 | 环路径（文件:行号） | 性质 |
|---|---|---|---|
| A | 11 | `payment_link_manager:48,49 → pay_link` ↔ `pay_link/{adapters,base,core,normalize,persistence,registry}:4 → payment_link_manager`；另 `pay_link.registry:44,62 → gen_pp_link:6,7 → paypal_link → paypal_link.gen_link:1225 → payment_link_manager` | **机械拆分残留，纯人造环** |
| B | 7 | `storage.py:7,8 → store` → `store.accounts:143 → mailbox_remail:165 → storage`；`store.connection:47,79 → storage（导入私有 `_storage`）` | 父壳 ←→ 子包互相穿透 |
| C | 4 | `config.py:179 → registration_drivers.external_sessions._driver_config`；`phone_proxy:24 → config`；`registration_drivers.{browser_session,external_sessions}:9,15 → phone_proxy` | **扇入 64 的配置总线反向依赖浏览器驱动** |
| D | 3 | `payment_catalog:193,194 → checkout_contract / payment_flow` ↔ `checkout_contract:10 / payment_flow:9 → payment_catalog` | 目录与其校验器互引 |
| E | 3 | `account_liveness:80 → registration_drivers.playwright:16` ↔ `:21,1845 → registration_outcome:15 → account_liveness` | 注册驱动与存活探测互引 |
| F | 3 | `sentinel.__init__:3 → sentinel.client:330 → sentinel_tokens:405 → sentinel` | 子包自环 |
| G | 2 | `payment_routing:216,241,406,432 → paypal_proxy:695 → payment_routing` | 路由与代理互引 |

延迟内部导入 TOP：`registration_handlers` 22、`account_recovery` 19、`cli` 19、`commands.payment` 17、`commands.accounts` 14、`registration_drivers.playwright` 11。

### 1.2 子包 → 父壳的反向边（按被依赖目标聚合）

| 父壳模块 | 反向边数 | 来源子包模块数 | 判定 |
|---|---|---|---|
| `config` | 38 | 17 | **合法**（共享配置总线），但 C 号环要修的是 config 自己 |
| `payment_routing` | 35 | 8 | 合法（支付路由是领域服务） |
| `upi_link` | 25 | 1 | **该下沉**：`paypal_link.gen_link:150` 导入 8 个私有符号（`_write_qr_png` `_upi_nested_get` `_method_cfg` `_payment_stage_proxies_from_config` …） |
| `paypal_proxy` | 24 | 3 | 合法，但 `paypal_link.gen_link:92` 导入 `_PAYPAL_PROXY_STATE_CACHE`（私有缓存）越界 |
| `pp_link_helpers` | 21 | 1 | **该下沉**：`gen_link:40` 导入 21 个符号含 `_SIDE_EFFECT_STAGES` |
| `payment_operation` / `payment_contracts` / `payment_catalog` | 20/18/18 | 7/6/6 | 合法（支付领域公共层），应正式提升为 `payment/` 子包 |
| `sanitizer` | 15 | 9 | 合法（叶子工具），但 `pay_link/*:26` 导入 `_canonical_sanitize` 私有别名→改公开名 |
| `paths` | 14 | 7 | 合法（叶子工具，扇出 0） |
| `payment_link_manager` | 13 | 10 | **必须删**：见下 |

### 1.3 A 号环：机械拆分的硬伤

`sms_tool/payment_link_manager.py`（49 行）自称「thin backward-compatibility shell」，`pay_link/` 7 个子模块每个第 4 行都写 `import sms_tool.payment_link_manager as _plm`，共 **24 处 `_plm.*` 调用**：

```
pay_link/adapters.py:40,48,60,91,161,234,369,430,477,532,587   (11)
pay_link/base.py:34,37,92,98,158                                (5)
pay_link/persistence.py:33,60                                   (2)
pay_link/registry.py:39,77,170,280                              (4)
pay_link/core.py:221  pay_link/normalize.py:244                 (2)
```

用到的符号分三类：① `_plm.subprocess` / `_plm.subprocess.TimeoutExpired`（adapters:40,48,60；normalize:244）——**纯属多余，直接 `import subprocess` 即可**；② `_plm.current_config_data` / `_plm.CATALOG_METHODS` / `_plm._protocol_cfg` / `_plm._run_extractor_subprocess` / `_plm._persist_run` / `_plm._run_wallet_adapter` / `_plm._state_path`——**全部已在 `pay_link` 内部或 `payment_catalog`/`payment_routing` 中**，改为直接导入即可；③ 7 个子模块共用同一段 18–26 行的 import 序言（逐字节复制），是拆分脚本的产物。

**改法**：① 7 个子模块删掉 `import ... as _plm`，把 24 处 `_plm.X` 换成直接导入（`subprocess` 直接 import；`_protocol_cfg` 等从 `.base` 导入；`_run_wallet_adapter` 从 `.adapters` 导入）；② 把 7 份复制的 import 序言合并进 `pay_link/base.py` 一处；③ `payment_link_manager.py` 退化为纯 re-export（`from .pay_link import *`），保持单向。

### 1.4 B 号环：`store` ←→ `storage`

`storage.py` 全文 8 行（`from .store import *`），但 `store/connection.py:47,79` 反过来 `from ..storage import _storage`（导入私有单例），`store/accounts.py:143` 又 import `mailbox_remail:165 → storage`。
**改法**：`storage.py` 只保留 re-export；`connection.py:47,79` 改为从 `store` 内部获取 `_storage`（`store/` 自己持有连接单例），`storage.py` 从 `store/__init__.py` 反向取一次即可，或直接把 `storage` 这个壳删掉、把 25 个引用方批量改到 `store`。

### 1.5 C 号环：配置总线反向插到浏览器驱动

`config.py:179` 在 `validate_registration_driver_config` 里 `from .registration_drivers.external_sessions import _driver_config`（try/except 包着）。config 扇入 64，是最底层的总线，却依赖浏览器驱动层。
**改法**：`external_sessions._driver_config` 的环境覆盖逻辑下沉到 `registration_drivers/base.py`（该模块扇出应为 0），config 只调 `registration_drivers.base.resolve_driver_config(config, name)`；若嫌重，至少把 `base.py` 提升为不依赖 `phone_proxy` 的纯函数模块，断开 `config → external_sessions → phone_proxy → config`。

---

## 2. God module 与枢纽节点

### 2.1 扇入 TOP 15（隐式全局总线）

| # | 模块 | 扇入 | 扇出 | LOC | 判定 |
|---|---|---|---|---|---|
| 1 | `sms_tool.config` | **64** | 4 | 656 | 真总线。但：`CFG`（:648）被 37 模块 / 65 处 `CFG.` 引用；**C 号环需修** |
| 2 | `sms_tool.storage` | 26 | 1 | 8 | 兼容壳，应删或冻结 |
| 3 | `sms_tool.paths` | 26 | 0 | 26 | 健康叶子（扇出 0），保持 |
| 4 | `sms_tool.sanitizer` | 24 | 0 | 106 | 健康叶子，保持 |
| 5 | `sms_tool.phone_proxy` | 23 | 3 | 549 | 较健康，但被卷入 C 号环 |
| 6 | `sms_tool.auth_headers` | 21 | 2 | 490 | 健康 |
| 7 | `sms_tool.payment_catalog` | 15 | 2 | 214 | 数据驱动（读 `payment_methods.json`），**模式正确** |
| 8 | `sms_tool`（`__init__`） | 14 | 0 | 1 | 空壳被 14 处 import，可清理 |
| 9 | `sms_tool.account_liveness` | 13 | 7 | 501 | 职责混杂（配额+探测+ID），E 号环 |
| 10 | `sms_tool.paypal_proxy` | 13 | 4 | 795 | G 号环 |
| 11 | `sms_tool.http_client` | 13 | 1 | 154 | 健康 |
| 12 | `sms_tool.payment_routing` | 12 | 4 | 610 | G 号环 |
| 13 | `sms_tool.payment_link_manager` | 11 | 11 | **49** | **49 行却有 121 的耦合积——纯粹是环的枢纽，应删** |
| 14 | `sms_tool.mailbox` | 11 | 6 | 1006 | 注册中心+适配层，1006 行偏大 |
| 15 | `sms_tool.payment_contracts` | 11 | 0 | 240 | 健康契约层 |

### 2.2 扇出 TOP 15（上帝模块）

| # | 模块 | 扇入 | 扇出 | LOC | 判定 |
|---|---|---|---|---|---|
| 1 | `sms_tool.registration` | 3 | **27** | 278 | 278 行拉 27 个模块 = 编排枢纽，可接受但需接口化 |
| 2 | `registration_drivers.playwright` | 6 | 20 | **2032** | 全仓最大模块 + 20 扇出 + 11 处延迟导入 → 状态机/页面对象/会话三分 |
| 3 | `sms_tool.cli` | 1 | 19 | 969 | 19 处延迟 import + 6 处 `global` 语句 |
| 4 | `sms_tool.account_recovery` | 6 | 17 | 944 | 17 扇出 + 19 处延迟导入 |
| 5 | `pay_link.adapters` | 3 | 17 | 602 | 含对 `_plm` 的反向依赖 |
| 6 | `pay_link.core` | 1 | 17 | 251 | 251 行 17 扇出，机械拆分产物 |
| 7 | `pay_link.registry` | 2 | 14 | 291 | 同上 |
| 8 | `sms_tool.payment_batch` | 1 | 13 | 987 | |
| 9 | `sms_tool.registration_handlers` | 1 | 13 | **1118** | 22 处延迟导入（全仓第一） |
| 10–12 | `pay_link.{normalize,persistence}` `paypal.orchestrator` | 2/2/1 | 12/12/12 | 248/63/477 | `persistence` 63 行 12 扇出 = 序言复制 |

**耦合积（扇入×扇出）TOP 10**：`config` 256 · `payment_link_manager` 121 · `registration_drivers.playwright` 120 · `account_recovery` 102 · `account_liveness` 91 · `registration` 81 · `codex_oauth` 72 · `phone_proxy` 69 · `mailbox` 66 · `pay_link.base` 66。

**最大模块**：`registration_drivers/playwright.py` 2032 · `paypal_link/reconciliation.py` 1305 · `paypal_link/gen_link.py` 1255 · `registration_handlers.py` 1118 · `codex_oauth.py` 1158 · `mailbox.py` 1006 · `payment_batch.py` 987 · `cli.py` 969。

---

## 3. 全局可变状态

**总体健康**：44 个模块级锁/信号单例；25 个「被原地修改的模块级可变绑定」中 20 个有锁保护。真正裸奔的只有 5 个。

### 3.1 无锁且被并发改写的模块级状态（必修）

| 位置 | 名字 | 修改点 | 风险 |
|---|---|---|---|
| `services/protocol-payment/momo/momo_qr_extract.py:35` | `COUNTRY_CURRENCY`（**公开名**） | `:36 setdefault()`、`:2016 item-assign` | 模块导入期自举 + 运行期改写，跨进程脚本内并发 |
| `sms_tool/desktop_read.py:140` | `_SESSION_PARSE_CACHE` | `:149 clear()` + `:150 assign`（check-then-act） | 无锁的 clear→put 序列，双线程可丢缓存/读到半态 |
| `sms_tool/desktop_read.py:141` | `_SESSION_SANITIZE_CACHE` | `:159 clear()` + `:160 assign` | 同上 |
| `sms_tool/desktop_read.py:176` | `_SESSION_STATE_CACHE` | `:204 assign` | 无锁写入 |
| `sms_tool/mailbox_chongzhi.py:29` | `_last_fetch_ts` | `:176 assign` | 无锁限流时间戳，多 worker 下失效 |

**改法**：`desktop_read.py` 三个缓存各配一个 `threading.Lock`（照抄 `browser_fingerprint_pool.py:192-215` 的 `_SHARED_POOLS` + `_SHARED_POOLS_LOCK` 写法，那是全仓正确范式）；`momo_qr_extract.COUNTRY_CURRENCY` 改 `MappingProxyType` 冻结（:36 的 `setdefault` 是导入期自举，可改成一次性构造后冻结）。

### 3.2 `global` 语句（12 模块 / 23 处）

`cli.py` 6 处（`_build_session_file` `_heavy_deps_loaded` `_load_mailbox_pool` `_mailbox_snapshot` `_remail_enabled` `run_email`）· `account_health_queue` 3（`_WINDOWS_KERNEL32` `_WORKER`）· `registration_concurrency` 2 · `registration_drivers/playwright` 2（`_BROWSER_POOL` `_BROWSER_POOL_KEY`，配 `_BROWSER_POOL_LOCK`:1488）· `blik/ideal/kakao/twint_qr_extract` 各 2（`_dump_counter` `_proxy_state` `_access_token_probe`）· `env_loader` 1 · `logging_setup` 1。
**判定**：`_heavy_deps_loaded` / `_LOADED` / `_CONFIGURED` 是一次性初始化标志，无害；`cli.py` 的 `_build_session_file`/`run_email` 是懒加载函数缓存，可用 `functools.lru_cache` 替掉；`playwright._BROWSER_POOL` 有锁但持有的是可变对象，改 `contextvars` 或显式传入。

### 3.3 伪全局

- `config.py:648 CFG: MutableMapping = LegacyConfigView()` —— 实际由 `ContextVar`（`:243 _CURRENT_CONFIG`）支撑，**不是真全局可变**，设计可接受；但它把 37 个模块 / 65 处 `CFG.x` 钉死在 config 的形状上，是解耦的隐性障碍。中长期把 65 处显式传参。
- `pay_link/base.py:166 CFG: dict[str, Any] = {}` —— 子包里**第二个** `CFG`，无导入方、无修改方 = 死全局，直接删。

---

## 4. 缺失的接口 / 协议（「加一个实现要改 N 处」）

### 4.1 已有的正确范式（照抄这三个）

| 范式 | 位置 | 固化方式 |
|---|---|---|
| 短信供应商 | `sms_provider.py:20 class SmsProviderAdapter(ABC)` → `provider_key` / `prepare()` / `wait_code()` / `complete()` / `cancel()` | **ABC**，2 个实现：`phone_reuse.py:703 _StaticSmsProviderAdapter`、`:720 _SmsBowerProviderAdapter`，工厂 `:736` |
| 支付渠道 | `payment_catalog.py:43 PaymentMethodDefinition`（数据驱动，读 `payment_methods.json`）+ `PaymentAdapterRegistry` / `FunctionPaymentAdapter` | 8 个适配器在 `pay_link/registry.py:33 build_default_payment_registry` 一次注册 |
| 邮箱策略 | `mailbox_strategies.register_message_fetcher/register_otp_poller`（`mailbox.py:67-192`） | 注册表存在，但**不彻底**，见下 |

### 4.2 缺口 1：邮箱适配器 —— 加一个要改 **5 处**（用户线索说 4 处，实际多 1–2 处）

| # | 位置 | 要改什么 |
|---|---|---|
| 1 | `mailbox.py:24-38` | 导入块（现挂 11 个 provider 模块） |
| 2 | `mailbox.py:67-192` `_register_mailbox_strategies()` | 每个 provider 两次 register，各约 13 行 lambda |
| 3 | `mailbox_parsers.py:245-256` `parse_mailbox_pool_line` | 6 分支 if 链 |
| 4 | `mailbox_parsers.py:299-326` `_parse_mailbox_token_file` | **与 #3 完全相同的 6 分支 if 链，逐行复制粘贴** |
| 5 | `mailbox_strategies.py:183` `_graph_matcher` | 硬编码排除集 `{"cfworker","remail","smailr","icloud","icloud_url","gmail","chongzhi"}` |
| (+1) | `account_email_change.py:119-123` | 又一份独立 4 分支 provider 分发（带延迟 import） |

现有实现方（9 个，证明契约真实存在）：`mailbox_cfworker` `mailbox_remail` `mailbox_smailr` `mailbox_icloud_url` `mailbox_gmail` `mailbox_chongzhi` `mailbox_graph` + `mailbox_strategies._graph_*`（兜底）+ `providers/{cfworker,smailr}_mailbox`（低层客户端）。

**应定义的 `MailboxProvider` Protocol**：
```python
class MailboxProvider(Protocol):
    key: str                                    # "cfworker"
    aliases: tuple[str, ...]                    # ("icloud_url",)
    def match(self, mailbox: MailboxAccount) -> bool: ...
    def parse_pool_line(self, line, source_path, line_no) -> MailboxAccount | None: ...
    def fetch_messages(self, mailbox, *, limit, proxy, include_body=False, **kw) -> list: ...
    def poll_otp(self, mailbox, *, subject_keyword, timeout, issued_after_unix, proxy, excluded_otps, **kw): ...
    def create_mailboxes(self, args, count) -> list[MailboxAccount]: ...   # 可选
```
配套：`mailbox_strategies` 加 `register_provider(p: MailboxProvider)`；#3/#4 两条链合并为「按注册顺序遍历 provider.parse_pool_line」；#5 的排除集改为「注册的 key 集合之差」自动推导。

### 4.3 缺口 2：注册驱动 —— 加一个要改 **6 处**

| # | 位置 | 内容 |
|---|---|---|
| 1 | `registration_drivers/base.py:8-14` | `RegistrationDriver` enum（含 `ADSPOWER`，但**没有 adspower 模块**） |
| 2 | `base.py:18-24` | `BROWSER_REGISTRATION_DRIVERS` frozenset（枚举值再抄一遍） |
| 3 | `base.py:26-52` | `normalize_registration_driver` 别名集合（`{"roxy","roxybrowser","roxy_browser"}` …） |
| 4 | `cli.py:248` | argparse `choices=[...]` **字面量再抄一遍** |
| 5 | `config.py:448` | `supported_drivers = {...}` **再抄一遍** |
| 6 | `registration_drivers/external_sessions.py:78` | `env_overrides` 每驱动一张 {env 名 → (类型)} 表；`:881-893` `create_browser_session` 的 if 链；每个驱动一个类：`:208 CloakBrowserSession` `:305 CamoufoxBrowserSession` `:479 RoxyBrowserSession` `:689 AdsPowerBrowserSession` |

外加 `camoufox.py` / `cloak.py` / `roxy.py` 三个 12 行近复制 shim（都只是 `run_browser_registration(driver_name=...)`），且 `adspower` **没有**对应 shim —— 四者不对称。

**应定义的 `BrowserDriver` Protocol**：`key` / `aliases` / `env_spec() -> Mapping[str, tuple[str,str]]` / `validate(config) -> None` / `open_session(config, **kw) -> BrowserSession` / `run_registration(**kw) -> dict`。
再建 `DRIVERS: dict[str, BrowserDriver]` 注册表；`cli.py:248`、`config.py:448`、`base.py:18` 三处全部改为 `sorted(DRIVERS)` + 别名表自动展开。

### 4.4 缺口 3：协议支付脚本适配器

`pay_link/adapters.py:133-148` 对 `spec.key` 的 4 分支 if 链（`ideal`/`kakao`/`blik`/`twint`）逐个注入不同 env 变量（`IDEAL_PROXY_SEED_FILE`、`KAKAO_TOKEN`、`TWINT_PROXY_SEED_FILE`…），另有 `:175`、`:230-231`、`:328-332`、`:365-366` 散落的按 key 分支。`services/protocol-payment/` 下 14 个脚本各有一套 env 契约，但全仓无一处声明。
**应定义 `ProtocolScriptAdapter` Protocol**：`build_env(spec, access_token, proxies, kwargs) -> dict[str,str]` / `parse_result(stdout, returncode) -> PaymentResult` / `script_path(spec) -> Path`，按 `spec.adapter` 注册。

### 4.5 其他同构缺口（AST 扫出的字符串分发链）

- `services/protocol-payment/{blik:1719, ideal:1262, twint:1255}` —— **同一个 `promo_mode` 4 分支链被复制了 3 份**（`trial`/`campaign`/`coupon`/`code`）。
- `sms_tool/cli.py:448-452` —— `desktop_read` 子命令 5 分支；`backend` 子命令体系整体缺注册表。
- `account_email_change.py:119`（见 4.2）、`pay_link/adapters.py:133`（见 4.4）、`registration_handlers.py:560`（`flow` 3 分支）、`external_sessions.py:91`（`value_type` 4 分支，类型强制转换）、`pix_core.py:507`（`country` 4 分支）。
- `sms_tool/providers/` **半迁移**：9 个邮箱 provider 只搬了 2 个（`cfworker_mailbox` `smailr_mailbox`），`mailbox_{graph,gmail,chongzhi,icloud_url,remail}` 仍在顶层。要么搬完，要么删掉 `providers/`。

---

## 5. Python ↔ C# 的重复实现

共 **512 个长度 ≥5 的字符串字面量在两边各有一份**。

| 重复逻辑 | Python | C# | 共享常量 | 严重度 |
|---|---|---|---|---|
| **CLI argv 规划** | `cli.py`（969 行 argparse） | `SmsWorkbench.Contracts/BackendCommandPlanner.cs`（**644 行**） | **50 个** flag 字面量（`--buy-cfworker-mailbox` `--change-email-provider` `--approve-proxy-pool` `--no-jit-at-refresh` …） | ★★★ C# 侧重建整个 Python argv；加一个 CLI 参数要改两边 |
| **账号状态解释** | `store/normalize.py:268 _status`、`:333 _looks_account_deactivated` | `AccountStatusInterpreter.cs`（**398 行 / 25 静态方法**） | **46 个**（`account_deactivated`、`at_invalid`、`alive`、`access_token_invalid` …），**含 typo `account_deatived` 两边各一份** | ★★★ typo 同步 = 复制粘贴铁证 |
| **配置分片合并** | `config.py:121 load_merged_config`、`:29 SHARD_FILES`、`:36 SHARD_OWNERSHIP` | `ConfigStore.cs:89 ReadMerged`、`:18 ShardFiles`、`:29 ShardOwnership` | 21 个归属键逐一硬编码；算法（探测分片→深合并→legacy 迁移）逐行同构 | ★★☆ 当前 21=21 无漂移，纯靠人肉 |
| **IPC 信封** | `desktop_ipc.py:10 IPC_PREFIX`、`:57 "smsworkbench.ipc.v2"`、`:19 normalize_stage_event`（16 字段） | `BackendJsonProtocol.cs:9 Prefix`、`:33`、`:56`；`BackendProgressEvents.cs:30,44` | 前缀常量 **3 处**、schema 串 **4 处**；事件 28 字段字面量 | ★★☆ 协议版本升级极易漏改 |
| **邮箱池行解析** | `mailbox_parsers.py:235 parse_mailbox_pool_line` + `_parse_icloud_url_line` | `MailboxCredentialLineParser.cs TryParseICloudUrlLine`（32 行） | `----` / `---` 分隔符、`icloud.com`/`me.com`/`mac.com` 域名 | ★★☆ C# 重实现子集，格式一改必漂 |
| **路径解析** | `paths.py:4 PROJECT_ROOT`、`project_path` | `ApplicationPaths.cs:24 FindRepositoryRoot` | 两边都以 `chatgpt_phone_reg.py` 作 root marker（硬编码） | ★☆☆ 已有 `IApplicationPaths` 接口，尚可 |
| **代理归一化** | `phone_proxy.py:103 normalize_proxy_url`、`paypal_proxy.infer_proxy_country` | `ProxyInputNormalizer.cs:30 Normalize`、`:88 InferCountry` | `host:port:user:pass` → URL 同一算法 | ★★☆ |
| **支付状态机** | `pay_link/normalize.py` | `ProtocolPaymentExecution.cs` | 38 个状态字面量（`completed`/`cancelled`/`canceled`/`decision`/`error_stage` …） | ★★☆ `canceled` 单 l / 双 l 两边都在用 |
| **脱敏** | `sanitizer.py`（扇入 24） | `SensitiveDataSanitizer.cs`（85 行） | 规则集未对齐 | ★★☆ 漏脱敏 = 凭据泄漏 |

**改法（按性价比）**：
1. **单一事实源文件**：把分片归属表、IPC 字段名、状态枚举、CLI 参数表抽成机器可读的 `contracts/*.json`（像已经做对的 `payment_methods.json`：`payment_catalog.py:14 CATALOG_SCHEMA` ↔ `PaymentMethods.cs:102 CatalogSchema` 共用同一 JSON）。Python 读它、C# 读它、CI 加一条「两边常量表 diff 必须为空」的检查。
2. **CLI 契约**：`BackendCommandPlanner.cs` 改为从 Python 端 `--dump-cli-schema` 导出的 JSON 生成（或在 CI 里校验 50 个 flag 与 `cli.py` 一致），否则每加一个参数必漂。
3. **状态解释**：`AccountStatusInterpreter.cs` 的 25 个方法应与 `store/normalize.py` 共用一张「原始值 → 展示状态」映射表，或由 Python 端直接输出已归一化的 `display_status` 字段、C# 只做渲染。

---

## 6. 可独立出去的子系统

| 子包 | 文件 | LOC | 内部边 | 对外出边符号 | 对外入边符号 | 内聚 | 结论 |
|---|---|---|---|---|---|---|---|
| `services/protocol-payment` | 14 | 17,977 | **0** | **1** | 0 | 0.00 | **已事实独立**（14 个独立子进程脚本，内部零 import）。唯一反向边 `pix → sms_tool.account_liveness.probe_account_liveness` 应改为子命令/HTTP 调用。已可视为独立服务 |
| `sms_tool.sentinel` | 4 | 689 | 12 | **7** | 9 | 0.46 | **最容易独立**。对外只要 `auth_headers.{auth_impersonate,auth_user_agent,sentinel_fingerprint}`、`config.current_config_data`、`phone_proxy.normalize_proxy_url`、`sentinel_tokens._extract_sentinel`。先断 F 号环（3 模块自环）即可整包搬走/成服务 |
| `sms_tool.paypal` | 8 | 2,110 | 99 | **14** | 2 | 0.89 | 内聚高、出口窄（14 符号）。10 条反向父壳依赖（`session_refresh` `sms_utils` `account_seed` `utils` `config` `storage` `gen_pp_link` `paypal_fingerprints` `paypal_reverse`）下沉后即可独立 |
| `sms_tool.store` | 7 | 1,605 | 80 | **13** | 1 | 0.88 | 出口 13 符号。卡在 B 号环：`connection.py:47,79` 导入 `storage._storage`（私有）。断环即可独立 |
| `sms_tool.providers` | 3 | 1,013 | 0 | 4 | 2 | 0.00 | 只有 2 个客户端，**半迁移**；要么补全 9 个，要么合并回 `mailbox_*` |
| `sms_tool.paypal_link` | 3 | 2,768 | 170 | 74 | 2 | 0.93 | 内聚最高但**出口最宽**（74 符号，其中 25 个来自 `upi_link` 且 8 个是私有符号）。先把 `upi_link`/`pp_link_helpers` 合并进来、收窄出口，再谈独立 |
| `sms_tool.registration_drivers` | 9 | 3,505 | 18 | 34 | 12 | 0.35 | 出口 34 符号 + 6 处反向父壳依赖 + `playwright.py` 2032 行。需先按 4.3 建 `BrowserDriver` 注册表、把 `playwright.py` 拆成「状态机 / 页面交互 / 会话管理」，才能独立 |
| `sms_tool.pay_link` | 7 | 1,768 | 91 | **179** | 1 | 0.55 | **最差**。179 个出口符号 + 对 `_plm` 的反向依赖 + 7 份复制 import 序言。必须按 1.3 先断环 |
| `sms_tool.commands` | 10 | 2,646 | 10 | 76 | 3 | 0.16 | 纯编排层，本就该薄；76 个出口符号说明它还没成为「薄壳」 |

---

## 7. 解耦路线图

### 第一梯队：收益高 / 成本低（可一次性做完，纯机械重构）

| 动作 | 证据 | 成本 |
|---|---|---|
| 删 `pay_link/*` 的 `_plm` 反向依赖，24 处改直接导入 | `pay_link/{adapters,base,core,normalize,persistence,registry}:4` + 24 处 `_plm.*` | 低（纯符号替换，`payment_link_manager` 保留为 re-export 壳，行为不变） |
| 断 B 号环：`store/connection.py:47,79` 不再 import `storage._storage` | 同上 | 低 |
| 断 F 号环：`sentinel_tokens.py:405` 的延迟导入改构造注入 | 同上 | 低 |
| 删死全局 `pay_link/base.py:166 CFG` | 无导入方、无修改方 | 极低 |
| `desktop_read.py` 三缓存加 `Lock`（照抄 `browser_fingerprint_pool.py:192-215`） | `:140,141,176` | 低 |
| 冻结 `momo_qr_extract.py:35 COUNTRY_CURRENCY` 为 `MappingProxyType` | `:36,:2016` | 低 |
| `mailbox_parsers.py` 两条 6 分支链（`:245-256` / `:299-326`）合并为一个遍历 | 逐行复制粘贴 | 低 |
| `providers/` 二选一：搬完 9 个 provider 或删掉 | 现只有 2/9 | 低–中 |

### 第二梯队：收益高 / 成本中（分阶段，需配套测试）

1. **建 3 个 Protocol + 注册表**：`MailboxProvider`（4.2，9 个实现方）、`BrowserDriver`（4.3，5 个实现方 + 3 个 shim）、`ProtocolScriptAdapter`（4.4，14 个脚本）。照抄 `sms_provider.py:20` 的 ABC 范式。每建一个，同步删除对应的硬编码名单（5–6 处 → 1 处）。
2. **断 C 号环**：`external_sessions._driver_config` 的环境覆盖逻辑下沉到 `registration_drivers/base.py`，`config.py:179` 改为调 `base`。config 扇入 64，这一步能同时让 C、phone_proxy 两条边消失。
3. **拆 `registration_drivers/playwright.py`（2032 行）**：按「状态机 / 页面交互 / 会话管理」三分为 3 个模块，扇出 20 → 各 ≤8。
4. **Python↔C# 单一事实源**：把分片归属表（21 键）、IPC 事件字段（28 个）、状态枚举、CLI 参数表（50 个 flag）抽成 `contracts/*.json`，两边各自读取 + CI 加 drift 检查。**优先做 CLI 参数表**——`BackendCommandPlanner.cs` 644 行全靠它。

### 第三梯队：收益中 / 成本高（看资源）

- `registration_handlers.py`（1118 行 / 22 处延迟导入）与 `account_recovery.py`（944 行 / 17 处延迟导入 / 19 扇出）的编排层拆分。
- `config.CFG` 的 65 处调用点改显式传参（37 个模块）。
- `paypal_link/`（`gen_link` 1255 + `reconciliation` 1305）合并 `upi_link`、`pp_link_helpers` 后收窄 74 个出口符号，再独立。
- `sentinel/` 独立成服务（技术上随时可做——对外仅 7 个符号，F 号环断掉即可）。

### 不值得做

- **`paths.py`（26 行，扇入 26，扇出 0）与 `sanitizer.py`（106 行，扇入 24，扇出 0）**：扇入高但扇出为 0，是健康的叶子工具，不是隐式总线。不要为了降扇入去包一层 façade。
- **`sms_tool/__init__.py`（1 行，扇入 14）**：只是 14 处 import 的落点，无逻辑，拆它零收益。
- **`payment_catalog` ↔ `checkout_contract`/`payment_flow` 的 D 号环**：两处都是函数级延迟导入（`payment_catalog:193,194`），且是**故意的启动顺序设计**（catalog 加载后自校验），解开反而更绕。加注释说明即可。
- **`services/protocol-payment`（17,977 行）进一步拆分**：内部零 import、对外 1 条边，已经是事实上的独立进程集合；再拆没有解耦收益。
- **`pay_link` / `commands` 的 179 / 76 个出口符号**：这些是「机械拆分未收尾」的症状，不是独立的设计目标。先按 1.3 / 4.2 断环建注册表，出口自然会收窄；不要单独为了降扇出去做。

---

## 附：本轮脚本

`F:\tmp\audit5\r5\` — `build_graph.py`（AST import 图，严格排除）、`reverse.py`（反向依赖+调用上下文）、`fan.py`（扇入扇出）、`mutable.py` / `cache.py`（全局可变状态，区分有锁/裸 dict）、`dispatch.py`（字符串分发链→缺注册表的信号）、`crosslang.py`（Python↔C# 共享字面量）、`subsys.py`（子包内聚/耦合 + 延迟导入）、`cycles.py`（Tarjan SCC）。

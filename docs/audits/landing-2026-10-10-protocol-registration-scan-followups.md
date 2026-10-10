# 落地记录：协议注册扫描 P1-A′ / P1-B′ / P1-C′ / P2-2（2026-10-10）

> 落地来源：`docs/audits/scan-2026-10-10-protocol-registration.md` §8 的四项未落地项
> （该扫描 HEAD `e94688b`；本轮基线 HEAD `762ca58`）。
>
> 四项均为**对照工具与开关守卫**的修复，**不改变任何生产默认行为**：全部开关的默认值、
> 请求形状与阶段顺序逐字不变。

---

## 1. 为什么做这个

`scan-2026-10-10` 的头条 P0-A′（`read_toggles` 漏登记四个实验）已在扫描当轮修复，但同一
份报告列出的四项遗留全部**仍未落地**。它们不是理论问题：

- **P1-A′**：P1-6 / P1-7 的机制行是裸 `print`，只进 `backend_stdout.jsonl`；而 runbook
  的 `collect` 示例传的是 `sms_tool.log`。照 runbook 执行，这两个实验的机制行**永远找不到**
  ⇒ `mechanism_ok=false` ⇒ 误判 `manipulation_failed`。这是 P1-6/P1-7 开跑的**前置**。
- **P1-B′**：P1-7 的机制行是**有条件**的（只在撞到 `registration_disallowed` 时出现），
  却登记在常开机制门禁上。一个没撞到该错误的窗口会被读成 `manipulation_failed`，而正确
  结论是「这个窗口什么都没测到」。同族的 P0-2b 已被明确改成 `not_judgeable`。
- **P1-C′**：`e94688b` 声称八个注册开关共用 `steps._registration_flag`，实际只覆盖
  `auth_flow` 内的八个；另外四个 A/B 开关仍各自手写，且未知值语义分三套 ——
  `sentinel_password_bundle` **fail-open 为真**（一个拼错的值会把默认关的开关打开）。
- **P2-2**：runbook 与 `DEFAULT_OUT_DIR` 都写 `runtime/registration_ab/`，而工作区里**唯一
  存在的记录**在 `runtime/ab/`。照 runbook 的 `compare` 示例复现会 file-not-found。

---

## 2. 落地内容

### 2.1 P1-A′：机制行走 `operator_output.emit`

| 位置 | 改动 |
| --- | --- |
| `sms_tool/http_utils.py::_follow_continue_url` | 标签行 `print` → `emit(_LOGGER, ...)`；新增模块 logger 与 `operator_output` 导入。这是 P1-6 的机制行（`label="About you page prime"`）。 |
| `sms_tool/registration_handlers.py::_prime_about_you_page` | 两行 prime 报告 `print` → `_emit`。 |
| `sms_tool/registration_handlers.py::create_account` | P1-7 机制行 `Create account temporarily disallowed…` `print` → `_emit`。 |
| `scripts/registration_ab.py::build_record` docstring | 删除「a marker must be a stdout line so it survives **either** channel」的错误陈述，改为事实：机制行必须走 `operator_output.emit`（一次调用喂两条通道）；裸 `print` 只进 stdout 镜像。 |

`emit` 的两条通道都由 `sanitize_log_text` 净化（`HumanLogFormatter` / `CorrelatedJsonFormatter`
各自调用），所以 `_follow_continue_url` 打印的 `response.url`（可能含 OAuth code）**不会**
因新增日志通道而泄露。P1-6 的机制行经 `label` 参数产生，因此这一处修复同时覆盖所有
`_follow_continue_url` 调用点的标签行。

### 2.2 P1-B′：`"conditional": true`

`scripts/registration_ab.py`：

- `EXPERIMENTS["p1-7-create-disallowed-backoff"]` 新增 `"conditional": True`，判定规则
  文案补上 `not_judgeable` 分支。
- `build_record`：条件式机制行在**处理臂**缺失时记 `mechanism_ok = None`（"未观察到"），
  而不是 `False`（"路径没跑"）；对照臂「没看到」仍记 `True`。记录新增
  `mechanism_conditional` 字段。
- `compare_records`：在 `unverified` 之后、`manipulation_failed` **之前**新增守卫 ——
  设计声明为 conditional 且**任何臂都没看到**该机制行 ⇒ 返回 `not_judgeable`
  （"窗口什么都没测到"），而不是 `manipulation_failed`。

`_mechanism_failed` 的三态契约（`None` = 未检查，不是失败）不变，所以这次改动**不会**给
其它实验开后门：非 conditional 的机制行缺失仍判 `manipulation_failed`（有专门测试钉住）。

### 2.3 P1-C′：新增 `sms_tool/registration_flags.py`

新模块是**叶子**（只 import `typing`），承载唯一一份 `registration_flag(config, key, default)`：
`Mapping` 安全、未知值回落到开关自身默认值（双侧语义）。四个手写点全部收敛：

| 开关 | 实验 | 原语义（未知值） | 新语义 |
| --- | --- | --- | --- |
| `sentinel_password_bundle` | P1-3 | **真**（falsy 黑名单，fail-open） | 假（默认值）**← 修复** |
| `edge_challenge_rotate_exit` | P0-2b | 假（truthy 白名单） | 假（不变） |
| `prime_about_you_page` | P1-6 | 假（truthy 白名单） | 假（不变） |
| `create_account_disallowed_backoff` | P1-7 | 假（truthy 白名单） | 假（不变） |

`registration_protocol_helpers.registration_flag` 改为**re-export** 新模块的实现（保留历史
导入路径与 `__all__`），删除重复的常量与函数体。

**为什么新开一个模块而不是挂到 `registration_policy.py` / `auth_flow.steps`**：
四个调用点都在 `sms_tool/` 目录内，同目录导入**不产生跨目录边**，`import_layer_ratchet`
保持 287 / 31 不变。`auth_flow.steps._registration_flag` **刻意保留自己的副本** ——
从 `auth_flow/` 导入 `sms_tool.registration_flags` 会给 `sms_tool/auth_flow -> sms_tool`
这条已饱和的边 +1（基线 9），棘轮会拒绝。两份实现用测试钉住「同一输入同一答案」。

### 2.4 P2-2：记录路径统一

`scripts/registration_ab.py::DEFAULT_OUT_DIR` 与 `docs/current/registration-ab-runbook.md`
的四处引用统一为 `runtime/ab/`，与工作区里实际存在的记录（`runtime/ab/p1-5-*`、
`runtime/ab/p1-13-*`）一致。

### 2.5 文档回流

- `docs/current/registration-ab-runbook.md`：机制行段改为陈述 `emit` 要求（并点名 P1-A′
  缺陷），新增 P1-7 的 `conditional` 说明段。
- `docs/current/protocol-registration.md`：Owners 表新增 `registration_flags.py` 一行；
  Validation limits 段改为「机制行必须经 `operator_output.emit`、条件式机制须声明
  `conditional`」。

---

## 3. 验证证据

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6251 passed, 8 skipped**, 1176 subtests passed |
| 协议域选择（registration/auth_flow/signin/edge_challenge/sentinel/protocol/frozen_config/http_utils/create_account） | 1416 passed, 139 subtests |
| `tests/test_registration_ab.py` | **87 passed**（新增 6 例 conditional / out-dir） |
| `tests/test_frozen_config_guards.py` + `test_create_account_wire_behaviors.py` + `test_http_utils_pure.py` | 78 passed, 12 subtests |
| `ruff check sms_tool scripts tests` | All checks passed |
| `import_layer_ratchet` | OK 287 edges / 31 pairs（**未涨**） |
| `delayed_import_ratchet` | OK 421 ≤ 421 |
| `unused_import_ratchet` | OK 354 ≤ 355 |
| `bare_print_ratchet` | OK **509**（基线 513，减少 4 个裸 print） |
| `config_key_ratchet` | OK `registration=46`, `email_registration=33` |
| `endpoints_literal_ratchet` | OK 235 / 41 |
| `format_guard` | clean（协议注册泳道 **34** 文件，含新模块） |
| `line_ending_guard` | 41 passed |
| `sentinel_asset_guard` | 2 pinned asset(s) match |
| `docs_consistency_scan` | passed |

### 3.1 新增/扩展的测试

- `tests/test_registration_ab.py::ConditionalMechanismTests`（6 例）：P1-7 声明 conditional；
  条件式机制行缺失记 `None` 而非 `False`；两臂都没看到 ⇒ `not_judgeable`；处理臂看到 ⇒
  正常判定；**非 conditional 实验不受影响**（仍 `manipulation_failed`）；`DEFAULT_OUT_DIR`
  指向 `runtime/ab`。
- `tests/test_frozen_config_guards.py`：四个收敛开关的冻结段读取、缺键默认、未知值 falsy
  （含 `sentinel_password_bundle` fail-open 的回归），以及与 `steps._registration_flag`
  的语义一致性。
- `tests/test_create_account_wire_behaviors.py`：P1-6/P1-7 两行**确实进入 logging 通道**
  （`assertLogs`），以及两个开关门禁的未知值语义。
- `tests/test_http_utils_pure.py`：`_follow_continue_url` 的标签行进入
  `sms_tool.http_utils` 的 logging 通道（P1-6 机制行的通道回归）。

### 3.2 顺带修复（与四项无关，属工作区卫生）

首次全量跑出**唯一**一处失败：`test_line_ending_guard.py::test_no_tracked_text_file_is_crlf_while_its_index_is_lf`
—— `README.md` / `README_EN.md` 在磁盘上是 CRLF 而索引是 LF（`git diff` 因规范化而看不见，
正是该守卫存在的理由）。两文件本轮**未被改动**，属既有工作区状态。按守卫自己的处方做了
**逐字节 CRLF→LF 重写**（`git diff` 为空，内容与 HEAD 逐字相同），该守卫现 41/41 通过。

---

## 4. 未落地（明确留给后续）

1. **P1-C′ 剩余两处**：`http_client.edge_challenge_discrimination`（默认 **true** 的观察开关）
   与 `registration_handlers._obtain_refresh_token_enabled`（`bool(cfg.get(...))`，字符串
   `"false"` 会被读成真）仍手写。二者都**非 A/B 开关**，收敛它们会改变某些字符串配置的
   既有语义，需要各自的判定，故未并入本批。
2. **P1-6 / P1-7 的实跑**：本批只修好前置。两份 arm 记录（含 `--funnel`）仍需按 runbook 采集。
3. **P1-9..P1-12 的 collect 记录**：`read_toggles` 已在上一轮修好，但 signin 系列仍是手工
   TSV 判读，尚无 harness 记录（10-10 扫描 §7 已说明该家族不控制挂起键）。

---

## 5. 复现方式

```powershell
# 四项的回归
.venv\Scripts\python.exe -m pytest -q tests\test_registration_ab.py `
  tests\test_frozen_config_guards.py tests\test_create_account_wire_behaviors.py `
  tests\test_http_utils_pure.py

# 门禁
.venv\Scripts\python.exe scripts\import_layer_ratchet.py
.venv\Scripts\python.exe scripts\bare_print_ratchet.py
.venv\Scripts\python.exe scripts\format_guard.py

# P1-B′ 复现：两臂都没有机制行 ⇒ not_judgeable（不再是 manipulation_failed）
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); sys.path.insert(0,'scripts'); import registration_ab as ab; r=[ab.build_record(experiment='p1-7-create-disallowed-backoff',arm='default',log_text='',funnel={'attempted':30,'registered':10},toggles={'registration.create_account_disallowed_backoff':False}), ab.build_record(experiment='p1-7-create-disallowed-backoff',arm='backoff',log_text='',funnel={'attempted':30,'registered':12},toggles={'registration.create_account_disallowed_backoff':True})]; print(ab.compare_records(r)['verdict'])"

# P1-C′ 复现：未知值不再打开 sentinel_password_bundle
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from types import SimpleNamespace; from sms_tool.registration_sentinel_stages import password_sentinel_bundle_enabled as f; print(f(SimpleNamespace(config={'registration':{'sentinel_password_bundle':'maybe'}})))"
```

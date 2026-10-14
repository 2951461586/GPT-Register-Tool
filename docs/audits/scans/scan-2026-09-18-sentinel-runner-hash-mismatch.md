# 最新一轮协议注册全灭（0/126）根因诊断

- 扫描时间：2026-09-18 10:20
- 事故批次：`ef1b981f6a67485ca04c98de9dded318`（09-17 21:18:33 → 23:20:05）
- 结论级别：**P0，生产全灭，根因已复现**

---

## 0. 结论（一句话）

`sms_tool/sentinel/runtime/sentinel-runner.js` 被**换行符从 CRLF 转成 LF**（内容逐字未变），
SHA256 随之改变，而 `sms_tool/sentinel/bundle.py` 的 `RUNNER_SHA256` 常量**未同步**
⇒ `node_runner` 后端 **100% 完整性校验失败** ⇒ 降级 legacy
⇒ 该次 legacy 提取的 `oauth_create_account` 子流**空手而归**
⇒ **所有走到 `create_account` 的账号（42/42）必然失败**。

叠加长期存在的 `auth_flow` 全局串行 + 20s 网络超时（78 条），构成 126 条 **success=0**。

> 🔴 **本节初稿有两处结论已被推翻，已更正**（详见 §2.4）：
> 1. 初稿称「legacy **结构上**给不出 `oauth_create_account` token」——**错**。映射与构造函数都在，
>    legacy 是结构性支持的；真问题是该次 oauth 子流返回空值。
> 2. 初稿称降级是「静默」的——**不准确**。降级**会打印**；真正把根因抹掉的是
>    `client.py:272` 的异常包装**只保留类型名、丢弃消息体**。

---

## 1. 现象与量化

### 1.1 批次对比（`runtime/registration_progress.jsonl`，774 行）

| batch_id | 记录数 | 成功 | 结束时间 | 成功率 |
|---|---|---|---|---|
| `57f1d262ce2f4947` | 112 | 94 | 09-17 13:40 | 83.9% |
| `1184989155f74b96` | 94 | 45 | 09-17 17:01 | 47.9% |
| `d0780aa88bb94d5b` | 8 | 2 | 09-17 17:42 | 25.0% |
| `df666d0aaffa4005` | 150 | 64 | 09-17 20:56 | 42.7% |
| **`ef1b981f6a67485c`** | **126** | **0** | **09-17 23:20** | **0.0%** |

上一批（20:56）与事故批（21:18 起）**用同一套代理池**（`SYS436615dpm`，09-17 17:10 导入，全 IN 出口），
间隔仅 22 分钟。⇒ **不是代理池问题**。

### 1.2 事故批失败分布

| 条数 | error |
|---|---|
| 78 | `auth_flow_transport:Failed to perform, curl: (28) Operation timed out after 20012 milliseconds with 0 bytes received…` |
| **42** | **`create_account_transport:sentinel_legacy_incomplete:oauth_create_account`** |
| 4 | `email_otp_send_stuck` |
| 1 | `email_otp_validate` → HTTP 409 |
| 1 | `user_register_transport:sentinel_legacy_incomplete:username_password_create` |

### 1.3 与上一批的**关键差异**（回归点）

| error | `df666d0a`（20:56） | `ef1b981f`（事故批） |
|---|---|---|
| `auth_flow_transport curl(28)` | 74 | 78 |
| `sentinel_legacy_incomplete` | **0** | **43** |
| `missing_auth_session_access_token` | 7 | 0 |

⇒ **`curl(28)` 是两批共有的长期问题**；
**`sentinel_legacy_incomplete` 从 0 暴增到 43 才是本次回归**。

### 1.4 漏斗（事故批 126 条）

```
126 started
 └─ 126 sentinel / identity_ready
     └─ 48 通过 auth_flow  → 78 死在 auth_flow 网络超时
         └─ 47 email_otp_send
             └─ 43 email_otp_validate
                 └─ 42 create_account  → 42 条 sentinel_legacy_incomplete（100%）
```

**42/42 = 100%**，无一例外 —— 这是系统性故障的典型特征，不是概率性的反爬波动。

---

## 2. 根因：`sentinel-runner.js` 换行符转换击穿完整性校验

### 2.1 字节级证据（决定性）

| 副本 | mtime | 字节数 | CRLF | 裸 LF | SHA256 |
|---|---|---|---|---|---|
| `sms_tool/sentinel/runtime/sentinel-runner.js`（**生效**） | **09-17 21:16** | **56154** | **0** | **1438** | `b388b2e2…` ❌ |
| `runtime/tmp/p1_3_backup_330/…/sentinel-runner.js` | 09-17 21:16 | 57592 | 1438 | 0 | `334ceb33…` ✅ |
| `dist/installer/package/…/sentinel-runner.js` | 08-31 14:12 | 57592 | 1438 | 0 | `334ceb33…` ✅ |
| `bundle.py: RUNNER_SHA256` 常量 | — | — | — | — | `334ceb33…` |

**57592 − 56154 = 1438 = CRLF 行数**（该文件 1438 行）。
`diff <(sed 's/\r$//' A) <(sed 's/\r$//' B)` ⇒ **0 行差异**。

⇒ **两个文件内容逐字相同，唯一区别是行尾符。**

`bundle.py` 期望 CRLF 版本的 hash，而**工作区在 09-17 21:16 被写成了 LF 版本**。

### 2.2 实测复现

```
$ .venv/Scripts/python.exe -c "from sms_tool.sentinel.bundle import validate_runtime_bundle; validate_runtime_bundle()"
SentinelBundleError: sentinel_runtime_hash_mismatch:sentinel-runner.js
```

`sdk.js` 未受影响 —— 它是**压缩单行**（30863 B，`CRLF=0`、`LF=0`），无换行符可转换。

### 2.3 因果链（8 步，逐步可查）

1. **09-17 21:16** — P1-3 重构批次创建全项目快照 `runtime/tmp/p1_3_backup_330/`，随后某次文件重写把
   `sms_tool/sentinel/runtime/sentinel-runner.js` 由 CRLF 写成 LF。
   （同批 `sms_tool/sentinel/` 下全部文件 mtime 均为 21:16，`__pycache__` 为 21:18。）
2. 该文件 1438 行，CRLF→LF 使字节数 **57592 → 56154**，SHA256 **`334ceb33…` → `b388b2e2…`**。
3. `bundle.py` 的 `RUNNER_SHA256` **仍是 `334ceb33…`**（未同步）。
4. **git 完全看不见**：本机 `core.autocrlf=true`，索引本就是 LF，工作区也变 LF ⇒ `git status` 干净，
   `git hash-object` 与 `HEAD` 一致（`b3762689`）。**这个缺陷已经提交进仓库。**
5. **09-17 21:18** — 批次 `ef1b981f` 启动，第一次使用被改坏的资产。
6. `runner.py:74` → `validate_runtime_bundle(verify_hash=True)` → `bundle.py:38` 抛
   `SentinelBundleError`。
7. `client.py:328-334` 的 `except Exception as runner_error` 捕获它，
   `_legacy_fallback_enabled()` 默认 `True` ⇒ **打印一行后降级** legacy。
   （🔴 措辞纠正：这里**不是「静默」** —— 降级本身会打印。真正抹掉根因的是第 7 步之后的
   **异常包装**，见 §2.4 更正 2 与 §3 的「异常包装」行。）
8. `_token_from_bundle()`（`client.py:89-97`）取 `sentinel_oauth_token`；该次 legacy bundle 里
   这一项是**空串** ⇒ 函数返回 `None` ⇒ `issue_sentinel_flow()` 抛
   `sentinel_legacy_incomplete:oauth_create_account` ⇒ **42/42 必死**。
   （🔴 **不是** key 映射缺失 —— 映射齐全，见 §2.4 更正 1。）

> 注：第 7 步的降级把**「自有资产损坏」伪装成「sentinel 渠道故障」**，
> 这是本次定位最大的干扰项 —— 表面症状（sentinel 报错）与真实根因（文件字节）相隔两层。
> 但真正**抹掉**根因的不是降级本身（降级会打印），而是第 7 步之后的**异常包装**，见 §2.4。

---

### 2.4 两处更正：源码与日志推翻了初稿结论

#### 更正 1 —— legacy 对 `oauth_create_account` 是**结构性支持**的

初稿断言「legacy bundle 没有 `sentinel_oauth_token` 这个键」。核对源码，**映射与构造函数都在**：

| 位置 | 事实 |
|---|---|
| `sms_tool/sentinel_tokens.py:250` | `sentinel_oauth_token = _build_sentinel_pow_token(oauth, did, "oauth_create_account")` |
| `sms_tool/sentinel_tokens.py:292` | 该键**确实写进**返回 dict |
| `sms_tool/sentinel/client.py:92` | `"oauth_create_account": "sentinel_oauth_token"` 映射**存在** |
| `sms_tool/sentinel/client.py:96` | `"oauth_create_account": "sentinel_so_token"` 映射**存在** |

⇒ 所以 `_token_from_bundle` 拿不到 token 的唯一可能是**值本身为空串**：
`_build_sentinel_pow_token()` 第 178-179 行 `if not flow_data or not flow_data.get("token"): return ""`。

**为什么初稿会错**：错误串 `sentinel_legacy_incomplete:oauth_create_account` 里的 `oauth_create_account`
是**被请求的 flow**，不是「缺失的键名」。把它读成「键不存在」是一次**字符串语义误读**。
⇒ 教训：**动手改代码前，必须回源码复核「结构性不可能」这类断言。**

#### 更正 2 —— 真根因字符串**一次都没进日志**

事故批日志 `runtime/logs/processes/28952/backend_stdout.jsonl`
（3161 行；剔除 1151 行 `@@SMSWORKBENCH_V2@@` IPC 载荷后 2010 行）标记计数：

| 标记 | 次数 | 判读 |
|---|---|---|
| `Node runner failed` | **43** | 与 42 `create_account` + 1 `user_register` **完全对应** |
| `SentinelBundleError` | 29 | **仅以类型名出现**，无消息体 |
| `HTTP sentinel fetch failed for flow=` | **0** | ⇒ legacy 提取**没有**因 HTTP 异常整体失败 |
| `HTTP sentinel: no token in` | **0** | ⇒ upc / authorize **都拿到了 token** |
| **`sentinel_runtime_hash_mismatch`** | **0** | 🔴 **根因字符串一次都没出现** |
| `sentinel_legacy_incomplete` | 43 | 42 × `oauth_create_account` + 1 × `username_password_create` |
| `sentinel_fallback_incomplete` | 0 | （本次修复引入的新串，当时不存在） |

由此得到三条**修正后**的结论：

1. **legacy 的 HTTP 提取是成功的**（无 fetch 失败，upc / authorize 都有 token），
   只有 `oauth_create_account` 子流空手而归。而 `_extract_sentinel_http`
   （`sentinel_tokens.py:240-245`）**只校验 upc 与 authorize，从不校验 oauth**
   ⇒ 这个缺口**完全静默**，与「legacy 整体失败」**共用同一个错误串**，事后无法区分。
2. 🔴 **真根因从未进日志**：`client.py:272` 的
   `f"sentinel_issue_failed:{type(exc).__name__}"` 把
   `SentinelBundleError("sentinel_runtime_hash_mismatch:sentinel-runner.js")`
   压成了 `sentinel_issue_failed:SentinelBundleError` —— **消息体被丢弃，只剩类型名**。
   ⇒ 这才是「自有资产损坏」被读成「上游渠道故障」的**直接机制**。
3. 因此 §3 的「防御失效」要**补第五层**：**异常包装层**。

---

## 3. 为什么没被拦住（五层防御同时失效）

| 层 | 应有作用 | 实际 |
|---|---|---|
| **测试** | `tests/test_sentinel_runner.py:64` | 基线是**红的**：`3 failed, 2 passed`。改动的提交**跑了全量**，但把这 3 个用例在**命令行上 `--deselect` 掉了**（提交 `20ab44e` 正文原文：「4 known pre-existing deselections: 3 sentinel pinned-hash mismatches + 1 idempotent-baseline」）⇒ **详见 §3.1** |
| **异常包装** | 异常链应把根因带进日志 | 🔴 `client.py:272` 的 `f"sentinel_issue_failed:{type(exc).__name__}"` **只保留类型名、丢弃消息体** ⇒ `sentinel_runtime_hash_mismatch` 在整个事故批 3161 行日志里出现 **0 次**。这是「自有资产损坏」被读成「上游渠道故障」的**直接原因** |
| **测试命名** | `test_vendored_runtime_bundle_matches_pinned_hashes` | 名字说 "matches pinned hashes"，**断言只检查文件名**（`sdk.name == "sdk.js"`），从未显式比对 hash |
| **git** | 版本控制应能看见改动 | `autocrlf=true` + 索引已是 LF ⇒ **字节变化对 git 完全不可见** |
| **pre-commit** | 换行符守卫 | `line_ending_guard` 是 **09-18 10:10** 才上线（`c1016b2`），**晚于事故 13 小时**；且它防的是「单文件内 CRLF/LF 混用」，而本事故是**整文件一致地转 LF**，守卫抓不到 |

`.gitattributes` 的注释本身已经预言了这个危害：

> 3. 逐字节比对工具（本项目大量使用：`git show HEAD:path` + SHA256 做基线归属）
>    会在「语义相同的两份文件」上报出差异。

—— 而 `bundle.py` 正是这样一个工具。策略方向（`*.js text eol=lf`）是对的，**但没同步调整依赖字节的校验逻辑**。

### 3.1 「已知既存失败」是怎么把红基线变成绿基线的

这是本次最值得记的一条**方法学坑**。事实链（全部按文件 mtime 取证）：

| 时间（09-17/18） | 事件 | 取证物 |
|---|---|---|
| 21:16:25 | P1-3 批次建立改写前备份 | `runtime/tmp/upi_rewrite_backup/upi_link.py.orig` |
| **21:16:44** | `sentinel-runner.js` 被写成 LF（**19 秒后**） | 文件 mtime + 字节三元组 |
| 21:18:33 | 事故批 `ef1b981f` 启动 | `registration_progress.jsonl` |
| 21:58:33 | 建立并运行既存性证明脚本 | `runtime/tmp/prove_sentinel_preexisting.py` |
| 10:11:32 | 提交 `20ab44e` 落地，正文记「4 known pre-existing deselections」 | `git log -1 20ab44e` |

`prove_sentinel_preexisting.py` 做的事：把 `sms_tool/upi_link.py` 换回改写前备份 → 跑
`tests/test_sentinel_runner.py` → 再换回来，做 A/B 对照。它**确实**证明了这 3 个失败
**与 UPI 改写无关**。

🔴 **但它证明不了「与 `sentinel-runner.js` 改写无关」——因为它的两个对照臂都取自 21:16:44 之后的状态。**
对照组已被污染，它只能排除「UPI 改写」这一个嫌疑，而真凶在 **42 分钟前**就已落进工作区。

由此产生两个后果：

1. 3 个用例被贴上 `known pre-existing` 标签、在命令行 `--deselect` 掉
   ⇒ 全量跑出 `4510 passed, 6 skipped, 0 failed` 的**绿**基线，而基线实际上是**红**的。
2. 🔴 **这个排除动作在仓库里零留痕**：
   - `pytest.ini` **无** `addopts`；
   - `pyproject.toml` **无** `[tool.pytest.ini_options]` 段；
   - 全仓（排除 `dist/`、`runtime/`）搜 `--deselect` **零命中**；
   - `prove_sentinel_preexisting.py` 位于 `runtime/tmp/`，该目录被 `.gitignore:16:/runtime/` 忽略，
     `git log --all` 对它**零记录**。

   ⇒ 任何后来者看到的都是一个绿基线，**没有任何线索指向被压制的 3 个用例**。

**方法学铁律**：A/B 对照必须确认**两个臂都晚于所有可疑改动**；
当嫌疑改动有多笔时，「证明与 A 无关」**不等于**「证明与任何改动无关」。

**修复建议（P2，未落地）**：把「已知既存失败」写进**仓库内**的显式清单
（如 `tests/known_preexisting.txt` + 一条读取它的 `conftest.py` 钩子），并让测试断言该清单为空或已过期 ——
使压制行为**可审计、可过期、可复核**，而不是活在某个人的命令行历史里。

---

## 4. 修复方案

### P0 — 立即止血（让 node_runner 恢复工作）

**核心思路：让完整性校验对换行符免疫**，而不是把文件改回 CRLF。

理由：`.gitattributes` 已明确 `*.js text eol=lf`，把文件改回 CRLF 会在下次 checkout 时**再次被转成 LF**，
属于**必复发的修法**。

改动点 1 —— `sms_tool/sentinel/bundle.py`：

```python
# 常量登记为「LF 归一化后」的摘要（与 .gitattributes 的 eol=lf 策略一致）
RUNNER_SHA256 = "b388b2e2cca9511bfa0cf06689407142cd790136c44020d1c4fc321c0ea9eece"
SDK_SHA256    = "de9ae60f5bcd3b8f57f5f86628630e28022f72b47056a87f37d4d8a0b5b88537"  # 不变

def _digest(path: Path) -> str:
    # 换行符归一化后再摘要：CRLF 与 LF 视为同一内容。
    # 若不做归一化，git 的 autocrlf / .gitattributes 会在 checkout 时
    # 静默改变字节，而 git status 完全看不见 —— 09-17 事故即由此产生。
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()
```

**已实证**：三个副本（工作区 LF / 备份 CRLF / dist CRLF）归一化后摘要**完全一致** = `b388b2e2…` ✅
且 `sdk.js` 归一化后仍 = `de9ae60f…`（无换行符，不受影响）✅

改动点 2 —— 补测试断言（`tests/test_sentinel_runner.py`）：

```python
def test_vendored_runtime_bundle_matches_pinned_hashes():
    from sms_tool.sentinel.bundle import SDK_SHA256, RUNNER_SHA256, _digest, SDK_PATH, RUNNER_PATH
    assert _digest(SDK_PATH) == SDK_SHA256          # 显式断言，不再只查文件名
    assert _digest(RUNNER_PATH) == RUNNER_SHA256
    assert validate_runtime_bundle()                 # 端到端仍通过
```

### P1 — 加固（消除「静默降级」这个伪装层）

> ⚠️ **本节初稿的方案已作废**。初稿假设 legacy 对 `oauth_create_account` 结构性不可能成功，
> 于是打算对非 upc/authorize 的 flow **禁止回退**。§2.4 更正 1 的源码核对推翻了该前提 ——
> 映射齐全、构造函数也在。**照初稿实施会误伤**：在 legacy 本可成功的场景下直接抛错。
> 以下是**实际落地**的改法。

改动点 3 —— 让根因穿透异常包装（**已落地**）：

- `client.py:272`：`f"sentinel_issue_failed:{type(exc).__name__}"`
  → `f"sentinel_issue_failed:{_root_reason(exc, normalized_proxy)}"`
- 新增 `_root_reason()`：沿 `__cause__` 走到**最深层**异常，渲染为 `类型名(消息)`，
  **脱敏**（复用 `phone_proxy.redact_proxy_text`，覆盖 `scheme://user:pass@host` 与
  `host:port:user:pass` 两种形态）、**限长 120 字符**
  （进度账本 `registration_progress.py:180` 截断到 300、账号库 `store/accounts.py:159` 截断到 800，
  必须留出余量，否则有用的尾巴会被截掉）。
- `issue_sentinel_flow()` 的降级失败分支、以及 `Node runner failed` 的**打印行**同步改用 `_root_reason()`。

效果 —— 离线端到端实证（`runtime/tmp/_prove_root_reason.py`，只桩掉 `_challenge` 网络调用，
其余走真实 `issue_sentinel_token` → `run_sentinel_sdk` → `validate_runtime_bundle`）：

| 层 | 改前 | 改后 |
|---|---|---|
| 包装层 | `sentinel_issue_failed:SentinelBundleError` | `sentinel_issue_failed:SentinelBundleError(sentinel_runtime_hash_mismatch:sentinel-runner.js)` |
| 打印行 | `... fallback for oauth_create_account: SentinelIssueError` | `... fallback for oauth_create_account: SentinelBundleError(sentinel_runtime_hash_mismatch:sentinel-runner.js)` |
| error 字段 | `sentinel_legacy_incomplete:oauth_create_account` | `sentinel_fallback_incomplete:oauth_create_account:SentinelBundleError(sentinel_runtime_hash_mismatch:sentinel-runner.js)`（**120 字符**，小于 300 截断阈值） |

改动点 4 —— pre-commit 增加 sentinel 资产字节门禁（**已落地**）：
`scripts/sentinel_asset_guard.py` **复用运行时自己的 `_digest`**（保证门禁与运行时永不漂移），
对 `sms_tool/sentinel/runtime/*.js` 做归一化摘要比对，不匹配即拒绝提交。

### P2 — 观察项与建议（未落地）

- 🔴 `_extract_sentinel_http`（`sentinel_tokens.py:240-245`）**只校验 upc 与 authorize，从不校验 oauth**。
  oauth 子流空手而归时会产出**空串** `sentinel_oauth_token`，而**没有一行日志提示** ——
  本次事故的错误串正是由此与「legacy 整体失败」**共用同一个串**，事后无法区分（§2.4 更正 2）。
  建议补上 oauth 校验并打印一行，把该情况变成可定位的独立信号。
- `sentinel_issue_failed:*` 与 `sentinel_legacy_incomplete:*` 都**未注册**进
  `failure_registry.py`（该文件只有 `sentinel_extract_failed`）⇒ 判为 `unknown` ⇒ 重试守卫视为**终态**（见 §7.4）。
- 每次改动 `sms_tool/sentinel/runtime/` 后**必须**跑 `tests/test_sentinel_runner.py` 与全量基线。
- 「已知既存失败」应改为仓库内显式清单（见 §3.1 修复建议），不再靠命令行 `--deselect`。
- `dist/installer/package/` 内的副本是 08-31 的 CRLF 版本，与工作区不同步；发布打包前需重新生成。

---

## 5. 附：同期独立问题（**非本次回归**，两批共有）

### 5.1 `auth_flow` 全局串行（吞吐锁死）

`registration_concurrency.py:37,48`：

```python
_STAGE_GROUPS = { "auth_flow": "auth", ... }
_DEFAULT_CAPS = { "auth": 1, "network": 4, "at_probe": 4, "payment": 2 }
```

`auth_flow` 归入 `auth` 组，cap=**1**（且默认启用跨进程文件锁，`cross_process: true`）。

事故批 `auth_flow` 事件 126 个，其中 94 个带排队记录：
**中位排队 64.5 s，最大 136.2 s**。批次总时长 21:18→23:20（约 2 小时）与串行模型吻合。

⇒ 吞吐被锁死在 ~55 账号/小时。**建议**：将 `auth` cap 从 1 提到 2–3，或按出口维度而非全局维度设限
（当前限制的动机是 sentinel 配额，而 `email_registration.sentinel_max_concurrency = 2` 已是另一道独立闸门）。

### 5.2 `auth_flow_transport` 的 `curl: (28)` 超时（78 条 / 62%）——已定位到**供应商单一**

**逐字错误串**（事故账本原文，此前本节写作 `curl (28) ... 20003 ms` 是转述、不准确，已更正）：

```
auth_flow_transport:Failed to perform, curl: (28) Operation timed out after 20012 milliseconds
with 0 bytes received. See https://curl.se/libcurl/c/libcurl-errors.html first for more details.
```

#### 5.2.1 失败发生在**哪一个请求**

事故日志里逐请求统计（`runtime/logs/processes/28952/backend_stdout.jsonl`）：

| 请求 | 标签 | 重试行数 |
|---|---|---|
| `GET https://chatgpt.com/` | `ChatGPT prime` | **174**（其中 `1/3` 首轮 **91** 个账号命中） |
| `GET https://auth.openai.com/create-account` | `Auth prime` | **0** |
| `GET https://chatgpt.com/api/auth/csrf` | `Auth csrf` | **0** |

⇒ 超时**全部**发生在 `chatgpt.com`，`auth.openai.com` 一次都没有。

**为什么打的是 `chatgpt.com`**：`registration_handlers.auth_flow` 按 `registration_mode` 二选一 ——
`passwordless` 打 `chat_base`（chatgpt.com），否则打 `auth_base/create-account`。
而 `email_registration.registration_mode` 在 `runtime.json` 与 `config.json` **都不存在**，
落到 `registration_checkpoint.py:123` 的默认值 **`"passwordless"`** ⇒ 这批打的是 chatgpt.com。

⚠️ **但改模式不是解药**：`Auth csrf`（`registration_handlers.py:738`）**无条件**打 `chat_base`，
两种模式都会经过 chatgpt.com。改模式只降低暴露次数，不消除。

#### 5.2.2 重试阶梯：6 次请求，但只有 **2 个出口 IP**

| 层 | 次数 | 是否换出口 |
|---|---|---|
| `http_client.request_with_retry` | **3**（`timeouts.http_retries` 缺省 = 3） | ❌ **不换**，同一个 session/出口重打 |
| `batch_runner` 账号级重试 | **2**（`max_attempts=2`） | ✅ 换 —— 但只 `refresh_proxy_sid()`，**同供应商同地区**换一个 sticky session |

`refresh_proxy_sid` → `proxy_entry.rotate_session(proxy, "")` 对 rola 是**有效**的
（`_ROLA_SID_RE` 匹配 `SYS436615dpm_1-country-in` 里的 `_1`；且 `_random_session_id(avoid=...)`
已修掉「1 字符 id 有 1/10 概率原地不动」的静默 no-op）⇒ 第 2 次账号级尝试确实拿到新出口 IP。

🔴 **所以每个账号最多只有 2 个出口 IP，且都来自同一个 rola 账号、同一个 `-country-in`。**

#### 5.2.3 现状复测（本机实测，2026-09-18 11:2x）

用**生产同款调用**（`request_with_retry` + 真实 `chatgpt_headers` + `oai-did` cookie +
`impersonate='firefox144'` + `timeout=20`）逐出口打 `chatgpt.com/`，每出口 3 次（共 30 次）：

| 出口 | 结果 |
|---|---|
| [0] | **FAIL(Timeout)/20043ms** · 200/451KB/2835ms · **FAIL(Timeout)/20004ms** |
| [2] | 200 · 200 · **FAIL(Timeout)/20016ms** |
| [3] | **FAIL(Timeout)/20017ms** · 200 · 200 |
| [5] | 200 · **FAIL(Timeout)/20005ms** · **FAIL(Timeout)/20009ms** |
| [1][4][6][7][8][9] | 3/3 全部 200（2.0–5.9 s，~450 KB） |

⇒ **6/30 = 20% 的请求吃满 20 s 超时、`0 bytes received`**，且**与出口不相关**（同一出口 3 次里时好时坏）。

⚠️ **一处必须记下的探针教训**：第一次探针自己拼请求（只给 UA + Accept，无 `oai-did` cookie、
无 `chatgpt_headers`），得到「6/10 出口被 Cloudflare 403」的结论 —— **纯属夹具失真**。
换成生产同款调用后 **403 归零、8/10 返回 200 / 460 KB**。
⇒ **先怀疑夹具，再怀疑实现**；探针必须复用生产调用，不能自己拼。

#### 5.2.4 归因结论

- ❌ **不是「20 s 超时太短」**：健康响应 2.0–5.9 s（最大观测 7.6 s），20 s 有 2.6× 余量；
  且 09-17 那次是同一请求连失败 3 次（~66 s），加长超时同样会失败。
- ❌ **不是单一坏出口**：复测里 6 个出口全绿、4 个间歇；09-17 的 78 条也均匀分布在全部 10 个索引上。
- ✅ **是供应商/地区单一**：池中 10 条**全是同一个 rola 账号**（`SYS436615dpm_1..10`）、
  **同一个地区**（`-country-in`）、**同一个网关**（`proxysg.rola.vip:2000`）。
  账号级重试换 sid 仍留在**同一供应商**内 ⇒ 一旦该供应商的印度出口对 `chatgpt.com` 大面积挂死，
  **2 个 IP × 3 次尝试全部落空，账号必丢**。

自洽性校验：设「出口对 chatgpt.com 挂死」的概率为 q，坏出口会让 3 次 HTTP 尝试全部挂死，
则账号失败率 ≈ q²。实测 `ChatGPT prime` 首轮命中率 **91/126 = 72%**（≈ q），
账号失败 **78/126 = 62%**（≈ q² = 0.72² = 52%，同量级）⇒ **模型成立**：
当晚约 **4/5 的出口**打不通 chatgpt.com。

#### 5.2.5 建议（**未落地，需拍板**）

1. 🔴 **给池加第二个供应商/地区**（恢复多样性）。这是唯一能同时解释并消除 62% 的措施 ——
   单供应商下，「换 sid」换不出真正的失败域。历史备份链里有 VN 池：
   `runtime/proxy-backups/proxy.json.before-in-switch-20260917-153600`（VN 20 条）。
2. **不要**为了这个桶去调 `timeouts.request`：复测显示超时不是瓶颈，调它只会掩盖问题。
3. ⬜ 可选（收益较小、改动在核心路径）：`request_with_retry` 对
   **`curl: (7)` / `curl: (28)` 且 `0 bytes received`** 这类「出口一个字节都没吐出来」的失败，
   在第 1 次就换出口再试，而不是同一出口重打 3 次 ——
   可省下 2/3 的挂死等待（每次 ~20 s）。⚠️ 必须**只对纯传输失败**生效：
   `batch_runner.py:485-489` 把出口钉死是有意为之（换出口会被注册方看作 proxy churn ⇒ 触发封禁），
   那条理由针对的是**应用层结果**（403/429/风控），与「出口压根没连上」是两回事。

### 5.3 `email_otp_validate` HTTP 409（1 条）

与记忆中的 `validate 409 根因（与发码顺序无关）` 同源，待办未结。

---

## 6. 回滚路径

本次诊断**未修改任何生产文件**。若按 P0 方案执行：

- 改动前备份 `sms_tool/sentinel/bundle.py`（单文件，可逆）。
- 回滚 = 恢复该文件 + 还原 `RUNNER_SHA256` 常量。
- 佐证材料（只读，未改动）：
  - `runtime/tmp/p1_3_backup_330/sms_tool/sentinel/runtime/sentinel-runner.js`（57592 B，CRLF）
  - `dist/installer/package/sms_tool/sentinel/runtime/sentinel-runner.js`（57592 B，CRLF）
  - `runtime/registration_progress.jsonl`（事故批 126 条原始记录）

---

## 7. 修复落地记录（2026-09-18 10:27–11:05，老板拍板 P0+P1）

### 7.1 已落地

| 项 | 文件 | 改动 |
|---|---|---|
| **P0-1** | `sms_tool/sentinel/bundle.py` | `_digest()` 改为换行符归一化后摘要；`RUNNER_SHA256` → `b388b2e2…`（LF 归一化版） |
| **P0-2** | `tests/test_sentinel_runner.py` | 改为显式 `assert _digest(PATH) == PIN`；新增换行符不敏感回归测试、篡改仍被拒测试 |
| **P1-1** | `sms_tool/sentinel/client.py` | 降级后仍失败时抛 `sentinel_fallback_incomplete:<flow>:<根因>` 并 `from` 原异常，不再伪装成 `sentinel_legacy_incomplete` |
| **P1-2** | `scripts/sentinel_asset_guard.py`（新增）· `.githooks/pre-commit` | 第 4 道门禁：随包资产内容必须匹配运行时强制的 pin |
| **P1-3** | `sms_tool/sentinel/client.py` | 新增 `_root_reason()`（沿 `__cause__` 取最深异常、脱敏、限长 120）；`client.py:272` 与降级打印行改用它 ⇒ **根因字符串首次真正进日志** |
| **P1-b** | `sms_tool/failure_registry.py` · `tests/test_failure_registry.py` | `sentinel_legacy_incomplete` / `sentinel_issue_failed` / `sentinel_fallback_incomplete` **三个前缀标记登记进 `network` 类**（该类的 `attempt_retryable=True, retain_for_future_batch=True` 正是那 42 个地址需要的处置）；新增 2 个测试 |

> **P1-3 的由来**：P1-1 落地后复核事故日志，发现 `sentinel_runtime_hash_mismatch` 在整批
> 3161 行里出现 **0 次**（§2.4 更正 2）—— 说明 P1-1 **没有达成它自己声明的目标**
> （「让根因可见」）。根因在 `client.py:272` 就被 `type(exc).__name__` 丢掉了。
> ⇒ 补 P1-3，并加两条测试：`test_fallback_failure_surfaces_the_root_cause_behind_a_wrapper`
> 与 `test_root_reason_is_bounded_and_redacted`。

### 7.2 验证凭据

| 判据 | 改前 | 改后 |
|---|---|---|
| `pytest tests/test_sentinel_runner.py` | **3 failed / 2 passed** | **11 passed**（P0/P1 后 9，P1-3 再加 2） |
| `pytest tests/test_failure_registry.py` + 两个分类测试 | 66 passed | **66 passed**（含 P1-b 新增 2 个） |
| P1-b 行为变更 | `unknown`（三项全 False，等同终态） | **`network`**（立即重试 / 留待后续批次 均 True，不拉黑） |
| `validate_runtime_bundle()` | 抛 `SentinelBundleError` | 正常返回 `(sdk.js, sentinel-runner.js)` |
| 门禁变异验证 | — | **3/3 全部捕获**（常量改错 → 抓到 runner.js；`_digest` 改回字节敏感 → 自检失败；`_digest` 变成常量 → 自检失败） |
| 四道 pre-commit 实跑 | — | 全绿（含新增 `sentinel-asset-guard: 2 pinned asset(s) match their digests`） |
| 改动文件行尾符 | — | 7 个文件全部纯 LF，**无混合**（逐文件 `len / CRLF / LF` 三元组核对） |
| 根因穿透（离线端到端） | `sentinel_runtime_hash_mismatch` 在事故批日志出现 **0 次** | 三层全部带出根因（`runtime/tmp/_prove_root_reason.py`，error 字段 120 字符） |
| 出口复测（30 次请求） | 09-17：约 4/5 出口打不通 chatgpt.com | 今日：**6/30 吃满 20 s 超时**，与出口不相关（`runtime/tmp/_probe_prime_faithful.py`） |
| **全量 `pytest -q`** | 改前基线**已红**（`test_sentinel_runner.py` 3 failed，且靠命令行 `--deselect` 压成绿） | **4522 passed / 6 skipped / 841 subtests / 0 failed**，**零 deselect**（6 个 skip 为既有 skipif，非压制） |

### 7.3 备份与回滚

**P0/P1 改动前**的备份目录 `runtime/p0p1-backup-20260918-1027/`：

| 文件 | 字节数（备份） | 字节数（当前） |
|---|---|---|
| `bundle.py` | 2262 | 3009 |
| `client.py` | 14276 | 17289 |
| `test_sentinel_runner.py` | 4659 | 10126 |
| `pre-commit` | 2355 | 3009 |

**回滚路径（两条，按粒度选）**：

1. **整体回滚到改动前**：用上表四个文件覆盖回去，并删除新增的
   `scripts/sentinel_asset_guard.py` 与 `.githooks/pre-commit` 中对应的那一行。
2. **只回滚 P1-3**（保留 P0/P1）：🔴 **我没有为 P1-3 单独留快照**（P1-3 是在 P1-1 落地后复核日志时才追加的）。
   可精确回退的 4 处改动是：
   - `client.py`：删除 `_REASON_LIMIT` 与 `_root_reason()` 整个函数；
   - `client.py:272`：`_root_reason(exc, normalized_proxy)` → `type(exc).__name__`；
   - `client.py` 降级失败分支与 `Node runner failed` 打印行：同样改回 `type(...).__name__`；
   - `client.py` 导入行去掉 `redact_proxy_text`；`tests/test_sentinel_runner.py` 删除 P1-3 新增的 2 个测试。

   ⇒ 提交后更简单的等价做法：`git revert <本次提交>` 只回滚到「P0/P1 后、P1-3 前」不可行
   （两者在同一提交），此时按上面 4 处**逐条手改**即可，改动面很小且都在一个文件内。

### 7.4 未落地（需拍板）

**P1-b —— 已落地（2026-09-18 11:2x，老板授权「同时修 P1-b」）。**
三个串此前都判 `unknown`，而 `unknown` 在重试守卫眼里**等同终态**
（`failure_registry.py:228` 原话），09-17 那批 42 个账号就是这样被直接丢号的。
已按 §2.4 更正 1 的源码事实归类为 **`network`**（不是 `account`）：

- **为什么不是 `account`**：token 没发出来，`create_account` 从未带着可用载荷发出，**地址没有被消耗**。
  `account` 带 `batch_dropped=True`，会把完全可注册的地址拉黑 ——
  与 `user_already_exists` / `password_step_unconfirmed` 分开的同一个理由。
- **为什么不是终态**：runner 可以瞬时失败；唯一已知的本地成因（资产损坏）修好即可重跑。
  `retain_for_future_batch` 正是那 42 个地址当时**需要却没拿到**的处置。
- **标记取前缀**（不是整串）：`sentinel_issue_failed` / `sentinel_fallback_incomplete`
  会由 `_root_reason` 追加根因后缀，后缀只作诊断。

行为变更实证（真实分类器）：

```
错误串 : create_account_transport:sentinel_legacy_incomplete:oauth_create_account
改前   : unknown  ⇒ 立即重试 False / 留待后续批次 False / 拉黑 False（等同终态）
改后   : network  ⇒ 立即重试 True  / 留待后续批次 True  / 拉黑 False
```

**其余待拍板项**（§4 P2 已记，此处汇总）：

- `_extract_sentinel_http`（`sentinel_tokens.py:240-245`）**只校验 upc / authorize，从不校验 oauth**
  ⇒ oauth 子流空手而归时产出空串且**无一行日志**，与「legacy 整体失败」**共用同一个错误串**（§2.4 更正 2）。
- **§5.2.5 的三条**：给代理池加第二供应商（🔴 首要）；不要调 `timeouts.request`；
  可选让 `request_with_retry` 对纯传输失败提前换出口。
- 「已知既存失败」改为仓库内显式清单（§3.1）。

### 7.5 关于打包产物副本

`dist/installer/package/sms_tool/sentinel/` 仍是 08-31 的旧版：
`bundle.py` 为字节敏感摘要，`sentinel-runner.js` 为 57592 B / CRLF ⇒ **两者自洽**（能通过校验），
**当前不构成事故**，但它保留着完全相同的脆弱性。

该目录是**打包产物**，应在下次打包时从工作区重新生成，**不要手工改**。


---

## 8. 第二供应商接入（2026-09-18，同日）

> **后续处置（2026-09-18 同日稍后）**：最新活动批次显示 9http 的最终失败集中度
> 明显高于 rola，操作者决定将 9http 从活动注册池移除。`proxy.json` 当前只保留
> 10 条 rola IN 路由；本节以下内容保留为当时的历史取证，不再描述当前配置。
> 原 `tests/test_second_provider_pool.py` 已删除，IN 地理档案守卫迁移到
> `tests/test_registration_protocol_geo.py`，通用抗健康排序守卫迁移到
> `tests/test_batch_error_classification.py`。

§5.2.5 的第一条建议（"给池加第二个供应商/地区"）已于同日落地，本节记录**实测结论**与
一处**比预想更深的坑**。

### 8.1 动作

`proxy.json` 的 `proxy.pool` 从 **10 条（rola 单一供应商）** 扩到 **20 条**：

| 供应商 | 网关 | 模板 | 条数 |
|---|---|---|---|
| rola | `proxysg.rola.vip:2000` | `country-in` | 10（偶数下标） |
| 9http | `global.9http.com:9091` | `geo-IN` | 10（奇数下标） |

**交错排布**，不是追加。实测 **20/20 出口均为 IN**（Cloudflare `cdn-cgi/trace` 的 `loc`），
声明地区与实测地区**零不一致**。9http 侧有 2 条出口走 **IPv6**（`2401:…` / `2409:…`），
若上游对 IPv6 有额外风控需留意。

⚠️ 顺带修正一个**既有文档错误**：本机 `proxy.json` 在 09-17 已被切到 **IN 单地区 10 条**，
而 `docs/registration-and-proxy-architecture.md` 仍写着"30 条 / VN 主池 + PH 备池 /
唯一可用出口是 rola PH"。已按现状更新并把手改项补全（见 §8.3）。

### 8.2 🔴 为什么必须"交错"：追加式会静默失去多样性

`batch_runner._run_one` 每个账号只算一次
`account_proxy_index = (i + offset) % len(pool)`，然后**终生钉住**该出口；重试只
`refresh_proxy_sid()`（换 sid **不换 IP**）。唯一能换出口的是**波次级游标**
`_rotate_proxy_pool_cursor`（**+1**，由 OTP 派发侧封禁信号触发）。

⇒ 把新家**追加在尾部**（`[rola x10][9http x10]`），wave_size=4 时的实际切法：

| wave | 下标 | 分布 | 供应商数 |
|---|---|---|---|
| 0 | 0-3 | `rrrr` | 1 |
| 1 | 4-7 | `rrrr` | 1 |
| 2 | 8-11 | `rr99` | 2（只是边界劈开） |
| 3 | 12-15 | `9999` | 1 |

**每批前 8 个账号仍全在 rola 上**，且游标 +1/2/3 都救不回来
（offset=1/2/3 时 wave0 仍单供应商）。"加进池子"于是只是**名义上的**多样性。

交错后（rola 偶数位 / 9http 奇数位）**任意偏移下每个 wave 都含两家**，且游标 +1 就换一家。
守卫见 `tests/test_second_provider_pool.py`：用例 A 钉不变量，用例 B 是**负向对照**
（证明 A 不是恒真）。

### 8.3 自查发现的第二层坑：健康排序会不会压平交错？

注册池**会**过 `ProxyHealthTracker.rank()`（`batch_runner.py:40`），而 `rank` 是
"冷却最後 / 成功率降序 / 失败次数升序"的稳定排序 ⇒ 若某一家整体更差，它会把该家整体后移，
**交错就被压平**（手工构造同一 sid 的伪场景可复现：9http 全挂时排序退化为 `rrrrrrrrrr9999999999`）。

**实际不会发生**，原因是键的粒度：`ProxyHealthTracker.key` = `host:port#sid-<hash>`，
而 `select_registration_proxy_pool` 的预检探的是 `refresh_proxy_sid(base)` **之后**的地址，
下一批又换一个新 sid ⇒ **查不到上一批的记录** ⇒ 分数回中性 0.5 ⇒ 稳定排序保持**配置顺序**。

这条已写成用例（含"键必须含 sid"的断言），并对 `key()` 去掉 sid 做了变异验证（rc=1，非空守卫生效）。

> ⚠️ 反过来说：若哪天有人把 `key()` 改成按 endpoint 聚合（去掉 sid 以求"跨批累积"），
> 交错就会被压平，前几个账号回到单一供应商 —— 那时必须同时改 `rank` 的调用方式。

### 8.4 ⚠️ 该池**不解决** 62% 超时

用**生产调用路径**（`request_with_retry` + `chatgpt_headers` + `oai-did` + `auth_impersonate()`）
实测 9http 10 条在 `chatgpt.com` 上：

| 结果 | 条数 |
|---|---|
| 200 / 460 KB（`~3-11s`） | **4** |
| `curl: (28)` 20s / **0 bytes** | **6** |
| 403 / 429 | **0** |

⇒ 与 rola **完全同一签名**（0 字节超时、零 403）。这反过来**加强**了 §5.2 的归因：
瓶颈**不在某一家的出口信誉**，而在"印度 → chatgpt.com"这条**路径**本身
（或本机到印度的链路）。加第二家的真正收益是**把单点故障域拆开**
——一家被封不再等于整批全灭 —— 而不是把 62% 降下来。

**要降 62% 应做的是**：换**地区**（同一家换 `-geo-XX` 比加第二家更可能直接绕开），
或接受该路径并靠重试吞吐覆盖。

### 8.5 新增/改动文件

| 文件 | 改动 |
|---|---|
| `proxy.json` | pool 10 → 20，交错；**字节级文本拼接**（CRLF / 无末尾换行 / `\u002B` 转义全部原样保留） |
| `sms_tool/geo/profiles.py` | `MARKET_PROFILES` 补 `IN`（`en-IN` / `Asia/Kolkata`） |
| `sms_tool/browser_fingerprint_pool.py` | `COUNTRY_LOCALE_PROFILE_MAP["IN"]="in"`；`TIMEZONE_NAME_BY_IANA` 补 `Asia/Kolkata`+`Asia/Calcutta` = `India Standard Time` |
| `tests/test_second_provider_pool.py` | **新增** 10 条用例（交错不变量 / 负向对照 / 抗健康排序 / IN 两 lane 接线） |
| `docs/registration-and-proxy-architecture.md` | 池形态段落更新 + 两条 lane 表；由 `scripts/refresh_doc_symbol_lines.py --apply` 刷新 5 个行号指针 |
| `runtime/tmp/verify_egress_regions.py` | **新增**：按 `(供应商, 声明地区)` 抽测并对账（替代文档里已不存在的 `runtime/_probe_egress_regions.py`） |

### 8.6 坑

- 🔴 **`write_text` 会把 CRLF 文件悄悄改成 LF**：本仓库 `.gitattributes` 是
  `eol=lf`，所以这个方向**恰好是对的**；但当它是**错的**方向时（`proxy.json` 要求 CRLF）
  就会造成整文件重写。改 `proxy.json` 一律用**字节级拼接**，不要 `json.load`+`json.dump`。
- 🔴 **探针的"不可达"多为瞬时**：9http 10 条首轮 6 条超时，但重测时 `W` / `zjV`
  两条从 `SSLError` 转 **200 / IN**。判"这家死了"必须多轮复测。
- 🔴 **别用 `ip-api.com` / `ipapi.co` 当唯一地区判据**：同一出口实测 `ipapi.co` 回 **429**
  而 Cloudflare / ipinfo / ipwho 全 200 ⇒ 会把活出口读成坏出口（`GEO!` 假告警）。
  权威判据用 Cloudflare `cdn-cgi/trace` 的 `loc`。
- **健康记录按 sid 键控**（见 §8.3）——这是"交错不会被压平"的**唯一**依据，别拆。

# 扫描：代理 IP 池凭据暴露面（2026-11-02）

- **日期**：2026-11-02
- **HEAD**：`bbd12f1`（`fix(desktop): read the mailbox lane pool, not the lane object`）
- **范围**：1358 个 tracked 文件的凭据形态串、616 个提交（717,683 条新增行）的历史、`runtime/` 下 6392 个运行时文件的日志/IPC/argv/导出/备份五条通道
- **不在范围**：未对任何凭据做探活（不产生供应商流量）。轮换供应商口令是运营动作，本文只给结论与顺序
- **相关**：`docs/audits/records/security-exposure-assessment-2026-08-31.md`（同一仓库的首次暴露面评估）、`scripts/precommit_guard.py`、`docs/current/ui-architecture.md`

---

## 0. 结论先行

1. **远端（公开）仓库确实泄露过 2 组真实供应商凭据**：一组来自 rola 的注册池条目，一组来自 kookeey 的解析样例。它们从 **2026-08-06 / 2026-09-13** 起就在 `origin/main` 上，公开可检索，仓库当时有 187 个 fork（见 2026-08-31 评估）⇒ **按已泄露处理：历史重写无意义，只能轮换**。
2. **当前运营凭据没有泄露**：`proxylane` 那一对在 HEAD 与全部 616 个提交里 **0 命中**（逐串 pickaxe + 全历史正则两路复核），只存在于 `%LOCALAPPDATA%\GPTRegisterTool\config\proxy.json`。
3. **运行期通道是干净的**：`runtime/**/*.log` 里 3708 处代理串**全部已脱敏，明文 0**；代理不进 argv（走 `SMS_TOOL_PROXY*` 环境变量）；`admin.list_pool` 用 `mask_proxy`；`proxy_affinity` 只存 scheme/host/port/`session_id`；工作区备份是白名单（不吞 `runtime/`）。
4. **真正的结构性缺陷是守卫盲区，不是某一次手滑**：`precommit_guard.py` 的内容闸门对 `docs/`、`tests/` **提前返回**，且只认 `scheme://user:pass@host`、不认 `host:port:user:pass`。两条真实凭据正好落在这两个盲区里——`--all` 当时报 clean。已修（§3）。
5. **本地未跟踪残留已清**：`runtime/ab/p1-18/arm-*/config/` 的 9 份完整配置副本（458 处明文凭据）已删除，实验结论文件（funnel / log）保留。

---

## 1. 方法与判据

| 维度 | 做法 |
| --- | --- |
| 当前树 | 正则抽 `scheme://user:pass@host` 与四段式 `host:port:user:pass`，再按「合成值 / 真实值」分类：命中 `SYNTH`、`YOUR_`、`custom_zone`、`_sid_`、`_time_`、`***`、`user:pass` 等模板标记的记为 fixture |
| 与运营状态对撞 | 把 tracked 里的 `(user, pass, host)` 三元组与 `%LOCALAPPDATA%` 配置分片、`runtime/*.json`、`sessions/*.json` 做精确交集（只比哈希，不打印） |
| 历史 | `git log -p --all` 逐条新增行扫描 + `git log -S<具体串>` 定位引入提交 |
| 远端 | 对 `origin/main` 的树逐个 `git show` 复扫：HEAD 命中的是否也在远端、引入提交是否在 `origin/main` 祖先链上 |
| 通道 | 日志（脱敏 vs 明文计数）、`backend_stdout.jsonl`、C# 启动日志、`desktop_read` payload、会话/导出字段、`WorkspaceBackupService` 白名单 |

规模：tracked 凭据形态三元组 **223** 个；其中「provider 真实格式」共 **15** 个（13 个在 HEAD、2 个仅存于历史）。

---

## 2. 远端发现

| 位置 | 形态（不复述凭据） | 首次公开 | 判定 |
| --- | --- | --- | --- |
| `tests/proxy/test_proxy_entry.py`、`tests/registration/test_registration_second_egress.py`、`docs/audits/scans/scan-2026-09-12-localflow-register-reference.md`、`docs/audits/scans/scan-2026-09-21-payment-eligibility-in-promotion-column.md`、`sms_tool/proxy/entry.py`（注释） | `http://SYS…tbr_N-country-XX:…@gate.rola.vip:2000` | 2026-09-13 | **真实池条目**：无 fixture 标记；同文件注释写明「Probed against the live gateway on 2026-09-12 … all 10 rola pool entries」 |
| `tests/proxy/test_proxy_pool.py`（`test_from_url_four_segment`） | `gate.kookeey.info:1000:…:…-JP` | 2026-08-06（`81cbe18`） | **真实账号**：kookeey 的 `账号-会话 : 口令-国家` 形态齐全，非构造值 |
| 历史中已被删除、但仍在公开历史里 | 一组 kookeey（2026-06-13）、一组 ipwo（2026-08-29，口令侧已呈脱敏态） | — | 删除只影响 HEAD，不影响 `git log` / fork 副本 |

同时披露（非凭据）：供应商域名在 tests/docs 里广泛存在——`ipwo.net` 72 次/23 文件、`kookeey.info` 29/12、`cliproxy.io` 23/6、`rola.vip` 21/10、`lajiaohttp.net` 16/8、`9http.com` 14/10、`zooproxy.com` 10/3、`proxylane.dev` 5/4。这属于「用了哪家供应商」的信息披露，不是口令泄露；本次不处理。

### 2.1 通道复核

| 通道 | 结论 | 证据 |
| --- | --- | --- |
| Python 日志 | 干净 | `runtime/**/*.log`：脱敏 3708、明文 0 |
| stdout 镜像 | 干净 | `runtime/**/*.jsonl` 0 命中 |
| CLI argv | 受控 | 代理走 `SMS_TOOL_PROXY*` 并从 argv 移除；启动日志只打命令名 + command_id |
| IPC `admin.list_pool` | 受控 | `_row()` 走 `mask_proxy()`；`proxy_raw` 仅对**无凭据**条目返回 |
| 会话 / 导出 | 受控（提醒） | `identity_context.proxy_affinity` 只存 scheme/host/port/country/`session_id`；`session_id` 是**半凭据**（sticky session 靠它固定），对外发会话 JSON 时值得去掉 |
| 工作区备份 | 受控 | `WorkspaceBackupService` 是白名单（`accounts.sqlite3` + 主题/语言），不整目录打包 |
| 本地未跟踪状态 | **已清** | `runtime/ab/p1-18/arm-*/config/*.json`（9 份、458 处明文）已删；`runtime/phone_proxy_probe_cache.json`（304 处）保留（它是测量缓存，非配置副本） |

---

## 3. 本次落地

1. **HEAD 脱敏**：把上文两组真实值换成仓库自己的合成惯例——`SYNTHACCT7tbr_1-country-vn:synth%5E%23B`（保留 `^`/`#` 百分号编码这个真正的测试意图）、`SYNTHACCT7-sess1:synthpass-JP`；审计文档与 `sms_tool/proxy/entry.py` 注释里的同一标识符同步替换。
2. **守卫补口**（`scripts/precommit_guard.py`）：新增 `scan_proxy_endpoints()`，对**每个**文本文件生效（不再对 docs/tests 提前返回），同时覆盖四段式；判定为合成值的条件写死在本仓库的 fixture 惯例上（`SYNTH`/`YOUR_`/`custom_zone`/`_sid_`/`_time_`/`***`/`<…>`/`{…}`），并额外要求四段式的 host 必须带点。命中时**只报 host，不回显凭据**。`tests/governance/test_precommit_guard.py` 补 15 条用例（含「docs/tests 里也要拦」与「合成值不拦」两个方向）。
3. **`runtime/ab` 配置副本删除**：实验已结束，配置副本可由 runner 重新生成；funnel/log 结论文件保留。

验证：`precommit_guard.py --all` clean（1358 文件）；`pytest tests/governance/test_precommit_guard.py` 49 passed；`pytest tests/proxy tests/registration/test_registration_proxy_routing.py tests/registration/test_phone_proxy.py tests/payment/test_payment_proxy_canonical.py` 633 passed。

---

## 4. 仍需人工完成

1. **轮换 rola 与 kookeey 两个账号**（公开 ≥2 个月、fork 副本不可回收）。这是唯一能止损的动作；代码侧脱敏只降低后续扩散面。
2. 若这两个供应商还在用，换成新口令后再按 §3.1 的形态更新本地配置（不要回填到测试里，测试用合成值已足够）。
3. `session_id` 是否要从对外导出的会话 JSON 里移除/哈希化，需要产品侧拍板。

---

## 5. 口径与误报

- 判定「真实」依赖三条信号：provider 的账号/口令格式（如 kookeey 的 `账号-会话:口令-国家`）、**没有**任何合成标记、以及引入语境（注释/提交说明指向线上实测）。**没有**做探活，所以「是否仍然有效」未确认——这不影响「按已泄露处理」的结论。
- 反向误报也要记：`***:***@host` 这类**已脱敏**值在 tests 里大量存在，闸门必须放行（否则会逼着测试不再断言脱敏输出）；四段式在 `host:port:user` 三段的合法配置上不得误报（因此要求 host 带点且有口令段）。
- `runtime/` 是被忽略区，闸门看不见它；`runtime/ab` 这类「把配置整份拷进仓库树」的写法只能靠流程约束（实验结束即删），本次已按此清理。

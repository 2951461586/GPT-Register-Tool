# 审计：选中 22 个邮箱，报告为何是 3/6

- **日期**：2026-09-19
- **触发**：老板提问「明明选中了 22 个邮箱执行注册任务，但是最终报告显示只完成了 3/6？」
- **结论**：**22 和 3/6 不属于同一次运行**。22 是 20:40 那次的选择数；3/6 是 23:58 那次的报告。
  报告的分母**不是选择数**，而是「穿过两道过滤后真正被尝试的账号数」。
- **性质**：**不是 bug**，是三层拦截按设计工作；但**报告口径与 UI 选择数之间缺一条可见的换算链**。

---

## 1. 一句话结论

```
22 行被勾选（20:40 那次的选择，勾选状态一直保留）
  ├─ WPF 本地过滤（静默）      −10  →  12   ← 对话框显示的就是这个 12
  │    ├─ 4 个「已注册」
  │    └─ 6 个「半注册」（服务端已有账号、我们无凭据）
  └─ 后端死路过滤（有日志）    −6   →   6   ← 报告的分母 = 6
       └─ 6 个 email_otp_send_stuck 隔离地址
                             6 尝试 → 3 成功 → 报告「Done. 3/6」
```

---

## 2. 证据链

### 2.1 两次运行的原貌（`runtime/app_20260918.log` + 各 PID 的 `backend_stdout.jsonl`）

| | 20:40 那次 | 23:58 那次 |
|---|---|---|
| PID | 29896 | 26876 |
| command_id | `f26d421f33af4b6e85860e4904aa0ef7` | `7b35620fef9a43f9851a5a061b305073` |
| WPF 传参 | `--count 22 --workers 3` | `--count 12 --workers 3` |
| 临时邮箱文件 | `selected_mailbox_20260918_204016.txt`（**22 行**） | `selected_mailbox_20260918_235839.txt`（**12 行**） |
| 后端跳过 | `Skipped 12 mailbox(es) …` | `Skipped 6 mailbox(es) …` |
| 后端抱怨 | `Requested 22 account(s), but only 10 mailbox(es) were loaded` | `Requested 12 account(s), but only 6 mailbox(es) were loaded` |
| 批次头 | `Batch Registration - 10 accounts` | `Batch Registration - 6 accounts` |
| **报告** | `Done. 4/10 registered successfully` | **`Done. 3/6 registered successfully`** |

### 2.2 两个临时文件的包含关系（决定性判据）

```
20:40 文件 = 22 条
23:58 文件 = 12 条
B ⊆ A        : True
B 有 A 无    : 0 条
A 有 B 无    : 10 条
```

⇒ 老板是**对同一批 22 行重新勾选**了一次（勾选框在首次运行后保持选中），
不是两次不同的选择。

### 2.3 22 条的逐条归因（用真实加载器复跑，见 §4）

| 归因 | 条数 | 终态 | 去哪了 |
|---|---|---|---|
| `registered`（我们库里有账号） | 4 | 已注册 | WPF 本地丢掉 |
| `dead_end` → `partial_registered`（服务端已有账号，无凭据） | 6 | 半注册 | WPF 本地丢掉 |
| `otp_pending_quarantine` | 6 | 隔离中 | 后端 `_drop_already_registered` 丢掉 |
| 无任何记录 | 6 | — | **真正被尝试** → 3 成功 / 3 失败 |
| **合计** | **22** | | |

对账：`4 + 6 + 6 + 6 = 22` ✓ ，`22 − 10 = 12` ✓ ，`12 − 6 = 6` ✓

### 2.4 那 6 条隔离的明细（`runtime/registration_retry_guard.json`）

6 条**全部**是 `+oai01` 别名、`otp_pending_count = 2`、原因全是 `email_otp_send_stuck`：

| 地址（脱敏） | 隔离写入时间 | `quarantine_until` | 自动放行 |
|---|---|---|---|
| `ca***@icloud.com` | 09-18 20:44 | **无** | 09-19 20:44 |
| `st***@icloud.com` | 09-18 17:50 | **无** | 09-19 17:50 |
| `tu***@icloud.com` | 09-18 20:47 | **无** | 09-19 20:47 |
| `fu***@icloud.com` | 09-18 20:48 | **无** | 09-19 20:48 |
| `ar***@icloud.com` | 09-18 18:26 | **无** | 09-19 18:26 |
| `le***@icloud.com` | 09-18 18:33 | **无** | 09-19 18:33 |

全库隔离行 = 6（就是这 6 条），`has_quarantine_until` 分布 = `{False: 6}`。

⇒ 它们是**加 TTL 之前**被隔离的（行里没有截止字段），按回落规则
`last_attempt_at + otp_pending_quarantine_seconds(86400)` 在 **09-19 17:50–20:48 自然放行**。
**不需要任何清理脚本** —— 这正是昨天那个 TTL 改动在起作用。

**隔离是 20:40 那批自己写进去的**（`processes/29896/backend_stdout.jsonl` 的失败构成）：

```
[!] Registration failed for ca***@icloud.com: email_otp_send_stuck
[!] Registration failed for tu***@icloud.com: email_otp_send_stuck
[!] Registration failed for fu***@icloud.com: email_otp_send_stuck
[!] Registration failed for ho***@icloud.com: email_otp_send_stuck
[!] Registration failed for b23a6a8439c0dde5: registration_internal_error:RuntimeError:
    Failed to perform, curl: (56) Proxy CONNECT aborted      ← ×2
```

`ca/tu/fu` 的 `last_attempt_at` = 20:44/20:47/20:48 ⇒ 与本次时间窗吻合，**是这一批把
`otp_pending_count` 从 1 推到 2 才触发隔离**。而 `ho***` 同样是
`email_otp_send_stuck` 却**没被隔离**（此前 0 次），并在 23:58 成功注册
⇒ **隔离阈值 = `otp_pending_count` 累计到 2**，单次失败不隔离。

（另：`b23a6a8439c0dde5` 那 2 次 `curl: (56)` 落 `registration_internal_error`，
就是昨天 P0-1 修掉的「curl 传输码没登记进 network 清单」缺陷。）

### 2.5 6 次尝试的结果

| 地址 | 结果 | 失败原因 |
|---|---|---|
| `ho***@icloud.com` | ✅ 成功 | — |
| `be***@icloud.com` | ✅ 成功 | — |
| `ba***@icloud.com` | ✅ 成功 | — |
| `mi***@icloud.com` | ❌ 失败（重试 2 次） | `auth_flow_transport: curl: (28)` 20008ms / 0 bytes |
| `to***@icloud.com` | ❌ 失败 | `email_otp_send_stuck` |
| `lo***@icloud.com` | ❌ 失败 | `email_otp_send_stuck` |

3 个成功账号在 promotion check 里都是 `Free·无优惠`。

---

## 3. 报告分母到底是什么（代码判据）

`sms_tool/commands/registration.py:711`

```python
safe_print(f"\n[*] Done. {success_count}/{effective_count} registered successfully, …")
```

`effective_count` 的来源（`sms_tool/cli.py`）：

| 行 | 赋值 | 本次取值 |
|---|---|---|
| 673 | `requested_count = max(1, int(args.count or 1))` | **12**（WPF 传的 `--count`） |
| 636 | `mailboxes = filter_registered_mailboxes(mailboxes)` | 22 → **12** |
| 690 | `effective_count = len(mailboxes)` | **6** |

⇒ 报告分母 = `effective_count` = **穿过 `_drop_already_registered` 之后的存活数**，
既不是网格勾选数（22），也不是 WPF 写进文件的行数（12）。

---

## 4. 三段过滤各自丢了什么、为什么静默

| 段 | 位置 | 判据 | 本次丢掉 | 有没有提示 |
|---|---|---|---|---|
| ① 网格勾选 → WPF 写文件 | `MainWindow.Register.cs:646` `TryCreateSelectedUnregisteredMailboxFileAsync` | `IsUnregisteredMailboxRowAsync`：`IsPartial(RegistrationStatus/Status)` 或 `HasRegisteredAccountState`（状态含 已注册/PayPal/支付完成/已导入）或**拿不到可用邮箱行** | 10 | 🔴 **只把存活数塞进选项对话框**（"已选择 12 个邮箱"），**丢掉的 10 个一个字都不说** |
| ② WPF 文件 → 后端池 | `batch_runner.py:291` `filter_registered_mailboxes` → `_drop_already_registered` | `registered` / `partial_registered` / `blocked_email_states()` / checkpoint 错误 | 6 | ✅ 打印 `[*] Skipped 6 mailbox(es) …` + `[!] Requested 12 … only 6 …` |
| ③ 池 → 尝试 | `batch_runner.run_batch_impl` | 同上（幂等，第二遍是 no-op） | 0 | — |

🔴 **断链就在这里**：① 段静默。老板看到的是「22 个勾选框还打着勾」，
对话框写「已选择 12 个」，报告写「3/6」—— 中间少了「22 里排除了 10 个」这句话。

---

## 5. 为什么同一批 22 行，两次跑出不同的数

`blocked_email_states()`（`registration_retry_guard.py:364`）返回**三种**跨批封锁，
其中一种**带时钟**：

```python
if row.get("dead_end"):                        result[n] = "dead_end"                # 永久
elif self._quarantine_active(row, now):        result[n] = "otp_pending_quarantine"   # 有 TTL
elif float(row.get("cooldown_until") or 0)>now: result[n] = "cooldown"                # 300s 时钟
```

- `dead_end` 永久 ⇒ 永远被跳
- `otp_pending_quarantine` 有 TTL ⇒ 24h 后自然放行
- `cooldown` 只有 300s ⇒ **5 分钟后同一地址就重新变成候选**

所以「同一批勾选」在不同时刻的尝试集合**本来就不一样**。
`mi***/lo***/to***` 在 20:40 被跳过、在 23:58 被尝试，就是 300s 冷却已过期。
⇒ **拿两次不同时刻的运行互相比较「为什么少了」在方法上是无效的。**

---

## 6. 本轮踩到 / 复现的坑

1. 🔴🔴 **`_drop_already_registered` 收的是 mailbox 对象，不是字符串。**
   第一版探针直接传 `list[str]`，函数体里 `getattr(mailbox, "email", "")` 全返回空串
   ⇒ `records`/`blocked_states` 查的是空 key ⇒ 过滤**静默变成 no-op**，
   探针输出 `kept=12 skipped_dead=0`，与生产日志（跳 6）矛盾。
   **夹具失真 = 得出「实现没问题」的假结论**。改成 `SimpleNamespace(email=…)` 后与生产逐条一致。
   （与 09-18 预检并发测试的 `#0` 夹具坑同型：**先怀疑夹具，再怀疑实现**。）

2. 🔴 **「同一个 3 个字符的脱敏前缀」会撞车。**
   `fu***@icloud.com` 同时匹配 `fuels-earthy.4m+oai01` 和 `furrows_flagon.7o+oai01`，
   按脱敏前缀做集合运算会得到假重叠。**归因必须用全量地址做键**，脱敏只在展示层做。

3. 🔴 **报告分母口径要单独确认，别假定是「选择数」。**
   本项目里它是 `effective_count`（过滤后存活数）。同理
   `Batch Registration - N accounts` 的 N 也是它。

4. ⚠️ **顶层 `runtime/logs/backend_stdout.log` 停在 09-15**，只有
   `processes/<pid>/backend_stdout.jsonl` 是最新的 ⇒ 取证必须走 per-PID 目录。

---

## 7. 建议（待老板拍板，本轮未改任何代码）

| 优先级 | 建议 | 理由 |
|---|---|---|
| P1 | **WPF 补一行提示**：`TryCreateSelectedUnregisteredMailboxFileAsync` 返回被排除数与原因分布，在选项对话框里写明「勾选 22 → 可注册 12（4 已注册 / 6 半注册）→ 后端还会剔除隔离地址」 | 消掉 §4 的静默断链，这类问题不会再被问第二次 |
| P2 | **选项对话框显示 `pending.Count` 的同时显示 `SelectedRowsOrCurrent().Count`** | 让「勾选数 ≠ 可注册数」在点确定之前就可见 |
| P3 | **把「后端跳过的 N 条」回写到网格状态** | 现在只有控制台一行，网格里那些行仍显示旧状态 |
| — | 6 条隔离**不用动**，09-19 17:50–20:48 自动放行 | TTL 已生效（见 §2.4） |

---

## 8. 相关文件

- `sms_tool/batch_runner.py`（`_drop_already_registered` 170-270 · `filter_registered_mailboxes` 291-308 · `_announce_dead_end` 281-288 · 批次头 389）
- `sms_tool/cli.py`（`requested_count`/`effective_count` 673-691）
- `sms_tool/commands/registration.py`（报告行 711）
- `sms_tool/registration_retry_guard.py`（`blocked_email_states` 364-380 · `quarantined_emails` 350-362）
- `SmsWorkbench/MainWindow.Register.cs`（`TryCreateSelectedUnregisteredMailboxFileAsync` 646-672 · `IsUnregisteredMailboxRowAsync` 658-672 · `TryCreateMailboxFileAsync` 614-639 · 对话框 375-401）
- `SmsWorkbench/RegistrationStatusPresentation.cs`（`IsPartial`）
- 只读探针：`runtime/tmp/_probe_batch_skip_attribution.py`（gitignore 下）
- 关联：`scan-2026-09-18-latest-protocol-batch-diagnosis.md`（隔离 TTL 的落地）

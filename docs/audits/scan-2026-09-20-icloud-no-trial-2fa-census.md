# iCloud 账号「无试用优惠」普查 + 2FA 拆分

- 日期：2026-09-20
- 数据源：`runtime/accounts.sqlite3`（2114 条账号记录）
- 产物：`runtime/analysis/icloud_no_trial_20260920.txt`（清单）、`.csv`（带字段）
- 复现脚本：`runtime/tmp/census_icloud_no_trial_20260920.py`

## 一、结论

| 口径 | 数量 |
| --- | --- |
| iCloud 账号总数 | 1718 |
| **其中「无试用优惠」（Free·无优惠）** | **1684** |
| ├ 已设置 2FA | **517** |
| └ 未设置 2FA | **1167** |
| 其中「可试用Plus」 | 31 |
| 其中「检测失败 / 未检测」 | 3 |

全库参考：2FA 已设置 531 / 未设置 1583（共 2114）。

## 二、判据（取自项目既有契约，未新造语义）

### 优惠状态

- 真源：`raw_json.promotion_state`，枚举见 `sms_tool/promotion_states.py`
  （`trial_eligible` / `subscribed` / `free` / `auth_invalid` / `probe_failed` / `unknown`）。
- 老记录（2026-09-12 引入 `promotion_state` 之前写入）无 state，回退到展示标签
  `promotion_status`。
- **「无试用优惠」= 标签 `Free·无优惠`**——全库唯一含「无优惠」的标签，
  由 `account_promotion.promotion_status_label` 在 `plus_trial_eligible == False` 时产出。
- 落库路径：`store/markers.py::mark_promotion_status`。

### 2FA

- 判据与桌面端一致（`SmsWorkbench/AccountStatusInterpreter.cs::HasTwoFactor`）：
  `totp_present` / `totp_enrolled` / `has_totp` 任一为真，或 `totp_secret` 非空，
  或 `twofa_enrolled_at > 0`。
- 本库实际只有 `totp_secret` 一种信号（`totp_present` 等三个布尔键全库零出现）。
- 已核对：DB 列 `totp_secret` 与 `raw_json.totp_secret` **531 条完全一致，零不一致**。

## 三、全库优惠状态分布（归一后）

| state | 数量 | 备注 |
| --- | --- | --- |
| `free` | 2078 | 含 1120 条老记录（无 state，靠标签回退） |
| `trial_eligible` | 33 | 含 8 条老记录 |
| `probe_failed` | 2 | 标签「检测失败」 |
| `unprobed` | 1 | 从未检测 |
| `auth_invalid` / `subscribed` | 0 | — |

## 四、关键坑与注意事项

### 🔴 1. 「未设置 2FA」绝大多数是**主动跳过**，不是失败

按账号创建日切片（iCloud，2FA 覆盖率）：

```
08-05  0/47      09-06  30/30     09-13  0/13
08-06  0/33      09-08  265/268   09-14  0/88
08-10  53/70     09-11  9/23      09-15  0/102
08-11  158/160   09-12  1/80      09-16  0/71
09-04  0/84                       09-17  0/494
                                  09-18  0/70
                                  09-19  0/48
                                  09-20  0/13
```

交叉 `runtime/app_*.log` 的启动参数（`启动任务` / `含--no-2fa`）：

```
09-12  10/3     09-16  22/20     09-20  3/2
09-13   5/5      09-17  23/12
09-14  17/11     09-18   4/3
09-15  23/20     09-19  10/10
```

⇒ 09-12 起 WPF「选中未注册邮箱注册」任务基本都带 `--no-2fa`
（`registration_finalize.py:116-118` 直接 `return`，**连 `twofa_enroll_error` 都不写**）。
所以 1167 条「未设置」里只有 **19 条**有失败记录，其余是**从未尝试**。

19 条失败记录的归因：

| 错误 | 数量 |
| --- | --- |
| `pyotp_missing` | 12 |
| `curl: (56) Proxy CONNECT aborted` | 2 |
| `mfa enroll HTTP 500: Request timeout` | 1 |
| `curl: (28) 超时 0 bytes` | 1 |
| `recent_auth_required`（401，需重认证） | 1 |
| `browser_totp_activate_failed` HTTP 500 | 1 |
| `browser_totp_exception: NetworkError` | 1 |

⇒ 想真正提高 2FA 覆盖率，先去掉 `--no-2fa`，并确认 `pyotp` 已装进运行环境。

### 🔴 2. 优惠状态是**检测时刻的快照**，不是实时值

目标集合 1684 条的检测时间分布跨度 **08-30 → 09-20**：

```
08-30 306   09-12  80   09-17 484
09-04  84   09-13  11   09-18  68
09-05  21   09-14  86   09-19  48
09-06  30   09-15 100   09-20  13
09-08 265   09-16  68   09-10   2
09-11  18
```

其中 306 条（08-30）已过去 3 周。Plus 试用资格会随活动变化，
**「无试用优惠」只代表检测当时无优惠**。要拿实时结论需重跑
`refresh_promotion_statuses`（走代理，有成本）。

### 🔴 3. 老记录缺 `promotion_state`，只能靠中文标签回退

1129 条无 state，其中 1120 条标签为 `Free·无优惠`、8 条为 `可试用Plus*`。
本报告已做标签回退；但按项目既有设计（`promotion_states.py` 的模块注释），
**任何过滤/排序逻辑都不该再依赖中文标签**。这批老记录重跑一次优惠检测即可补齐 state。

### 4. `promotion_marker_is_stale` 本次无影响

全库 `auth_invalid` / 「AT失效」为 0，不存在「AT 失效标记早于已验证重登」的情形。

## 五、产物格式

`runtime/analysis/icloud_no_trial_20260920.txt`：

```
# iCloud 且无试用优惠（Free·无优惠）邮箱清单
# 生成时间: 2026-09-20 23:49:02
# 数据源  : runtime/accounts.sqlite3
# 总计    : 1684  已设置2FA: 517  未设置2FA: 1167
# 格式    : email<TAB>2FA状态<TAB>优惠检测时间
05.stoma.brother+oai01@icloud.com	已设置2FA	2026-09-08 05:45
...
```

排序：已设置 2FA 在前，同组按邮箱字典序。
CSV 列：`email,twofa,promotion_state,promotion_label,promotion_checked_at`。

## 六、建议下一步（按性价比排序）

1. **重跑优惠检测**补齐 1129 条缺失的 `promotion_state`，同时刷新 08-30 那批的陈旧快照。
2. 若需要 2FA 覆盖，去掉 `--no-2fa` 并确认 `pyotp` 在 `.venv` 中；
   19 条失败记录可作为回归验证样本。
3. 目标集合中「未设置 2FA + 无试用优惠」的 1167 条，
   在补齐 2FA 前不具备作为稳定交付物的条件。

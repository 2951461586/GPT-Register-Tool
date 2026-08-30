# 凭据与 PII 暴露面评估（2026-08-31）

本文记录对两起泄漏的**实际暴露面**核实结果，以及由此产生的处置决定。
核实方式是查证据（CI 运行记录、GitHub API、fork 副本），不是凭印象推断。

> 仓库性质：**公开**，408 stars，**187 forks**。评估任何泄漏都必须把 fork 算进去。

## 结论摘要

| 项目 | 是否真暴露 | 处置 |
| --- | --- | --- |
| 8 个分片凭据 + Roxy token + 代理口令 | **否**，从未上过 GitHub | 不轮换，改为补防线 |
| `kyl_state.json` PII（183 条账号记录） | **是**，已公开 2.5 个月，187 个 fork 副本 | 不处置，已无法撤回 |

这两项的结论**与最初的直觉相反**：看起来更严重的凭据其实从未外泄，看起来"只是 PII"的文件才是真正公开且不可撤回的那个。

## 一、分片凭据：从未上过 GitHub

### 涉及的值

| 分片 | 字段 | 备注 |
| --- | --- | --- |
| `proxy.json` | `phone_reuse.smsbower.api_key` | 32 位 |
| `runtime.json` | `registration.drivers.roxy.api_token` | 32 位 hex |
| `runtime.json` | `email_registration.remail.api_key` | 39 位 |
| `runtime.json` | `email_registration.smailr.api_key` | 35 位 |
| `runtime.json` | `email_registration.cfworker_admin_token` | 16 位 |
| `runtime.json` | `email_registration.cfworker_api_token` | 53 位 |
| `runtime.json` | `email_registration.gmail.app_password` / `.password` | 同一值，19 位 |
| `payment.json` | `sub2api.api_token` | 70 位 |
| `payment.json` | `cpa_mode.api_token` / `sub2api.password` | **与 cfworker_admin_token 同一值** |

### 证据链

1. **CI 运行记录的空白期**（决定性）
   `08-22T15:05`（`7fb6ecb`）到 `08-30T15:49 UTC`（`fea8132`，= 本地 23:09，正是 filter-repo 后的 force push）之间
   **一次 CI 运行都没有**。CI 由 push 触发，分片入库 `fdf2368`、Roxy 脚本 `37928f5` 全落在这段空白里
   → 这些提交从未被推送。
2. 重写前的老 sha（`fa70bd0` / `e4d5c8f` / `4515dfd` / `cc6910a`）在主仓库 API 全部返回 `No commit found`。
3. 187 个 fork 的最后 push 均 ≤ `08-22`，早于这些提交产生的时间。
4. Actions 只有 test job 且不上传 artifact；发布包是本地构建后 `gh release create` 上传，不经 CI。
   CI 环境里不存在三个分片文件（已 gitignore），日志无从泄漏。

### 决定：不轮换

轮换需要逐个登录 8 个服务商后台重新签发、改配置、重启并验证，成本实打实；
而以上证据表明 GitHub 侧从未获得明文，**收益为零**。

### 但有一个独立问题需要单独记一笔

`Zhy.…`（16 位）**同一个值用在三处**：`cfworker_admin_token`、`cpa_mode.api_token`、`sub2api.password`。
`gmail.app_password` 与 `gmail.password` 也是同一值。

这不是泄漏，但一处失守等于三处失守。本次未改动（会牵动配置与服务重启），留作独立待办。

## 二、kyl_state.json：已公开，且不可撤回

| 项 | 值 |
| --- | --- |
| 加入 | `a628a74`，**2026-06-08** |
| 删除 | `5971ee8`，2026-06-12（只存在过 4 天，但永久留在历史里） |
| blob | `26d992fb1ac4ede5eca0e18c9f93abab3eebcf2f`，42267 字节 |
| 内容 | 183 条 `accounts`：`email` / `sub` / `name` / `preferred_username` / `lastLoginAt`（仅 10 条有值） |
| 域名 | `samaagi.edu.kyl23333.xyz`(96) 与 `sama.edu.kyl23333.xyz`(87) |

### 暴露范围

主仓库 + **抽查的 3 个 fork 全部可取**（`?ref=a628a74`）。187 个 fork 是 08-22 前的完整快照，
每一份都含这个 blob。

### 危害定性

- **无凭据**：不含 token / password / cookie / secret。`sub` 是 OIDC 标识符，不是凭据。
- **PII 属性弱**：全是一次性/别名邮箱，非真实个人身份。
- **真正的风险是风控关联**：183 的规模、命名规律（`agi_base_<10 位随机>`）、域名归属（`kyl23333.xyz`）、
  10 个登录时间戳——这些信息足以让风控方把整个域名拉黑并识别命名模式。

### 决定：不处置

再跑一次 filter-repo 剔除该路径只清主仓库，187 个 fork 里的副本纹丝不动，**收益近零**，
而且要再冒一次毁仓风险（2026-08-30 那次曾因 safe-delete 钩子导致 `.git/objects` 被清空）。

真止损只有一个办法：**换邮箱域名**，让已泄露的 183 个邮箱失效。仅在该域名仍在使用时才值得做。

## 三、补上的防线

根因不是"忘了加 .gitignore"，而是**基于文件名的防御必然漏掉下一个新文件名**。
所以补了一道内容门禁，拦在提交之前。

### `scripts/precommit_guard.py`

两层：

1. **文件名门禁**——`config.json` / `proxy.json` / `runtime.json` / `payment.json` /
   `*_state.json` / `*_tokens.txt` 等本地与运行时产物一律拒绝（`config.example.json` 等模板在白名单）。
   正是这一层能拦住 2026-08-30 的分片事故和 `kyl_state.json`。
2. **内容门禁**——厂商凭据前缀（`rk-e` / `nm_` / `cfat_` / `sk_live_` / `ghp_` / `AKIA` …）、
   `sensitive_policy.json` 中声明的敏感字段值、32 位以上 hex 常量、带凭据的代理 URL。

设计上刻意**宁可漏也不吵**。通用的"高熵 + 变量名含 key"检测器在本仓库产生 40+ 误报
（hCaptcha / reCAPTCHA 的 *site* key 本就公开、文档引用 `http://user:pass@host` 示例、
错误码常量是长字符串）。门禁一吵就会被 `--no-verify` 绕过，等于没有。
因此做了这些收窄：跳过 `tests/`、`docs/`、注释与 docstring；正则定义、f-string 模板、
占位形态的代理 URL 均放行；`pk_live_` 是 publishable key，不算密钥。

收窄后：**514 个跟踪文件全干净，零误报**；三条负向用例（凭据 JSON / `*_state.json` / 厂商前缀）全部拦截。
输出只含前 8 位与长度，绝不打印完整值。

### `.githooks/pre-commit` + `scripts/install_git_hooks.py`

钩子放在 `.githooks/`（入库、可共享），不放在 `.git/hooks/`（不入库、换机即失效）。
`core.hooksPath` 是本地配置，所以每个 clone 需要跑一次：

```bash
python scripts/install_git_hooks.py            # 安装
python scripts/install_git_hooks.py --status   # 查状态
python scripts/install_git_hooks.py --uninstall
```

### `tests/test_precommit_guard.py`（CI 侧的同款防线）

25 个用例，其中 `test_no_tracked_file_contains_credentials` 扫全量跟踪文件。
**CI 已经在跑 pytest，所以这道检查会在每次 push 自动执行，无需改 `.github/workflows/ci.yml`。**
即使开发者没装钩子，凭据进了 index 也会在 CI 失败。

## 四、待办

1. **拆分复用口令**：`Zhy.…` 一个值用于三处（cfworker / cpa_mode / sub2api），
   `gmail.app_password` 与 `gmail.password` 重复。需登后台重签。
2. **换邮箱域名**（仅当 `kyl23333.xyz` 仍在使用）：唯一能切断风控关联的办法。
3. `scripts/filter_replacements.py` / `pick_final_replacements.py` 里残留 6 位真凭据前缀
   （`b'556c27'` 等）与"已确认的真实凭据前缀"注释。6 位前缀不足以还原，不构成泄漏，
   但等于在公开仓库里给攻击者指路，建议改为环境变量传入。
4. 8 处硬编码的 Stripe `pk_live_` key。publishable key 公开无害，但硬编码别人的 key
   意味着对方轮换后代码即失效，属代码质量问题。

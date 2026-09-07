# 凭据轮换清单（2026-09-01）

## 背景

此前推上公开仓库的 `scripts/pick_final_replacements.py` 里有三个凭据的 **6 位前缀**。
凭据本体从未进过 GitHub（8 个分片凭据、Roxy token、代理口令都没上过版本库），
发布包里也确认不含 PII（kyl_state / mailbox_tokens / 分片配置 / sessions 全为 0 条）。

**6 位前缀不足以直接利用，但会显著缩小爆破面**，故按保守策略全部轮换。

| 时间 | 动作 |
|---|---|
| 09-01 03:07 | v2026.08.31 的三个资产（exe / zip / sha256）已从 GitHub Release 下架 |
| 09-01 03:22 | v2026.09.01 重建并发布，资产已确认干净，SHA-256 已对账 |
| 09-01 03:23 | 发布流程接入闸门（`scripts/scan_release_payload.py`），v2026.09.01 已上线 |

**轮换本身需要在服务商后台操作，只能由你完成。** 下面是精确清单与一键替换命令。

---

## 一、Roxy API token

| 项 | 值 |
|---|---|
| 位置 | `runtime.json` → `registration.drivers.roxy.api_token` |
| 当前长度 | 32 字符 |
| 影响范围 | 仅此一处 |
| 去哪换 | Roxy Browser 客户端 → 设置 → API / 开放接口 → 重新生成 token |
| 泄漏面 | 6 位前缀 |

**注意**：Roxy 配置里还有 `workspace_id` / `project_id`（各 6 位）、`api_base`。
换 token 时这三个通常不变；若 Roxy 后台重建了 workspace，需要一并更新。

---

## 二、smailr API key

| 项 | 值 |
|---|---|
| 位置 | `runtime.json` → `email_registration.smailr.api_key` |
| 当前长度 | 35 字符 |
| 影响范围 | 1 处 key + 4 个 `domain_ids`（各 36 字符） |
| 去哪换 | smailr 服务后台重新生成 API key |
| 泄漏面 | 6 位前缀 |

**两个要先确认的点**：

1. 事故脚本的注释里写的是「**旧** smailr key」。如果这个 key 已经停用，
   本项只需确认、无需轮换。
2. `email_registration.smailr.domain_ids` 下有 4 个域（`smailr.com` / `loc.cc` /
   `mail.nodeloc.cc` / `nodeloc.cc`）。换 key 后这些 domain_id **可能要重新拉取** ——
   取决于 smailr 的 key 是否与域绑定。换完 key 先小批量验证一封再放量。

**代码已支持环境变量**：`SMAILR_API_KEY`（`sms_tool/account_email_change.py:49`）。
轮换后如果想彻底不落盘，可以直接走环境变量。

---

## 三、代理账号口令

| 项 | 值 |
|---|---|
| 位置 | 代理 URL 的 password 部分，分布在两个配置文件 |
| `proxy.json` | 103 条（`us.ipwo.net:7878`） |
| `payment.json` | 148 条（`us` 62 / `as` 48 / `eu` 38） |
| 当前口令长度 | 9 字符；用户名 40 字符（内含地区与会话参数，不要动） |
| 去哪换 | ipwo.net 服务商后台修改账号密码 |
| 泄漏面 | 6 位前缀 |

**要先确认的点**：三个节点（us / as / eu）用的是不是**同一个代理账号**？
251 条 URL 的 user 长度都是 40、password 长度都是 9，看起来是同一个账号，
那样改一次密码即可；若分属不同账号，需要分别轮换并分次执行替换脚本。

**不建议动的位置**（替换脚本默认跳过）：

- `config.json` —— 已被分片完全旁路（`load_merged_config` 只要分片存在就永不读它），
  改它没有意义，反而制造"两处不一致"的困惑
- `runtime/liveness_scan.json` —— 运行产物，会自行重新生成
- `dist/` 下的副本 —— 发布包已重建

---

## 四、轮换后一键替换

已备好脚本 `scripts/_rotate_credentials.py`（**文件名以 `_` 开头，被 `.gitignore`
的 `scripts/_*.py` 覆盖，不会重蹈 pick_final_replacements.py 入库的覆辙**）。

值一律走环境变量，别写在命令行里，避免进 shell history：

```bat
set NEW_ROXY_TOKEN=<新 token>
set NEW_SMAILR_KEY=<新 key>
set NEW_PROXY_PASSWORD=<新口令>

rem 先干跑，确认改动范围
python scripts/_rotate_credentials.py

rem 确认无误后真正写入
python scripts/_rotate_credentials.py --apply
```

脚本行为：

- 默认干跑，`--apply` 才写盘
- 写入用 temp + `os.replace` 原子替换，先落 `.bak` 备份
- 输出只印路径、条数、主机名，**不打印任何值**（连前缀都不打）
- 可选 `--include-artifacts` 连运行产物一起改（通常没必要）
- 可选 `--proxy-host <后缀>` 追加其他代理主机

已用假值干跑验证过，命中：

```
runtime.json → registration.drivers.roxy.api_token   [将替换]
runtime.json → email_registration.smailr.api_key     [将替换]
proxy.json:   103 条  us.ipwo.net×103
payment.json: 148 条  as.ipwo.net×48, eu.ipwo.net×38, us.ipwo.net×62
```

---

## 五、轮换后验证

1. 重启桌面端（常驻 Python 进程会缓存配置，改完必须重启）
2. `python chatgpt_phone_reg.py --doctor` 检查环境
3. 单个账号跑一次注册，确认 Roxy 能拉起浏览器
4. 单封邮件确认 smailr 新 key 与 domain_id 有效
5. 单笔协议支付确认代理出口正常
6. 确认服务商侧旧口令 / 旧 key 已失效后，删除 `.bak` 备份

---

## 六、这次事故的根因（别再犯）

**清理动作必须按「泄漏通道」逐个重做。** git 对象、Release 资产、容器镜像、
包仓库、CI 缓存是彼此独立的通道。这次 git 历史干净了，发布资产没有 ——
只查 `git log` 永远发现不了。

配套加固已经落地：

- `scripts/scan_release_payload.py` —— 发布包闸门，核心规则是
  **凡是 `.gitignore` 拒绝的文件一律不许进包**，已在 `build_installer.ps1` 打包前接入
- `scripts/scan_hardcoded_secrets.py` —— 修好了 `ROOT` 三层 `dirname` 的 bug
  并补上退出码，此前它在 CI 上永远 exit 0

# 注册链路线上受控对照 Runbook

本 runbook 描述**如何受控地执行**并**如何判读**三项"已落地但尚未线上对照"的改动：

| # | 假设 | 开关 | 参考 |
| --- | --- | --- | --- |
| P0-1 | 预检打浏览器入口页比打 `auth.openai.com/log-in` 更少触发 Cloudflare | `registration.preflight_login_page`（`browser`/`legacy`） | `docs/audits/scan-2026-10-01-protocol-registration-payment-link.md` §3 |
| P0-2 | 预检失败路径确实把出口级拒绝识别为 Cloudflare 挑战 | 无（分类恒开，**仅观察**） | 同上 §3 P0-2 |
| P1-3 | 密码页 Sentinel flow bundle 不降低注册成功率、不引入 Sentinel 类失败 | `registration.sentinel_password_bundle`（默认 `false`） | 同上 §3 P1-3 |

工具：`scripts/registration_ab.py`（`plan` / `collect` / `compare`）。

> **边界**：该工具**不发起任何网络或注册流量**，只做设计打印、日志/报告归一化与对比。
> 它给出的 `verdict` 是**受控对照的判定辅助**，不是成功率结论；离线测试同样不能建立
> 线上成功率（见 [`protocol-registration.md`](protocol-registration.md) 的 Validation limits）。
> 真实运行必须由操作者在受控批次上执行。

---

## 1. 前置条件（必须全部满足）

- 同一邮箱批次：同一 provider、同一数量档、同一有效期形态。
- 同一出口池：同一 `proxy_seeds` / lane 配置；对照期间不轮换池子。
- 同一注册 driver、并发数、阶段超时与预检预算。
- 同一时间窗：Cloudflare 负载有时段性；两个 arm 尽量相邻或交叉进行。
- 已捕获每个 arm 的：
  - 运行日志 `runtime/logs/processes/<pid>/sms_tool.log`（预检行在这里）
  - 批次报告 `runtime/registration_target_<batch_id>.json`（含 `funnel`）
  - 该次运行使用的 `config.json` 快照（**操纵检查**，缺它则 `compare` 拒绝判定）

---

## 2. 读设计

```powershell
python scripts/registration_ab.py plan
```

输出为 JSON：每个实验的假设、开关、arms、held-constant 变量、主指标与判定规则。
阈值与手数在脚本顶部预注册（`MIN_ARM_ATTEMPTED`、`RATE_DELTA`），**看到数据后不得改**。

---

## 3. 跑与采集

以 P0-1 为例（P1-3 同法，仅换 `--experiment` 与 arms）：

```powershell
# arm A：browser（默认）
#   1) 确认 config.json 的 registration.preflight_login_page == "browser"
#   2) 跑常规注册批次，记下 batch_id 与进程日志路径
python scripts/registration_ab.py collect `
  --experiment p0-1-preflight-endpoint --arm browser `
  --log runtime/logs/processes/<pid>/sms_tool.log `
  --funnel runtime/registration_target_<batch_id>.json `
  --config config.json

# arm B：legacy
#   1) 把 config.json 改为 registration.preflight_login_page == "legacy"
#   2) 跑同规模批次
python scripts/registration_ab.py collect `
  --experiment p0-1-preflight-endpoint --arm legacy `
  --log runtime/logs/processes/<pid2>/sms_tool.log `
  --funnel runtime/registration_target_<batch_id2>.json `
  --config config.json
```

记录落在 `runtime/registration_ab/<experiment>__<arm>.json`。

> `collect` 会校验 `config.json` 与该 arm 期望的开关值一致；不一致时打印 WARNING 且记录
> `toggle_verified=false`，`compare` 将直接拒绝判定 —— 这是防止"以为改了、其实没改"的操纵检查。

---

## 4. 对比

```powershell
python scripts/registration_ab.py compare `
  --arm browser=runtime/registration_ab/p0-1-preflight-endpoint__browser.json `
  --arm legacy=runtime/registration_ab/p0-1-preflight-endpoint__legacy.json
```

输出含各 arm 的 `attempted` / `registered_per_attempted` / 预检探测数 / `cloudflare_rate` /
`no_healthy_route` / 失败分类，以及 `verdict` 与 `reason`。

---

## 5. 判定规则（预注册）

- 每个 arm 的 `attempted` 必须 ≥ `MIN_ARM_ATTEMPTED`（30），否则 `underpowered`。
- 速率差阈值 `RATE_DELTA = 0.05`。

| 实验 | 判定 |
| --- | --- |
| P0-1 | `cloudflare_rate` 更低且差距 > 0.05、且 `no_healthy_route` 不更高的一方胜；否则 `inconclusive` |
| P0-2 | `observation_only`：只报每出口挑战计数；流程内换出口需**单独开关 + 单独 A/B**才能落地 |
| P1-3 | bundle 成功率不低 0.05 以上且 Sentinel 类失败不增长 → `favor_bundle`；Sentinel 类失败增长或成功率低 0.05 以上 → `keep_default_off` |

---

## 6. 停止规则

- 任一 arm 出现 `no_healthy_route`：先排查出口池，**暂停对照**，不以该轮结论。
- 预检 Cloudflare 挑战率 > 50%：暂停，先修出口质量。
- 不得"看中间结果调阈值/加样本重新跑"——阈值与手数预注册，改动即为新的实验。
- 一次只改一个变量；P0-1 与 P1-3 不要同批复跑。

---

## 7. 证据与复现

- 记录文件：`runtime/registration_ab/<experiment>__<arm>.json`（含 `collected_at`、
  `log_sha256`、配置快照值、原始计数）。
- 结论回流：对照完成后，把结论写回 `docs/audits/` 的 `landing-*` 或更新
  [`protocol-registration.md`](protocol-registration.md)，并说明 arm、手数、判定。

---

## 8. 明确不做

- 不改任何默认值、端点或代码。
- 不把 `verdict` 当作成功率证据。
- 不用离线单测替代线上对照。
- 不把 P0-2 的观察结果当成"流程内换出口已生效"。

---

## 9. 相关

- [`protocol-registration.md`](protocol-registration.md) — 端点/Sentinel 契约与 Validation limits
- [`registration-architecture.md`](registration-architecture.md) — 注册依赖与生命周期
- [`telemetry-and-runtime.md`](telemetry-and-runtime.md) — 日志位置与关联字段
- `docs/audits/scan-2026-10-01-protocol-registration-payment-link.md` — P0-1/P0-2/P1-3 取证

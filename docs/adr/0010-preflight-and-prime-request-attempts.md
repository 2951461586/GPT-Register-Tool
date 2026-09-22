# ADR-0010: 预检与预热请求的 attempts=1

- Status: Accepted
- Date: 2026-09-18

`request_with_retry` 的默认尝试次数是配置值 `timeouts.http_retries`（**缺省 3**），
退避 `timeouts.retry_delay`（缺省 2.0s，序列 **2/4/8/15s**、上限 15s），单次
`timeout` 缺省 20s。它**只在**
`is_transient_transport_error(exc) and not is_terminal_registration_error(exc)`
时重试；403/429 **不重试**，改为在 session 上记一条 `blocked_until` 熔断。

2026-09-18 有两处调用点显式传 `attempts=1`：

| 调用点 | 位置 | 理由 |
| --- | --- | --- |
| `ChatGPT prime` | `registration_handlers.py` `RegistrationEmailWorkflow.auth_flow`，**仅 `passwordless` 分支** | 它是**预热**请求：`GET {chat_base}/` 的唯一作用是在该 session 上先铺好 cookie 与 TLS 连接，紧接着的 `Auth csrf` 才是真正被消费的调用。**它的返回值被丢弃**，所以重试它只增加启动延迟、不改变结果。 |
| 预检的每次检查 | `registration_preflight.py` `registration_network_preflight` | 预检是**闸门**不是投递路径。一个候选要跑 4 个检查，每个检查默认 3 次尝试 ⇒ 单检查最坏 `3×15s + 2s + 4s = 51s`；一个候选（4 检查 × 外层 2 次换出口）最坏可达 **≈408s**，而预检总预算只有 **180s** —— **一个候选就能吃光全部预算**。改成 1 次后单检查 15s、单候选最坏 `4×15×2 = 120s`，回到预算之内。 |

## 这不是「取消重试」

预检有**两层**重试，`attempts=1` 只去掉了**同出口**那一层：

1. **内层（已去掉）** —— 在同一个出口上把同一个请求重发到 `http_retries` 次。
2. **外层（保留）** —— `registration_network_preflight(proxy_attempts=2)` 失败后
   调 `refresh_proxy_sid(candidate)` **换一个出口**，把整套检查重跑一遍。

去掉内层的理由：它与观测到的失败模式不匹配。本项目实测的失败集中在
「**这条路径**到 `chatgpt.com` 不通」（出口侧超时，而非请求本身抖动），
同出口重发只是在反复测量这个出口有多坏，**换出口才是有效动作**。
`ChatGPT prime` 的实测也同向：首轮命中率 **91/126 = 72%**，剩下 28% 靠同出口
重试换回来的收益，与它带来的启动延迟不成比例。

## Consequences

- **预检对同出口的瞬时抖动更敏感**：一次瞬断现在会直接触发外层换出口、重跑整套
  检查，而不是先在原地重试。代价是「一次抖动 = 多跑一整套检查」；收益是
  「不会被一个坏出口拖满预算」。这是**有意**的取舍，不是副作用。
- 预检单候选最坏耗时从 ≈408s 降到 **120s**（< 180s 预算），预算从
  「一个候选就能吃光」变成「至少能试 3 个候选」。
- 密码分支的 `Auth prime`（`else` 分支）**没有**改，仍走默认 3 次。
  两个分支的预热行为因此**不同**；若日后统一，需重新评估密码分支的启动开销。
- 🔴 **`attempts` 只管瞬时传输错误，而且判据有两份表。**
  `is_transient_transport_error` 读的是 `http_client.TRANSIENT_MARKERS`，
  与批次层 `failure_registry` 的 `network` 清单是**两份独立的表**。当前差异：

  | 表 | `curl: (6)` | `curl: (18)` | 服务对象 |
  | --- | --- | --- | --- |
  | `http_client.TRANSIENT_MARKERS` | ✗ | ✗ | `request_with_retry` 的**原地**重试 |
  | `failure_registry` `network` | ✓ | ✓ | 批次层 `classify_error` 的**终态/可重试**判定 |

  ⇒ `curl: (18) Partial file` 现在会被批次层归为可重试的 `network`，
  但**不会**被 `request_with_retry` 原地重试（`(6)` 通常还因消息里含 `proxy`
  而被宽松标记顺带命中，`(18)` 不会）。

  **此处有意不合并**：两张表服务不同层，合并会把批次策略耦合进传输层，
  与 `http_client` 已有的「不反向依赖 policy」原则冲突（见 `http_client.py:154-157`
  的注释）。但**差异本身必须被知道** —— 改任何一张表时都要回头看另一张。

## 相关

- `docs/audits/scan-2026-09-18-latest-protocol-batch-diagnosis.md` §3「已确认为有意变更」
  与 §5④
- `sms_tool/http_client.py:65-114`（`TRANSIENT_MARKERS` / `is_transient_transport_error`）
- `sms_tool/backoff.py:12-17`（`transport_backoff`）
- `sms_tool/registration_preflight.py:104-171`（外层 `proxy_attempts` 换出口循环）
- `sms_tool/registration_handlers.py:725-745`（两个 prime 调用）

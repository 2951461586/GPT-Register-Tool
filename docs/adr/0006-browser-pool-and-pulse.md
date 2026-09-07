# ADR-0006: 浏览器进程池与脉冲波调度

- Status: Accepted
- Date: 2026-08-31（2026-09-05/06 随 browser_flow 拆分接线复核）

## 浏览器进程池

`sms_tool/browser_pool.py` 的 `BrowserProcessPool` 以进程为单位复用本地
Playwright 浏览器：账号级值（代理、locale、时区、identity、viewport）每次
会话传入，昂贵的浏览器进程按 `registration.browser_process_pool.*` 配置
（enabled / max_concurrent / max_uses_per_process / recycle_on_error）跨账号
复用，降级进程按使用次数回收重启。进程池由 `flow_steps._browser_session_scope`
按进程级单例接线，默认关闭（每次注册独立会话，行为与历史一致）。

槽位获取走 `_lock` 串行化；池键包含 `id(session_factory)`，配置不同的作用域
不会互相复用进程。

## 脉冲波调度

`batch_runner` 在 `workers > 1` 时可启用 `registration.pulse.enabled` 的
脉冲调度（`registration_pulse.run_pulse_batch`）：批次拆成离散波，波间检测
OTP 失败聚集（IP 封禁信号），触发 ban 停顿给代理轮换留时间。波间延迟与
ban 停顿使用可取消的分片睡眠（见 ADR-0005）。

## Consequences

- `camoufox` 未实现 `release_account_context` / `renew_account_context`，
  池化对它退化为每次全量重启（已知能力缺口）。
- "池禁用"路径每次注册调用 `close_browser_process_pool()`，进程级单例
  在配置翻转时正确重建。

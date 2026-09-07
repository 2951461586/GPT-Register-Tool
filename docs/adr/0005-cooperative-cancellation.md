# ADR-0005: 注册协作取消（registration_cancel）

- Status: Accepted
- Date: 2026-09-06

注册批次使用进程内协作取消：`sms_tool/registration_cancel.py` 持有一个全局
`threading.Event`，外加可选的 per-batch `cancel_event` 参数（双通道按位或）。
取消语义是**协作式的**：worker 在有界检查点观察到取消后，以
`error="registration_cancelled"` / `failure_class="cancelled"` /
`registration_state="cancelled"` 的结果收尾，而不是在账号执行中途被硬杀，
保证持久化状态与浏览器清理一致。

## 检查点位置

- `batch_runner._run_one`：账号开始前、每次 attempt 开始前、重试睡眠中
  （`cancellable_sleep` 分片睡眠，2s 粒度可中断）。
- `registration_pulse`：波间取消会把剩余账号标记为 cancelled；ban 停顿与
  波间延迟均可提前唤醒。
- 协议路径：`RegistrationEmailWorkflow.run` 进入前、每个 `_run_stage` 边界、
  OTP 轮询窗口之间（`otp_strategy._poll_registration_email_otp`）。协议路径
  的取消以 `RegistrationCancelled` 异常表达，`_run_stage` 对其直通（不归类
  为 stage transport 失败），`run()` 转成与浏览器路径相同的 cancelled 契约。
- 浏览器路径：`page_state._raise_if_registration_cancelled` 在每次页面操作
  前检查，`flow_steps._poll_browser_otp` 在每个轮询窗口前检查。

## 生产者与生命周期

- `cancel_scope()` 是批次级所有者：安装 SIGINT/SIGBREAK 处理器（第一次
  Ctrl+C 置位、第二次恢复 KeyboardInterrupt 硬中断），退出时恢复处理器并
  **清除全局标志**，保证取消不会泄漏到同进程的下一个批次。
- `request_registration_cancel()` 供程序化生产者（IPC 命令、测试）调用；
  `ensure_not_cancelled()` 供检查点抛异常。
- conftest 的 autouse fixture 在每个测试前后复位全局标志。

## Consequences

- 桌面端硬杀进程树仍是最后的强制手段；协作取消覆盖交互式 CLI 与未来
  IPC 生产者。
- 协议路径 OTP 轮询窗口内（最长 `resend_after_seconds`）取消粒度受窗口
  时长限制；更细的粒度需要给 mailbox poller 传入取消回调，暂不做。

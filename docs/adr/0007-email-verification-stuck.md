# ADR-0007: email_verification_stuck 分类与一次性 reload 重试

- Status: Accepted
- Date: 2026-09-06

OTP 提交成功后，验证 SPA 偶尔停留在 `auth.openai.com/*/email-verification`
路由（OTP 输入框仍挂载）。`page_state._post_otp_registration_state` 把该路由
单列为 `email_verification` 状态并尝试一次显式续进点击
（`_advance_email_verification`，只允许 Continue/Verify/Done 等明确标签）；
点击后仍在该路由则返回 `email_verification_stuck`。

orchestrator 对 `email_verification_stuck` 做一次 `page.reload` + 二次探测
（记 `create_account_retry` 阶段事件）；二次仍卡死则抛
`BrowserRegistrationError("browser_email_verification_stuck")`，被
`_browser_failure_class` 归类为 `auth_state`，同时进入
`error_classification.AUTH_STATE_ERROR_MARKERS`（协议与浏览器共用分类面），
批量重试策略据此判定可重试。

## Consequences

- 判定"卡死"需要完整走完一次显式续进 + 一次 reload 重探，避免把慢 SPA
  误判为死路由。
- 该错误码同时是失败结果 `registration_state` 与批次事件的稳定性锚点；
  改名必须同步 `error_classification.py` 与
  `docs/current/registration-recovery.md`。

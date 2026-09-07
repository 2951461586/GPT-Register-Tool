# Project Context

## Domain Terms

- **Mailbox provider**: an adapter that creates, reads, refreshes, or polls a mailbox.
- **Registration driver**: the protocol or browser implementation that performs account registration.
- **Registration operations**: the immutable set of dependencies bound for one protocol workflow invocation.
- **Registration runtime**: per-invocation resources, identity, auth, OTP, account and outcome state; not a persistence schema.
- **Retry policy**: shared failure decisions; session, stage and mailbox circuits retain separate state lifetimes.
- **Command ID / Run ID**: a backend-process correlation ID and its per-registration attempt ID.
- **Proxy lane**: an isolated egress purpose such as registration, mailbox/OTP, liveness, or payment.
- **Desktop read**: the read-only sanitized account/mailbox contract consumed by WPF.
- **Payment batch**: a resumable cohort execution with per-account terminal results.

## Ownership Rules

- `sms_tool/providers/` owns provider implementations; `mailbox.py` owns routing.
- `sms_tool/store/` owns persistence and emits facts, not provider side effects.
- `SmsWorkbench/` owns presentation and command planning; Python owns provider and protocol behavior.
- `docs/architecture.md` describes current boundaries; `docs/audits/` contains historical evidence; `docs/adr/` records decisions.

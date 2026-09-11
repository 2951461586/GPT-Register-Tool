# Account Health Contract

## Modes

`--one-click-scan` is probe-only by default. It loads the saved account and
calls the canonical AT liveness probe; it does not start OAuth, mailbox OTP, or
phone verification. Use `--scan-deep-probe` only when an operator explicitly
accepts those side effects.

`--quota-auto-relogin` is a separate recovery option for a 401 result. Recovery
may use refresh tokens, browser sessions, or email authentication according to
`--scan-relogin-mode` and is not part of a normal liveness probe.

## Result semantics

- `probe_ok` / `health_state` describe the remote account observation.
- `persisted` describes whether the local marker was written.
- A persistence failure must not turn a healthy remote probe into an unhealthy
  account result.

Promotion checks are a separate `plan` check. They may inspect trial payment
methods, but the capability probe must stop before payment-method creation,
confirmation, or charge.

## Promotion status contract

The 优惠状态 badge has two representations:

- `promotion_status` — the Chinese display label ("可试用Plus·-100%·×1month").
  Display copy only.
- `promotion_state` — the machine state (`trial_eligible`, `subscribed`,
  `free`, `auth_invalid`, `probe_failed`, `unknown`), defined in
  `sms_tool/promotion_states.py` and pinned cross-language by
  `tests/fixtures/promotion_status_cases.json`. Filtering, sorting and
  branching must key off the state, never the label.

`promotion_marker_is_stale` in the same module is the single owner of the
"401 promotion marker cleared by a later verified AT-200" rule;
`desktop_read` applies it at display time and `account_recovery` at
persistence time. Both sides of the desktop read payload surface
`promotion_state` alongside `promotion_status`.

## Proxy precedence

Operation-specific callers must resolve proxies in this order:

1. Explicit command proxy
2. Registration affinity for accounts that have one
3. Operation pool (`liveness`, `promotion`, or browser health)
4. Configured fallback

The selected source should be retained in diagnostics as a non-sensitive label.

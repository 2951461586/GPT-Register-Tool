# v2026.08.19

## Registration correctness

- Fixed protocol registration treating an intermediate OpenAI `/login` redirect as proof that an email was already registered. The flow now continues the username step and confirms the resulting auth state.
- Prevented duplicate terminal registration-progress events. A failed attempt now contributes one failure rather than being counted by both the workflow and persistence layers.
- Removed the protocol-registration stage-matrix popup. Live progress remains on the task row, while the embedded protocol-payment matrix is unchanged.
- Live verification registered 3 previously unused iCloud mailboxes successfully, saved 3 sessions, and received HTTP 200 from all access-token probes.
- Follow-up log diagnosis separated HTTP 429 `rate_limit_exceeded` from auth-state failures, disabled immediate retries for rate limits, serialized auth-flow admission, and added a batch cooldown circuit after the first upstream 429.

## Proxy and payment routing

- Added compatible import for `host:port:username:password` proxy entries with automatic HTTP/SOCKS probing.
- Updated registration and payment proxy defaults for the configured US, JP, and GB routes.
- PayPal Approve routing now accepts the general country catalog instead of rejecting countries outside the former PayPal-only list.

## Validation

- Python focused registration/payment/proxy suite: 101 passed.
- Python full suite before the follow-up fix: 987 passed, 28 subtests passed; after the fix: 991 passed, 28 subtests passed.
- .NET suite: 210 passed.
- Desktop publish: `dist/net10/SmsWorkbench.exe`.
- Sensitive-field scan, architecture scan, Python compileall, and `git diff --check` passed.

# Randomness Classification (`random` vs `secrets`)

`python-insecure-random` is a heuristic: it flags every `random.*` call. This
document records the review that decided, per call site, whether the value is a
**security-sensitive identifier** (must come from `secrets`) or **presentation
noise** (`random` is correct and cheaper).

## Decision rule

Use `secrets` when **predicting the value gives an attacker leverage**:

- it authenticates, authorises, or is presented as proof of identity;
- it selects which egress (proxy session) a request uses, so predicting it lets
  another caller force a shared IP;
- it is a correlation id echoed to a third party (trace/parent ids), where
  prediction permits trace injection.

Use `random` when the value only needs to be *varied*, not *unguessable*:

- browser fingerprints, seeded deterministically from a SHA-256 digest;
- billing names, addresses, postal codes, generated buyer e-mail local parts;
- human-like delays, typing cadence, `time_on_page`, retry jitter;
- proxy-pool shuffle/selection order (all candidates are trusted);
- display names and profile labels.

## Reviewed call sites

| Site | Value | Decision |
| --- | --- | --- |
| `sms_tool/transport/auth_headers.py` `datadog_trace_headers` | `traceparent` / parent id | **`secrets.randbits(64)`** — echoed to OpenAI telemetry |
| `sms_tool/transport/auth_headers.py` `_device_profile_for` | screen/DPR/CPU choices | `random.Random(sha256(seed))` — deterministic fingerprint |
| `sms_tool/transport/auth_headers.py` `_auth_fingerprint` | profile pick | already `random.SystemRandom` |
| `sms_tool/proxy/entry.py` `_random_session_id` | sticky proxy session id | **`secrets.choice`** — egress selection |
| `sms_tool/utils.py` name/birthday/jitter helpers | fake persona, delays | `random` |
| `sms_tool/registration/humanize.py`, `sms_tool/registration/warmup.py` | delay jitter | `random` |
| `sms_tool/transport/fingerprint_pool.py` | weighted profile pick | `random.choices` |
| `sms_tool/paypal/*` | click/typing cadence | `random` |

## Guarding against regressions

The rule above is the review criterion, not an automated gate: `random` in a
new *presentation* helper is fine, and a static rule cannot tell the two apart.
When adding a value that authenticates, proves identity, selects an egress, or
is echoed to a third party, use `secrets` and add the site to the table above.

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
| `sms_tool/auth_headers.py` `datadog_trace_headers` | `traceparent` / parent id | **`secrets.randbits(64)`** — echoed to OpenAI telemetry |
| `sms_tool/auth_headers.py` `_device_profile_for` | screen/DPR/CPU choices | `random.Random(sha256(seed))` — deterministic fingerprint |
| `sms_tool/auth_headers.py` `_auth_fingerprint` | profile pick | already `random.SystemRandom` |
| `sms_tool/proxy_entry.py` `_random_session_id` | sticky proxy session id | **`secrets.choice`** — egress selection |
| `services/protocol-payment/pix/pix_core.py` `random_proxy_sid` | sticky proxy session id | **`secrets.choice`** |
| `services/protocol-payment/direct_card/direct_card_extract.py` `new_proxy_session_id` | sticky proxy session id | **`secrets.randbelow`** |
| `services/protocol-payment/common/provider_profile.py` `build_email` | buyer e-mail local part | `random` — presentation only |
| `services/protocol-payment/*/…_extract.py` billing helpers | name / address / postal / phone | `random` |
| `services/protocol-payment/kakao/kakao_extract.py` `random_kakao_billing` | billing identity | `random.Random(sha256(token + uuid4))` — already CSPRNG-seeded |
| `sms_tool/utils.py` name/birthday/jitter helpers | fake persona, delays | `random` |
| `sms_tool/humanize.py`, `sms_tool/registration_warmup.py` | delay jitter | `random` |
| `sms_tool/fingerprint_pool.py` | weighted profile pick | `random.choices` |
| `sms_tool/paypal/*`, `sms_tool/upi_link/*` | click/typing cadence | `random` |
| `services/protocol-payment/*` proxy `shuffle` / `sample` | candidate order | `random` — every candidate is trusted |

## Guarding against regressions

The rule above is the review criterion, not an automated gate: `random` in a
new *presentation* helper is fine, and a static rule cannot tell the two apart.
When adding a value that authenticates, proves identity, selects an egress, or
is echoed to a third party, use `secrets` and add the site to the table above.

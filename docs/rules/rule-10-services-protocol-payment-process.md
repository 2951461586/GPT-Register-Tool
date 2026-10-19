# Rule 10 — `services/protocol-payment/` is a process boundary with zero import-level coupling to `sms_tool`; the only permitted reference is the documented lazy import in `kakao/kakao_extract.py`

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 10`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "from sms_tool\|import sms_tool" services/`
- **Source section**: 依赖关系 (services/protocol-payment)

## Rule

`services/protocol-payment/` is a process boundary with zero import-level coupling to `sms_tool`; the only permitted reference is the documented lazy import in `kakao/kakao_extract.py`.

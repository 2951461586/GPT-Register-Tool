# Rule 18 — A watched-hub ratchet must resolve the hub from the **import statement**, never from the importing file or a bare name match

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 18`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `pytest tests/governance/test_mailbox_private_import_ratchet.py` (25 cases), including `test_same_named_module_outside_the_package_is_rejected` and `test_a_watched_module_is_not_its_own_consumer`
- **Source section**: Ownership Matrix; Dependency Direction

## Rule

A watched-hub ratchet must resolve the hub from the **import statement**, never from the importing file or a bare name match. `load_mailbox_pool` is imported from `.mailbox` by `sms_tool/cli.py`, so the consumer is `cli` and the hub is `mailbox`; conversely `mailbox/service.py` imports `mailbox_email_config` *from* `.mailbox`, so it is a consumer of `mailbox`, not a hub. Resolution is therefore on the last dotted segment of the `ImportFrom` **plus** a package check: `from sms_tool.providers.mailbox import ...` is accepted for a hub that may live in `providers/`, while `from third_party.utils import ...` is rejected — `utils` and `mail_otp` are generic enough that a vendor module could share the name. A hub is never counted as a consumer of its own surface.

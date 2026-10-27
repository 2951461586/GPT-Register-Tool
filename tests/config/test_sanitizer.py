from sms_tool.diagnostics import SanitizingTextIO, safe_print
from sms_tool.kernel.sanitizer import (
    POLICY_SCHEMA,
    SENSITIVE_POLICY,
    account_reference,
    mask_account,
    sanitize,
    sanitize_command_args,
    sanitize_log_text,
    sanitize_text,
)


def test_sanitizer_removes_complete_token_secret_and_card_values():
    text = "access_token=eyJabcdefgh.ijklmnop.qrstuvwx refresh_token=rt_super-secret BA-abcDEF123456 totp_secret=JBSWY3DPEHPK3PXP card_number=4242424242424242"
    safe = sanitize_text(text)
    for fragment in ("eyJabcdefgh", "rt_super", "BA-abc", "JBSWY3", "424242"):
        assert fragment not in safe


def test_sanitizer_recurses_for_ipc_and_reports_without_prefixes():
    safe = sanitize(
        {
            "access_token": "at-visible-prefix",
            "nested": {"totp_secret": "totp-visible-prefix", "error": "Bearer bearer-value"},
            "cardNumber": "4111111111111111",
        }
    )
    assert safe["access_token"] == "[REDACTED]"
    assert safe["nested"]["totp_secret"] == "[REDACTED]"
    assert safe["nested"]["error"] == "Bearer [REDACTED]"
    assert safe["cardNumber"] == "[REDACTED]"


def test_shared_sensitive_policy_schema_is_loaded():
    assert SENSITIVE_POLICY["schema"] == POLICY_SCHEMA
    assert any(item["name"] == "named_secret" for item in SENSITIVE_POLICY["text_patterns"])


def test_sanitizer_redacts_named_stripe_session_and_intent_fields_without_corrupting_urls():
    checkout_id = "cs_live_fixtureSession123"
    intent_id = "pi_test_fixtureIntent123"
    assert sanitize({"checkout_session_id": checkout_id, "payment_intent_id": intent_id}) == {
        "checkout_session_id": "[REDACTED]",
        "payment_intent_id": "[REDACTED]",
    }
    payment_url = f"https://pay.openai.com/c/pay/{checkout_id}"
    assert sanitize_text(payment_url) == payment_url


def test_command_arguments_use_shared_sensitive_option_policy():
    assert sanitize_command_args(["--proxy", "http://user:pass@example:80", "--count", "2"]) == [
        "--proxy",
        "[REDACTED]",
        "--count",
        "2",
    ]
    assert (
        sanitize_command_args(["--access-token=secret", "--email=user@example.com"])[0] == "--access-token=[REDACTED]"
    )


def test_safe_print_sanitizes_operator_output(capsys):
    safe_print("proxy=http://user:pass@example:80")
    output = capsys.readouterr().out
    assert "user:pass" not in output
    assert "[REDACTED]" in output


def test_account_labels_are_masked_and_persisted_refs_are_stable():
    assert mask_account("User.Name@example.com") == "Us***@example.com"
    assert account_reference("User.Name@example.com") == account_reference(" user.name@EXAMPLE.COM ")
    assert "user.name" not in account_reference("User.Name@example.com")
    assert "user.name@example.com" not in sanitize_log_text("failed user.name@example.com")


def test_sanitizing_stdio_enforces_policy_for_legacy_prints():
    import io

    target = io.StringIO()
    stream = SanitizingTextIO(target)
    print("access_token=raw-secret", file=stream)
    assert target.getvalue() == "access_token=[REDACTED]\n"


# ---------------------------------------------------------------------------
# Round-5 regression cases (2026-09-02)
#
# An audit found six redaction gaps, all caused by the policy matching only
# FULL key names instead of fragments. The absurd part: `cookie_header` was in
# the policy but bare `cookie` was not - while `cookie` is the single most
# important credential carrier in this project. Same for `session_token` being
# listed while `session_id` was not.
#
# These cases use obviously-fake values so they can never carry a real secret.
# ---------------------------------------------------------------------------

_FAKE = "ZZFAKE123456"


def test_bare_cookie_and_session_id_keys_are_redacted():
    for key in ("cookie", "session_id", "session_cookie", "set_cookie"):
        assert sanitize({key: _FAKE}) == {key: "[REDACTED]"}, key


def test_prefixed_api_key_fields_are_redacted():
    # `api_key` was listed but exact-name matching missed every prefixed
    # variant, so `smsbower_api_key` / `smailr_api_key` / `remail_api_key`
    # all passed through in the clear.
    for key in ("api_key", "apiKey", "smsbower_api_key", "smailr_api_key", "remail_api_key"):
        assert sanitize({key: _FAKE}) == {key: "[REDACTED]"}, key


def test_bare_token_query_and_set_cookie_header_are_redacted():
    # The `named_secret` pattern listed access_/refresh_/id_/session_ tokens
    # but not the bare `token` used in callback URLs.
    assert _FAKE not in sanitize_text(f"https://x.example/cb?token={_FAKE}")
    assert _FAKE not in sanitize_text(f"set-cookie: session={_FAKE}; Path=/")


def test_bare_proxy_credentials_without_url_scheme_are_redacted():
    # `proxy_credentials` required a `://` prefix, so the plain
    # `user:pass@host:port` form stored in configs was never touched.
    assert sanitize_text("user:pw123@host.example.com:8080") == "[REDACTED]@host.example.com:8080"
    assert "pw123" not in sanitize({"proxy": "user:pw123@host.example.com:8080"})["proxy"]


def test_proxy_passwords_containing_an_at_sign_are_fully_redacted():
    """``urlsplit`` splits the authority at the **last** ``@``; the masker must too.

    Measured 2026-10-26: the URL rule stopped at the first ``@``, so
    ``http://alice:p@ssmarker@host:2000`` was rendered as
    ``http://[REDACTED]@ssmarker@host:2000`` — the tail of the password survived
    in every log line and every desktop panel that loads this policy. The bare
    ``user:pass@host:port`` rule had the mirror defect: it excluded ``@`` from the
    password class, so a credential containing one matched nothing at all and was
    printed in full. ``proxy/entry._mask_credentials`` had already been fixed for
    the URL form; these are the two copies that see arbitrary log text.
    """
    tail = "ssmarker"

    url = f"http://alice:p@{tail}@gate.example:2000"
    assert tail not in sanitize_text(url)
    assert sanitize_text(url) == "http://[REDACTED]@gate.example:2000"

    socks = f"socks5h://alice:p@{tail}@gate.example:2000"
    assert tail not in sanitize_text(socks)
    assert sanitize_text(socks) == "socks5h://[REDACTED]@gate.example:2000"

    bare = f"alice:p@{tail}@gate.example:2000"
    assert tail not in sanitize_text(bare)
    assert sanitize_text(bare) == "[REDACTED]@gate.example:2000"


def test_the_proxy_url_rule_does_not_cross_a_path():
    # The greedy authority class is ``[^/\s]*``, so a path (or a query carrying
    # an ``@``) stops it: over-matching a URL into the next token would corrupt
    # otherwise harmless text.
    url = "http://gate.example:2000/path?next=a@b"
    assert sanitize_text(url) == url


def test_phone_proxy_text_redaction_splits_at_the_last_at_sign():
    """``redact_proxy_text`` carries the same rule for arbitrary message text."""
    from sms_tool.phone_proxy import redact_proxy_text

    assert redact_proxy_text("connect http://alice:p@ssmarker@gate.example:2000 failed") == (
        "connect http://***:***@gate.example:2000 failed"
    )
    assert "ssmarker" not in redact_proxy_text("socks5h://alice:p@ssmarker@gate.example:2000")


def test_path_exemption_is_surgical_not_global():
    # `session_id` means two different things in this codebase. Inside
    # `proxy_affinity` it is the sticky ID that `_restore_session()` splices
    # back into the proxy username/password to keep an account on the same exit
    # IP - redacting it built `sid-[REDACTED]` credentials that failed to
    # connect, with no error anywhere. Everywhere else it is a real session
    # credential and must still be redacted.
    #
    # The point of this test is that the exemption is PATH-scoped: narrowing it
    # breaks proxy affinity, widening it re-opens the leak.
    proxy = "http://user-region-US-sid-NEW5678-t-5:proxy-secret@proxy.example:443"

    assert sanitize({"session_id": _FAKE}) == {"session_id": "[REDACTED]"}
    assert sanitize({"identity_context": {"proxy_affinity": {"session_id": "NEW5678"}}}) == {
        "identity_context": {"proxy_affinity": {"session_id": "NEW5678"}}
    }
    # A look-alike path must NOT inherit the exemption.
    assert sanitize({"other_affinity": {"session_id": _FAKE}}) == {"other_affinity": {"session_id": "[REDACTED]"}}
    # The credentials sitting next to the routing ID are still redacted.
    assert "proxy-secret" not in sanitize({"proxy": proxy})["proxy"]


def test_exempted_affinity_still_rebuilds_a_usable_proxy():
    # End-to-end guard for the bug above: a sanitized identity_context must
    # still round-trip into the exact proxy it was captured from.
    from sms_tool.accounts.account_identity import create_registration_identity, resolve_account_proxy

    proxy = "http://user-region-US-sid-NEW5678-t-5:proxy-secret@proxy.example:443"
    identity = create_registration_identity(proxy, pool_index=0, fingerprint_key="chrome146", device_id="device-123")
    cleaned = sanitize({"identity_context": identity})["identity_context"]
    rebuilt = resolve_account_proxy(
        {"identity_context": cleaned},
        fallback_proxy=proxy,
        config={"proxy": {"pool": [proxy]}},
    )
    assert rebuilt == proxy, (
        f"sanitize() corrupted the proxy affinity session id, so the saved exit IP can no longer be reused: {rebuilt!r}"
    )


def test_non_credential_keys_stay_readable():
    # Over-redaction is its own failure mode: it makes logs and reports useless
    # and trains people to bypass the sanitizer. Pin the fields that must
    # survive - notably that the `api_key` fragment did not become bare `key`,
    # which would have swallowed `keyword` and `sort_key` too.
    payload = {
        "keyword": "registration",
        "sort_key": "created_at",
        "monkey": "lab",
        "proxy_country": "JP",
        "use_proxy": True,
        "token_count": 5,
        "email": "user@example.com",
        "plan_type": "plus",
        "license_type": "team",
    }
    assert sanitize(payload) == payload


def test_handoff_storage_state_is_redacted_as_a_whole_subtree():
    """The protocol -> browser handoff payload carries live session cookies.

    Decided 2026-10-05 (see ``docs/audits/plans/plan-2026-10-05-inflow-challenge-handoff.md``
    §3.4/§7): the handoff may be written under ``runtime/``, and its storage
    state must be covered by ``sensitive_policy.json``.

    Before this, the cookie values were protected only *by accident*: the inner
    key happened to be spelled ``cookies``, which matched the ``cookie``
    fragment, so ``sanitize`` replaced the list with ``[REDACTED]``. That left
    the enclosing field name and its siblings visible and made the protection
    depend on an inner spelling -- a ``jar`` / ``cookie_list`` variant would have
    leaked the values in the clear. The exact key plus the ``storage_state``
    fragment close both halves: the whole subtree collapses to one string, and a
    renamed variant is covered too.
    """
    payload = {
        "handoff_storage_state": {
            "cookies": [{"name": "oai-did", "value": _FAKE, "secure": True}],
            "origins": [],
        },
        "handoff_resume_url": "https://auth.openai.com/create-account/password",
        "handoff_flow": "authorize_continue",
        "handoff_attempt": 1,
    }
    redacted = sanitize(payload)
    # Whole subtree, not a partially-redacted mapping: nothing about the cookie
    # jar's shape survives, so no downstream consumer can be tempted to use it.
    assert redacted["handoff_storage_state"] == "[REDACTED]"
    assert _FAKE not in str(redacted)
    # The non-credential diagnostic siblings must stay readable -- a guard that
    # redacts everything is a guard someone turns off.
    assert redacted["handoff_flow"] == "authorize_continue"
    assert redacted["handoff_attempt"] == 1
    assert redacted["handoff_resume_url"].startswith("https://auth.openai.com/")
    # Renamed variants are the actual risk: the value is still a cookie jar.
    for key in ("storage_state", "storage_state_json", "protocol_storage_state"):
        assert sanitize({key: {"jar": [{"value": _FAKE}]}})[key] == "[REDACTED]", key

from sms_tool.diagnostics import SanitizingTextIO, safe_print
from sms_tool.sanitizer import POLICY_SCHEMA, SENSITIVE_POLICY, sanitize, sanitize_command_args, sanitize_text


def test_sanitizer_removes_complete_token_secret_and_card_values():
    text = "access_token=eyJabcdefgh.ijklmnop.qrstuvwx refresh_token=rt_super-secret BA-abcDEF123456 totp_secret=JBSWY3DPEHPK3PXP card_number=4242424242424242"
    safe = sanitize_text(text)
    for fragment in ("eyJabcdefgh", "rt_super", "BA-abc", "JBSWY3", "424242"):
        assert fragment not in safe


def test_sanitizer_recurses_for_ipc_and_reports_without_prefixes():
    safe = sanitize({"access_token": "at-visible-prefix", "nested": {"totp_secret": "totp-visible-prefix", "error": "Bearer bearer-value"}, "cardNumber": "4111111111111111"})
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
        "--proxy", "[REDACTED]", "--count", "2",
    ]
    assert sanitize_command_args(["--access-token=secret", "--email=user@example.com"])[0] == "--access-token=[REDACTED]"


def test_safe_print_sanitizes_operator_output(capsys):
    safe_print("proxy=http://user:pass@example:80")
    output = capsys.readouterr().out
    assert "user:pass" not in output
    assert "[REDACTED]" in output


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
    assert sanitize({"other_affinity": {"session_id": _FAKE}}) == {
        "other_affinity": {"session_id": "[REDACTED]"}
    }
    # The credentials sitting next to the routing ID are still redacted.
    assert "proxy-secret" not in sanitize({"proxy": proxy})["proxy"]


def test_exempted_affinity_still_rebuilds_a_usable_proxy():
    # End-to-end guard for the bug above: a sanitized identity_context must
    # still round-trip into the exact proxy it was captured from.
    from sms_tool.account_identity import create_registration_identity, resolve_account_proxy

    proxy = "http://user-region-US-sid-NEW5678-t-5:proxy-secret@proxy.example:443"
    identity = create_registration_identity(
        proxy, pool_index=0, fingerprint_key="chrome146", device_id="device-123"
    )
    cleaned = sanitize({"identity_context": identity})["identity_context"]
    rebuilt = resolve_account_proxy(
        {"identity_context": cleaned},
        fallback_proxy=proxy,
        config={"proxy": {"pool": [proxy]}},
    )
    assert rebuilt == proxy, (
        "sanitize() corrupted the proxy affinity session id, so the saved exit "
        f"IP can no longer be reused: {rebuilt!r}"
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

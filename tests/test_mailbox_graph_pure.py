"""Behaviour tests for ``sms_tool/mailbox_graph.py`` (2026-09-03, round 7).

39 lines, one function, **zero test coverage** -- and it sits on the mailbox auth
path for every Microsoft-account mailbox.  The reason it matters is one line in
``mailbox_strategies.py:271``::

    reraise=(MailboxTokenExpiredError,),

The polling loop re-raises ``MailboxTokenExpiredError`` (→ the mailbox is treated
as dead and the account is given up on) but **swallows** a plain ``RuntimeError``
(→ keep polling).  So the distinction between those two exception types is
load-bearing: get it wrong and either a permanently-dead mailbox burns the whole
timeout window, or a transient network blip silently kills a good account.

Production injects the HTTP client by assignment, not by import::

    # sms_tool/mailbox.py:493
    mailbox_graph.curl_requests = curl_requests

so ``curl_requests`` is a module attribute that production overwrites at call
time.  Tests patch the same attribute -- and note this makes it a
patch-injection-surface: "cleaning up" that assignment would silently switch the
module to its own import.

Patch seam: ``mailbox_graph.curl_requests``.  Nothing in this file touches the
network for real; every ``post`` is recorded, never sent.

Quirks pinned, not fixed:

* The 500-char truncation in ``{"raw": r.text[:500]}`` is silent -- a truncated
  body can no longer contain the ``invalid_grant`` marker, so an HTML error page
  longer than 500 chars is always classified as a generic failure.
* ``client_id`` comes from ``mailbox.token`` first, which is a *different* field
  from ``mailbox.access_token`` -- a mailbox object carrying a token in the
  ``token`` attribute silently overrides the configured client id.
"""
from __future__ import annotations

import unittest
from unittest import mock

from sms_tool import mailbox_graph

DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
DEFAULT_SCOPE = "https://graph.microsoft.com/.default offline_access"
DEFAULT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


class _Response:
    """Only three attributes are used by the code under test."""

    def __init__(self, status_code=200, payload=None, text="", raises=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeRequests:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class _Mailbox:
    """Only the four attributes the function actually reads/writes."""

    def __init__(self, email="box@example.test", refresh_token="rt-1", token=None):
        self.email = email
        self.refresh_token = refresh_token
        self.access_token = ""
        if token is not None:
            self.token = token


class _RefreshCase(unittest.TestCase):
    def run_refresh(self, response, *, mailbox=None, cfg=None,
                    proxy=None, scope_override=None):
        """Returns ``(outcome, fake, mailbox, cfg)``.

        ``outcome`` is either the returned token or the raised exception.
        """
        box = mailbox if mailbox is not None else _Mailbox()
        fake = _FakeRequests(response)
        with mock.patch.object(mailbox_graph, "curl_requests", fake):
            try:
                result = mailbox_graph.ms_oauth_refresh(
                    box, cfg if cfg is not None else {},
                    proxy=proxy, scope_override=scope_override,
                )
            except Exception as exc:
                result = exc
        return result, fake, box, (cfg or {})


class GuardTests(_RefreshCase):
    def test_a_missing_refresh_token_raises_before_any_request(self):
        box = _Mailbox(refresh_token="")
        outcome, fake, _, _ = self.run_refresh(_Response(), mailbox=box)
        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn("refresh_token is required", str(outcome))
        self.assertEqual(fake.calls, [], "no network call on the guard path")

    def test_a_none_refresh_token_raises_too(self):
        box = _Mailbox(refresh_token=None)
        outcome, fake, _, _ = self.run_refresh(_Response(), mailbox=box)
        self.assertIsInstance(outcome, RuntimeError)
        self.assertEqual(fake.calls, [])

    def test_the_expired_error_is_a_runtime_error_subclass(self):
        """``mailbox.py:400`` and ``mailbox_strategies.py:271`` both catch it.
        If it ever stops being a ``RuntimeError``, every ``except RuntimeError``
        upstream silently changes meaning."""
        self.assertTrue(issubclass(mailbox_graph.MailboxTokenExpiredError, RuntimeError))


class RequestShapeTests(_RefreshCase):
    def test_the_default_endpoint_and_impersonation(self):
        """⚠️ Pinned: ``impersonate="chrome124"`` and ``timeout=30`` are the TLS
        fingerprint and the bound on how long one account can block a worker.
        Neither is covered by any config key."""
        _, fake, _, _ = self.run_refresh(
            _Response(payload={"access_token": "at"}))
        call = fake.calls[0]
        self.assertEqual(call["url"], DEFAULT_TOKEN_URL)
        self.assertEqual(call["impersonate"], "chrome124")
        self.assertEqual(call["timeout"], 30)

    def test_the_form_payload_carries_the_refresh_grant(self):
        box = _Mailbox(refresh_token="rt-abc")
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}),
                                         mailbox=box)
        self.assertEqual(fake.calls[0]["data"], {
            "grant_type": "refresh_token",
            "client_id": DEFAULT_CLIENT_ID,
            "refresh_token": "rt-abc",
            "scope": DEFAULT_SCOPE,
        })

    def test_no_proxy_means_no_proxies_key(self):
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}))
        self.assertIsNone(fake.calls[0]["proxies"])

    def test_a_proxy_is_applied_to_both_schemes(self):
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}),
                                         proxy="http://127.0.0.1:7897")
        self.assertEqual(fake.calls[0]["proxies"],
                         {"http": "http://127.0.0.1:7897",
                          "https": "http://127.0.0.1:7897"})


class ClientIdTests(_RefreshCase):
    """``getattr(mailbox, "token", "") or cfg[...] or <hardcoded>`` -- a three
    level chain where every level has to be asserted separately."""

    def test_the_mailbox_token_attribute_wins_over_the_config(self):
        box = _Mailbox(token="from-mailbox")
        cfg = {"oauth_client_id": "from-cfg"}
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}),
                                         mailbox=box, cfg=cfg)
        self.assertEqual(fake.calls[0]["data"]["client_id"], "from-mailbox")

    def test_the_config_wins_over_the_hardcoded_default(self):
        cfg = {"oauth_client_id": "from-cfg"}
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}),
                                         cfg=cfg)
        self.assertEqual(fake.calls[0]["data"]["client_id"], "from-cfg")

    def test_the_hardcoded_default_is_used_when_nothing_is_supplied(self):
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}))
        self.assertEqual(fake.calls[0]["data"]["client_id"], DEFAULT_CLIENT_ID)

    def test_an_empty_mailbox_token_falls_through_to_the_config(self):
        """``or`` on the attribute, not ``is not None`` -- an empty string is
        treated as absent."""
        box = _Mailbox(token="")
        cfg = {"oauth_client_id": "from-cfg"}
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}),
                                         mailbox=box, cfg=cfg)
        self.assertEqual(fake.calls[0]["data"]["client_id"], "from-cfg")


class ScopeAndUrlTests(_RefreshCase):
    def test_scope_override_beats_the_config(self):
        _, fake, _, _ = self.run_refresh(
            _Response(payload={"access_token": "at"}),
            cfg={"oauth_scope": "cfg-scope"}, scope_override="override-scope")
        self.assertEqual(fake.calls[0]["data"]["scope"], "override-scope")

    def test_the_configured_scope_beats_the_default(self):
        _, fake, _, _ = self.run_refresh(
            _Response(payload={"access_token": "at"}), cfg={"oauth_scope": "cfg-scope"})
        self.assertEqual(fake.calls[0]["data"]["scope"], "cfg-scope")

    def test_the_default_scope_is_the_graph_offline_access_pair(self):
        _, fake, _, _ = self.run_refresh(_Response(payload={"access_token": "at"}))
        self.assertEqual(fake.calls[0]["data"]["scope"], DEFAULT_SCOPE)

    def test_the_token_url_is_configurable(self):
        _, fake, _, _ = self.run_refresh(
            _Response(payload={"access_token": "at"}),
            cfg={"oauth_token_url": "https://example.test/token"})
        self.assertEqual(fake.calls[0]["url"], "https://example.test/token")


class HappyPathTests(_RefreshCase):
    def test_the_access_token_is_returned_and_stored(self):
        box = _Mailbox()
        outcome, _, box, _ = self.run_refresh(
            _Response(payload={"access_token": "at-1"}), mailbox=box)
        self.assertEqual(outcome, "at-1")
        self.assertEqual(box.access_token, "at-1")

    def test_a_rotated_refresh_token_is_written_back(self):
        """Microsoft rotates the refresh token on every grant.  Not writing it
        back means the *next* refresh uses a dead token -- the account dies one
        cycle later, which is a miserable failure to debug."""
        box = _Mailbox(refresh_token="rt-1")
        self.run_refresh(_Response(payload={"access_token": "at",
                                            "refresh_token": "rt-2"}),
                         mailbox=box)
        self.assertEqual(box.refresh_token, "rt-2")

    def test_the_old_refresh_token_survives_when_none_is_returned(self):
        box = _Mailbox(refresh_token="rt-1")
        self.run_refresh(_Response(payload={"access_token": "at"}), mailbox=box)
        self.assertEqual(box.refresh_token, "rt-1")


class FailurePathTests(_RefreshCase):
    def test_an_empty_access_token_is_a_failure(self):
        outcome, _, _, _ = self.run_refresh(_Response(payload={"access_token": ""}))
        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn("empty access token", str(outcome))

    def test_a_missing_access_token_is_a_failure(self):
        outcome, _, _, _ = self.run_refresh(_Response(payload={}))
        self.assertIsInstance(outcome, RuntimeError)

    def test_an_empty_access_token_does_not_raise_the_expired_error(self):
        """A 200 with no token is a transient weirdness, not a dead mailbox --
        it must NOT be classified as expired, or the account is dropped for no
        reason."""
        outcome, _, _, _ = self.run_refresh(_Response(payload={"access_token": ""}))
        self.assertNotIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)


class ExpiredTokenTests(_RefreshCase):
    """The classification that decides whether an account is abandoned."""

    def _classify(self, payload=None, text="", status_code=400):
        outcome, _, _, _ = self.run_refresh(
            _Response(status_code=status_code, payload=payload, text=text))
        return outcome

    def test_invalid_grant_in_the_error_field_means_expired(self):
        outcome = self._classify({"error": "invalid_grant"})
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_the_error_code_9002313_means_expired(self):
        outcome = self._classify({"error_codes": [9002313], "error": "other"})
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_any_other_error_is_a_plain_runtime_error(self):
        outcome = self._classify({"error": "invalid_request", "error_codes": [70000]})
        self.assertIsInstance(outcome, RuntimeError)
        self.assertNotIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_the_marker_is_matched_case_insensitively(self):
        outcome = self._classify({"error": "INVALID_GRANT"})
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_the_marker_is_looked_for_anywhere_in_the_body(self):
        """``"invalid_grant" in str(body).lower()`` -- the whole serialised body
        is searched, not just the ``error`` field."""
        outcome = self._classify({"error_description": "AADSTS700082: invalid_grant"})
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_an_empty_error_codes_list_falls_back_to_the_error_field(self):
        """``(body.get("error_codes") or [body.get("error") or ""])[0]`` --
        an empty list is falsy, so the ``error`` field is used instead."""
        outcome = self._classify({"error_codes": [], "error": "9002313"})
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_the_dict_guard_only_protects_the_error_code_path(self):
        """⚠️ Pinned: ``isinstance(body, dict)`` 只包住 ``error_code`` 那一行
        （源码 :28），**包不住** :29 的自由文本扫描。所以一个 list/dict 形态的
        非 200 body 只要字符串里出现 ``invalid_grant``，照样判为已过期。
        我一开始写成了"非 dict body 一定是普通 RuntimeError"，跑出来才发现反了。"""
        outcome = self._classify(["invalid_grant"])
        self.assertIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_an_error_code_outside_the_error_codes_field_is_ignored(self):
        """同一条守卫的另一半：``9002313`` 出现在别处（不是 ``error_codes``）
        时，非 dict body 提取不到 error_code → 判为普通失败。"""
        outcome = self._classify(["9002313"])
        self.assertIsInstance(outcome, RuntimeError)
        self.assertNotIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)

    def test_the_expired_error_names_the_mailbox(self):
        box = _Mailbox(email="dead@example.test")
        outcome, _, _, _ = self.run_refresh(
            _Response(status_code=400, payload={"error": "invalid_grant"}),
            mailbox=box)
        self.assertIn("dead@example.test", str(outcome))

    def test_a_200_response_is_never_classified_as_expired(self):
        """Only the non-200 branch inspects error codes at all."""
        outcome = self._classify({"error": "invalid_grant", "access_token": "at"},
                                 status_code=200)
        self.assertEqual(outcome, "at")


class UnparseableBodyTests(_RefreshCase):
    def test_a_non_json_body_is_kept_as_raw_text(self):
        outcome, _, _, _ = self.run_refresh(
            _Response(status_code=502, text="<html>gateway</html>"))
        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn("gateway", str(outcome))

    def test_the_raw_body_is_truncated_at_500_chars(self):
        """⚠️ Pinned: ``r.text[:500]``.  Anything the marker appears in *after*
        character 500 is invisible to the expired-token classifier."""
        long_body = "x" * 700 + "invalid_grant"
        outcome, _, _, _ = self.run_refresh(
            _Response(status_code=502, text=long_body))
        self.assertIsInstance(outcome, RuntimeError)
        self.assertNotIsInstance(outcome, mailbox_graph.MailboxTokenExpiredError)
        self.assertIn("x" * 500, str(outcome))
        self.assertNotIn("x" * 501, str(outcome))

    def test_a_json_parse_error_is_swallowed(self):
        outcome, _, _, _ = self.run_refresh(
            _Response(status_code=500, raises=ValueError("not json"),
                      text="boom"))
        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn("boom", str(outcome))


if __name__ == "__main__":
    unittest.main()

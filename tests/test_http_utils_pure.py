"""Behaviour tests for ``sms_tool/http_utils.py`` (2026-09-03, round 7).

``http_utils.py`` (153 lines) had **zero direct callers in the suite** (AST
audit) despite being imported by six modules: ``auth_flow``,
``account_creation``, ``account_2fa``, ``codex_oauth``, ``session_refresh``
and ``auth_state``. That combination -- widely depended upon, never exercised
-- is where a regression does the most damage for the least warning.

Three of the six helpers are pure; the other three touch the network only
through ``request_with_retry``, which is faked here. No real HTTP, no real
account, no real OTP.

Patch seams (all module-level bindings inside ``http_utils`` itself, so
patching there is what the production code actually reads -- the same trap
that caused the ``payment_link_manager`` flake):

* ``sms_tool.http_utils.request_with_retry``
* ``sms_tool.http_utils.auth_impersonate``
* ``sms_tool.http_utils.CFG``

⚠️ Pinned, not fixed:

* ``_minimal_chatgpt_cookie_header`` does **not** deduplicate -- a repeated
  cookie name is emitted twice.
* ``_json_or_raw`` returns ``None`` (not ``{"_raw": ...}``) when the body is
  valid JSON ``null``. Callers that assume a dict will break on it.
* ``_validate_email_otp`` only falls through on **404/405**. Any other
  non-200 (401, 429, 500) stops the chain immediately and is returned as the
  failure -- the alternatives are never tried.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sms_tool import http_utils


# --------------------------------------------------------------------------- fakes


class _FakeResponse:
    """Minimal stand-in for a curl_cffi / requests response."""

    def __init__(self, status_code=200, payload=None, text="",
                 url="https://auth.test/echo", json_error=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _RequestRecorder:
    """Replacement for ``request_with_retry`` that records every call.

    Queued responses are consumed in order; queueing an exception instance
    makes the call raise it, so failure paths are expressible.
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, session, method, url, *, label="",
                 attempts=None, retry_delay=None, **kwargs):
        self.calls.append({
            "session": session,
            "method": method,
            "url": url,
            "label": label,
            **kwargs,
        })
        item = self._responses.pop(0) if self._responses else _FakeResponse(200, {})
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


class _DictCookieJar:
    def __init__(self, cookies):
        self._cookies = dict(cookies)

    def get_dict(self):
        return dict(self._cookies)


class _NamedCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


CSRF = "__Host-next-auth.csrf-token"
CALLBACK = "__Secure-next-auth.callback-url"
SESSION = "__Secure-next-auth.session-token"


# --------------------------------------------------------------------------- _json_or_raw


class JsonOrRawTests(unittest.TestCase):
    def test_parses_valid_json(self):
        self.assertEqual(_FakeResponse(200, {"a": 1}).json(), {"a": 1})
        self.assertEqual(http_utils._json_or_raw(_FakeResponse(200, {"a": 1})), {"a": 1})

    def test_falls_back_to_truncated_raw_text(self):
        response = _FakeResponse(200, text="x" * 1000,
                                 json_error=ValueError("not json"))
        self.assertEqual(http_utils._json_or_raw(response, limit=500),
                         {"_raw": "x" * 500})

    def test_limit_is_configurable(self):
        response = _FakeResponse(200, text="abcdef", json_error=ValueError("no"))
        self.assertEqual(http_utils._json_or_raw(response, limit=3), {"_raw": "abc"})

    def test_short_text_is_not_padded(self):
        response = _FakeResponse(200, text="ab", json_error=ValueError("no"))
        self.assertEqual(http_utils._json_or_raw(response, limit=500), {"_raw": "ab"})

    def test_missing_text_attribute_yields_empty_raw(self):
        class _NoText:
            def json(self):
                raise ValueError("no")

        self.assertEqual(http_utils._json_or_raw(_NoText()), {"_raw": ""})

    def test_json_null_returns_none_not_a_raw_dict(self):
        """⚠️ Pinned: body "null" parses fine, so the result is None -- not a dict."""
        self.assertIsNone(http_utils._json_or_raw(_FakeResponse(200, None)))

    def test_non_dict_json_is_passed_through(self):
        self.assertEqual(http_utils._json_or_raw(_FakeResponse(200, [1, 2])), [1, 2])


# --------------------------------------------------------------------------- _absolute_url


class AbsoluteUrlTests(unittest.TestCase):
    def test_empty_and_falsy_urls_stay_empty(self):
        for url in ("", None):
            with self.subTest(url=url):
                self.assertEqual(http_utils._absolute_url("https://b.test", url), "")

    def test_absolute_urls_are_returned_unchanged(self):
        for url in ("http://x.test/a", "https://x.test/a?b=1"):
            with self.subTest(url=url):
                self.assertEqual(http_utils._absolute_url("https://b.test", url), url)

    def test_relative_path_is_joined_with_one_slash(self):
        self.assertEqual(
            http_utils._absolute_url("https://b.test/api", "next"),
            "https://b.test/api/next",
        )

    def test_slashes_are_normalised_on_both_sides(self):
        self.assertEqual(
            http_utils._absolute_url("https://b.test/api/", "/next/"),
            "https://b.test/api/next/",
        )

    def test_empty_base_still_produces_a_relative_path(self):
        self.assertEqual(http_utils._absolute_url("", "next"), "/next")

    def test_query_string_survives(self):
        self.assertEqual(
            http_utils._absolute_url("https://b.test", "next?a=1&b=2"),
            "https://b.test/next?a=1&b=2",
        )

    def test_protocol_relative_url_is_treated_as_relative(self):
        """⚠️ '//host/path' does not start with http(s)://, so it gets joined."""
        self.assertEqual(
            http_utils._absolute_url("https://b.test", "//other.test/p"),
            "https://b.test/other.test/p",
        )


# --------------------------------------------------------------------------- cookie helpers


class MinimalCookieHeaderTests(unittest.TestCase):
    def test_keeps_only_the_three_essential_cookies(self):
        raw = f"tracker=1; {CSRF}=abc; {CALLBACK}=def; {SESSION}=ghi; other=z"
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(raw),
                         f"{CSRF}=abc; {CALLBACK}=def; {SESSION}=ghi")

    def test_empty_value_cookies_are_dropped(self):
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(f"{CSRF}="), "")

    def test_fragments_without_equals_are_skipped(self):
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(f"garbage; {CSRF}=x"),
                         f"{CSRF}=x")

    def test_equals_inside_the_value_is_preserved(self):
        raw = f"{CSRF}=ab==cd"
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(raw), f"{CSRF}=ab==cd")

    def test_whitespace_around_names_and_values_is_trimmed(self):
        raw = f"  {CSRF} =  abc  "
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(raw), f"{CSRF}=abc")

    def test_none_and_empty_input_give_an_empty_header(self):
        for raw in (None, ""):
            with self.subTest(raw=raw):
                self.assertEqual(http_utils._minimal_chatgpt_cookie_header(raw), "")

    def test_cookie_names_are_case_sensitive(self):
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(f"{CSRF.upper()}=x"), "")

    def test_duplicate_cookies_are_not_deduplicated(self):
        """⚠️ Pinned: the same name appearing twice is emitted twice, last not first."""
        raw = f"{CSRF}=first; {CSRF}=second"
        self.assertEqual(http_utils._minimal_chatgpt_cookie_header(raw),
                         f"{CSRF}=first; {CSRF}=second")


class CookieHeaderTests(unittest.TestCase):
    def test_missing_or_empty_jar_gives_an_empty_header(self):
        for jar in (None, {}, []):
            with self.subTest(jar=jar):
                self.assertEqual(http_utils._cookie_header(_FakeSession(jar)), "")

    def test_uses_get_dict_when_available(self):
        jar = _DictCookieJar({CSRF: "abc", "tracker": "z"})
        self.assertEqual(http_utils._cookie_header(_FakeSession(jar)), f"{CSRF}=abc")

    def test_falls_back_to_named_cookies(self):
        jar = [_NamedCookie(CSRF, "abc"), _NamedCookie("tracker", "z")]
        self.assertEqual(http_utils._cookie_header(_FakeSession(jar)), f"{CSRF}=abc")

    def test_all_three_essentials_survive_the_round_trip(self):
        jar = _DictCookieJar({CSRF: "a", CALLBACK: "b", SESSION: "c"})
        self.assertEqual(http_utils._cookie_header(_FakeSession(jar)),
                         f"{CSRF}=a; {CALLBACK}=b; {SESSION}=c")

    def test_session_without_a_cookies_attribute_gives_an_empty_header(self):
        self.assertEqual(http_utils._cookie_header(object()), "")


class _FakeSession:
    def __init__(self, cookies):
        self.cookies = cookies


# --------------------------------------------------------------------------- _follow_continue_url


class FollowContinueUrlTests(unittest.TestCase):
    def _run(self, url, *, referer="", label="continue"):
        recorder = _RequestRecorder(_FakeResponse(200, {}, url="https://auth.test/landed"))
        with patch("sms_tool.http_utils.CFG",
                   {"chatgpt": {"auth_base_url": "https://auth.test"}}), \
             patch("sms_tool.http_utils.auth_impersonate", lambda: "IMPERSONATE"), \
             patch("sms_tool.http_utils.request_with_retry", recorder):
            response = http_utils._follow_continue_url(
                _FakeSession({}), url, {"X-Base": "1"}, referer=referer, label=label,
            )
        return response, recorder

    def test_empty_url_short_circuits_without_a_request(self):
        response, recorder = self._run("")
        self.assertIsNone(response)
        self.assertEqual(recorder.calls, [], "a blank continue URL must not be fetched")

    def test_relative_url_is_resolved_against_the_configured_auth_base(self):
        _response, recorder = self._run("/api/go")
        self.assertEqual(recorder.urls, ["https://auth.test/api/go"])

    def test_absolute_url_is_used_as_is(self):
        _response, recorder = self._run("https://other.test/go")
        self.assertEqual(recorder.urls, ["https://other.test/go"])

    def test_accept_header_is_always_added(self):
        _response, recorder = self._run("/go")
        self.assertEqual(recorder.calls[0]["headers"]["Accept"],
                         "text/html,application/xhtml+xml")

    def test_base_headers_are_preserved(self):
        _response, recorder = self._run("/go")
        self.assertEqual(recorder.calls[0]["headers"]["X-Base"], "1")

    def test_referer_is_only_set_when_supplied(self):
        _response, without = self._run("/go")
        self.assertNotIn("Referer", without.calls[0]["headers"])
        _response, with_ref = self._run("/go", referer="https://auth.test/from")
        self.assertEqual(with_ref.calls[0]["headers"]["Referer"], "https://auth.test/from")

    def test_label_and_impersonation_are_forwarded(self):
        _response, recorder = self._run("/go", label="email-otp")
        self.assertEqual(recorder.calls[0]["label"], "email-otp")
        self.assertEqual(recorder.calls[0]["impersonate"], "IMPERSONATE")

    def test_uses_the_default_auth_base_when_config_has_none(self):
        recorder = _RequestRecorder(_FakeResponse(200, {}))
        with patch("sms_tool.http_utils.CFG", {"chatgpt": {}}), \
             patch("sms_tool.http_utils.auth_impersonate", lambda: "IMPERSONATE"), \
             patch("sms_tool.http_utils.request_with_retry", recorder):
            http_utils._follow_continue_url(_FakeSession({}), "/go", {})
        self.assertEqual(recorder.urls, ["https://auth.openai.com/go"])

    def test_returns_the_response_object(self):
        response, _recorder = self._run("/go")
        self.assertEqual(response.url, "https://auth.test/landed")


# --------------------------------------------------------------------------- _validate_email_otp


PRIMARY = "https://auth.test/api/accounts/email-otp/validate"
FALLBACK_1 = "https://auth.test/api/accounts/email-verification/validate"
FALLBACK_2 = "https://auth.test/api/accounts/email-verification/verify"
FALLBACK_3 = "https://auth.test/api/accounts/verify-email"
ALL_ENDPOINTS = [PRIMARY, FALLBACK_1, FALLBACK_2, FALLBACK_3]


class ValidateEmailOtpTests(unittest.TestCase):
    def _run(self, *responses, code="123456", **kwargs):
        recorder = _RequestRecorder(*responses)
        with patch("sms_tool.http_utils.auth_impersonate", lambda: "IMPERSONATE"), \
             patch("sms_tool.http_utils.request_with_retry", recorder):
            ok, body = http_utils._validate_email_otp(
                _FakeSession({}), "https://auth.test", {"X-Base": "1"}, code, **kwargs
            )
        return ok, body, recorder

    def test_success_on_the_primary_endpoint_hits_only_one_url(self):
        ok, body, recorder = self._run(_FakeResponse(200, {"ok": True}))
        self.assertTrue(ok)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(recorder.urls, [PRIMARY])

    def test_404_falls_through_to_the_next_endpoint(self):
        ok, _body, recorder = self._run(
            _FakeResponse(404, {}), _FakeResponse(200, {"ok": True}),
        )
        self.assertTrue(ok)
        self.assertEqual(recorder.urls, [PRIMARY, FALLBACK_1])

    def test_405_also_falls_through(self):
        ok, _body, recorder = self._run(
            _FakeResponse(405, {}), _FakeResponse(200, {"ok": True}),
        )
        self.assertTrue(ok)
        self.assertEqual(recorder.urls, [PRIMARY, FALLBACK_1])

    def test_every_endpoint_is_tried_when_all_return_404(self):
        """The chain is 1 primary + 3 fallbacks = 4 endpoints, not 5."""
        ok, body, recorder = self._run(*[_FakeResponse(404, {}) for _ in range(4)])
        self.assertFalse(ok)
        self.assertEqual(recorder.urls, ALL_ENDPOINTS)
        self.assertEqual(body["endpoint"], "/api/accounts/verify-email")
        self.assertEqual(body["status"], 404)

    def test_non_404_failure_stops_the_chain_immediately(self):
        """⚠️ Pinned: 401/429/500 do NOT fall through -- only 404 and 405 do."""
        for status in (401, 429, 500):
            with self.subTest(status=status):
                ok, body, recorder = self._run(
                    _FakeResponse(status, {"error": "nope"}),
                    _FakeResponse(200, {"ok": True}),
                )
                self.assertFalse(ok)
                self.assertEqual(recorder.urls, [PRIMARY],
                                 "a hard failure must not keep probing alternatives")
                self.assertEqual(body["status"], status)

    def test_code_is_posted_as_json(self):
        _ok, _body, recorder = self._run(_FakeResponse(200, {}), code="654321")
        self.assertEqual(recorder.calls[0]["json"], {"code": "654321"})
        self.assertEqual(recorder.calls[0]["method"], "post")

    def test_validation_headers_are_set(self):
        _ok, _body, recorder = self._run(_FakeResponse(200, {}))
        headers = recorder.calls[0]["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Origin"], "https://auth.test")
        self.assertEqual(headers["Referer"], "https://auth.test/email-verification")
        self.assertEqual(headers["X-Base"], "1", "base headers must survive")

    def test_impersonation_is_forwarded(self):
        _ok, _body, recorder = self._run(_FakeResponse(200, {}))
        self.assertEqual(recorder.calls[0]["impersonate"], "IMPERSONATE")

    def test_unparseable_body_is_captured_as_raw_text(self):
        ok, body, _recorder = self._run(
            _FakeResponse(401, text="nope", json_error=ValueError("no")),
        )
        self.assertFalse(ok)
        self.assertEqual(body["body"], {"_raw": "nope"})

    def test_label_names_the_endpoint_being_tried(self):
        _ok, _body, recorder = self._run(_FakeResponse(200, {}))
        self.assertEqual(recorder.calls[0]["label"],
                         "Email OTP validate /api/accounts/email-otp/validate")


if __name__ == "__main__":
    unittest.main()

"""Contract tests for :mod:`sms_tool.proxy_edge_probe`.

The probe exists because the SOCKS5 pool's tunnel-only health check cannot see a
Cloudflare 403.  These tests pin the two properties an operator tool depends on:
the classifier is **total** (every status maps to exactly one verdict), and the
probe **never raises** and never leaks credentials.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sms_tool.proxy_edge_probe import (
    BLOCKED,
    CLEAN,
    DEAD,
    DEGRADED,
    STATUSES,
    EdgeVerdict,
    classify_edge_response,
    edge_health,
    probe_openai_edge,
)


@pytest.mark.parametrize(
    "status, body, headers, expected",
    [
        (0, "", {}, DEAD),
        (200, "<html>login</html>", {}, CLEAN),
        (302, "", {}, CLEAN),
        (401, '{"error":"no token"}', {}, CLEAN),
        (429, "", {}, CLEAN),
        (403, "", {}, BLOCKED),
        (403, "<html>Just a moment...</html>", {}, BLOCKED),
        (403, "<html>Attention Required! | Cloudflare</html>", {}, BLOCKED),
        (403, "", {"cf-mitigated": "challenge"}, BLOCKED),
        (503, "upstream unavailable", {}, DEGRADED),
        (418, "", {}, DEGRADED),
        (400, "Just a moment...", {}, BLOCKED),
    ],
)
def test_classifier_is_total_and_matches_the_verdict_vocabulary(status, body, headers, expected):
    result = classify_edge_response(status, body, headers)
    assert result == expected
    assert result in STATUSES


def test_classifier_tolerates_non_numeric_status():
    """``response.status_code`` may be odd in a mock; classification must not crash."""
    assert classify_edge_response("not-a-number", "", {}) == DEAD  # type: ignore[arg-type]


class _FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})


class _FakeSession:
    """Minimal stand-in for ``curl_cffi.requests.Session``."""

    def __init__(self, response=None, error=None, **kwargs):
        self._response = response
        self._error = error
        self.proxies = {}
        self.trust_env = True
        self.closed = False
        self.last_headers = None

    def get(self, url, **kwargs):
        self.last_headers = kwargs.get("headers")
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        self.closed = True


def _probe_with(response=None, error=None, **kwargs):
    session = _FakeSession(response=response, error=error)

    def _factory(*_args, **_kwargs):
        return session

    with patch("curl_cffi.requests.Session", _factory):
        verdict = probe_openai_edge("http://user:Hunter2@proxy.example:8080", **kwargs)
    return verdict, session


def test_transport_failure_is_dead_and_never_raises():
    verdict, _ = _probe_with(error=ConnectionError("boom"))
    assert verdict.status == DEAD
    assert verdict.error == "ConnectionError"
    assert verdict.http_status == 0


def test_cloudflare_challenge_classifies_as_blocked():
    verdict, _ = _probe_with(response=_FakeResponse(403, "<html>Just a moment...</html>"))
    assert verdict.status == BLOCKED
    assert verdict.blocked_by_cloudflare is True
    assert verdict.http_status == 403


def test_clean_response_classifies_as_clean():
    verdict, _ = _probe_with(response=_FakeResponse(200, "<html>ok</html>"))
    assert verdict.status == CLEAN
    assert verdict.ok is True


def test_verdict_never_leaks_credentials():
    verdict, _ = _probe_with(response=_FakeResponse(200, "ok"))
    assert "Hunter2" not in verdict.proxy
    assert verdict.proxy == "http://***:***@proxy.example:8080"
    assert "Hunter2" not in str(verdict.to_dict())


def test_session_is_closed_and_proxy_is_applied():
    _, session = _probe_with(response=_FakeResponse(200, "ok"))
    assert session.closed is True
    assert session.trust_env is False
    assert session.proxies == {
        "http": "http://user:Hunter2@proxy.example:8080",
        "https": "http://user:Hunter2@proxy.example:8080",
    }


def test_direct_probe_is_labelled_and_makes_no_proxy_claims():
    session = _FakeSession(response=_FakeResponse(200, "ok"))

    def _factory(*_args, **_kwargs):
        return session

    with patch("curl_cffi.requests.Session", _factory):
        verdict = probe_openai_edge("")
    assert verdict.status == CLEAN
    assert verdict.proxy == "DIRECT"
    assert session.proxies == {}


# ---------------------------------------------------------------------------
# edge_health: the pool-facing adapter
# ---------------------------------------------------------------------------


def _edge_health_with(verdict):
    with patch("sms_tool.proxy_edge_probe.probe_openai_edge", return_value=verdict):
        return edge_health("http://u:p@h:1")


def test_edge_health_clean_is_healthy_with_no_detail():
    assert _edge_health_with(EdgeVerdict(proxy="h", status=CLEAN, http_status=200)) == (True, "")


def test_edge_health_blocked_reports_cloudflare_and_status():
    verdict = EdgeVerdict(proxy="h", status=BLOCKED, http_status=403, blocked_by_cloudflare=True)
    assert _edge_health_with(verdict) == (False, "edge:blocked:http_403:cloudflare")


def test_edge_health_dead_has_no_http_status():
    assert _edge_health_with(EdgeVerdict(proxy="h", status=DEAD, error="Timeout")) == (False, "edge:dead")


def test_edge_health_degraded_reports_status():
    assert _edge_health_with(EdgeVerdict(proxy="h", status=DEGRADED, http_status=503)) == (
        False,
        "edge:degraded:http_503",
    )

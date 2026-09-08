from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli, registration, registration_preflight, sentinel_tokens
from sms_tool.config import ConfigError


ROOT = Path(__file__).resolve().parents[1]


def test_cli_registration_proxy_prefers_registration_and_never_payment_proxy():
    config = {
        "proxy": {
            "registration": "http://registration.example:8080",
            "default": "http://default.example:8080",
            "pool": ["http://pool.example:8080"],
        },
        "paypal": {"proxies": ["http://payment.example:8080"]},
    }
    args = SimpleNamespace(
        proxy="http://default.example:8080",
        proxy_explicit=False,
        proxy_pool="",
    )

    with patch.object(cli, "CFG", config):
        cli._apply_registration_proxy_defaults(args)
        pool = cli._proxy_pool_values(args)

    assert args.proxy == "http://registration.example:8080"
    assert pool == ["http://registration.example:8080", "http://pool.example:8080"]
    assert "http://payment.example:8080" not in pool


def test_cli_main_uses_registration_proxy_for_direct_registration():
    captured = []
    config = {
        "proxy": {
            "registration": "http://registration.example:8080",
            "default": "http://default.example:8080",
            "pool": [],
        },
        "storage": {},
        "email_registration": {},
    }

    def run_email(**kwargs):
        captured.append(kwargs["proxy"])
        return {"success": True, "email": "account@example.com"}

    with patch.object(cli, "CFG", config), \
         patch.object(sys, "argv", ["sms_tool", "--email", "account@example.com", "--registration-at-only"]), \
         patch.object(cli, "_load_mailbox_pool", return_value=[SimpleNamespace(email="account@example.com")]), \
         patch.object(cli, "_preflight_registration_before_mailbox", return_value={"ok": True}), \
         patch.object(cli, "_registration_phone_pool", return_value=None), \
         patch.object(cli, "run_email", side_effect=run_email), \
         patch.object(cli, "_save_registration_results", return_value={}):
        cli.main()

    assert captured == ["http://registration.example:8080"]


def test_cli_registration_emits_standard_desktop_result():
    report = {
        "ok": True,
        "total": 1,
        "success": 1,
        "failed": 0,
        "batch_id": "batch",
    }
    config = {"proxy": {}, "storage": {}, "email_registration": {}}
    with patch.object(cli, "CFG", config), \
         patch.object(sys, "argv", ["sms_tool", "--email", "account@example.com", "--registration-at-only", "--desktop-ipc"]), \
         patch.object(cli, "_load_mailbox_pool", return_value=[SimpleNamespace(email="account@example.com")]), \
         patch.object(cli, "_preflight_registration_before_mailbox", return_value={"ok": True}), \
         patch.object(cli, "_registration_phone_pool", return_value=None), \
         patch.object(cli, "run_batch", return_value=[{"success": True, "email": "account@example.com"}]), \
         patch.object(cli, "_save_registration_results", return_value=report), \
         patch.object(cli, "emit_result") as emit:
        cli.main()

    emit.assert_called_once_with(report, enabled=True)


def test_cli_registration_preflight_failure_emits_standard_desktop_result():
    config = {"proxy": {}, "storage": {}, "email_registration": {}}
    with patch.object(cli, "CFG", config), \
         patch.object(sys, "argv", ["sms_tool", "--email", "account@example.com", "--desktop-ipc"]), \
         patch.object(cli, "_preflight_registration_before_mailbox", side_effect=RuntimeError("route_failed")), \
         patch.object(cli, "emit_result") as emit:
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("preflight failure did not exit")

    payload = emit.call_args.args[0]
    assert payload["ok"] is False
    assert payload["success"] == 0
    assert payload["failed"] == 1
    assert payload["error"] == "route_failed"


def test_cli_preflights_before_claiming_mailbox_and_promotes_healthy_proxy():
    args = SimpleNamespace(
        proxy="http://first.example:8080",
        proxy_explicit=False,
        proxy_pool="",
    )
    calls = []

    def preflight(proxy, **_kwargs):
        calls.append(proxy)
        if "first" in proxy:
            raise RuntimeError("tls_failed")
        return {"ok": True, "proxy": proxy}

    with patch.object(
        cli,
        "_proxy_pool_values",
        return_value=["http://first.example:8080", "http://second.example:8080"],
    ), patch("sms_tool.registration.registration_network_preflight", side_effect=preflight):
        result = cli._preflight_registration_before_mailbox(args)

    assert result["ok"] is True
    assert calls == ["http://first.example:8080", "http://second.example:8080"]
    assert args.proxy == "http://second.example:8080"
    assert args.proxy_pool.splitlines()[0] == "http://second.example:8080"


def test_cli_rejects_missing_browser_driver_credentials_before_network_preflight():
    args = SimpleNamespace(
        registration_driver="roxy",
        proxy="http://registration.example:8080",
        proxy_explicit=False,
        proxy_pool="",
    )
    config = {
        "registration": {"driver": "roxy", "drivers": {"roxy": {}}},
        "proxy": {"registration": "http://registration.example:8080"},
    }
    with patch.object(cli, "CFG", config), patch(
        "sms_tool.registration.registration_network_preflight"
    ) as network_preflight:
        try:
            cli._preflight_registration_before_mailbox(args)
        except ConfigError as exc:
            assert str(exc) == "roxy_workspace_id_missing"
        else:
            raise AssertionError("missing Roxy credentials should fail preflight")
    network_preflight.assert_not_called()


def test_registration_normalizes_provider_proxy_before_sentinel_extraction():
    proxy = "http://sg.cliproxy.io:443:user-region-JP:pass"

    assert registration._resolve_proxy_scheme(proxy) == "http://user-region-JP:pass@sg.cliproxy.io:443"

    with patch.object(sentinel_tokens, "_get_cached_sentinel", return_value=None), \
         patch.object(
             sentinel_tokens,
             "_extract_sentinel_uncached",
             return_value={"sentinel_token": "token"},
         ) as extract:
        sentinel_tokens._extract_sentinel(proxy)

    assert extract.call_args.args[0] == "http://user-region-JP:pass@sg.cliproxy.io:443"
    assert sentinel_tokens._redact_proxy_url(proxy) == "http://***:***@sg.cliproxy.io:443"


def test_registration_preflight_checks_chatgpt_auth_and_sentinel_before_mailbox():
    urls = []

    class Session:
        def __init__(self):
            self.proxies = {}

        def get(self, url, **_kwargs):
            urls.append(url)
            return SimpleNamespace(status_code=200)

    with patch.object(registration_preflight.curl_requests, "Session", Session), \
         patch.object(registration_preflight, "auth_fingerprint_capabilities", return_value={
             "configured": ["chrome146"], "available": ["chrome146"], "missing": [],
         }), \
         patch.object(registration_preflight, "_sentinel_frame_version", return_value="sv-test"), \
         patch.object(registration_preflight, "auth_impersonate", return_value="chrome146"), \
         patch.object(registration_preflight, "current_auth_fingerprint", return_value={"impersonate": "chrome146"}):
        result = registration.registration_network_preflight("http://proxy.example:8080")

    assert result == {"ok": True, "profile": "chrome146"}
    assert urls == [
        "https://chatgpt.com/login",
        "https://auth.openai.com/log-in",
        "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=sv-test",
        "https://chatgpt.com/backend-api/wham/usage",
    ]


def test_registration_preflight_accepts_unauthorized_backend_probe_response():
    class Session:
        def __init__(self):
            self.proxies = {}

        def get(self, url, **_kwargs):
            status = 401 if url.endswith("/backend-api/wham/usage") else 200
            return SimpleNamespace(status_code=status)

    with patch.object(registration_preflight.curl_requests, "Session", Session), \
         patch.object(registration_preflight, "auth_fingerprint_capabilities", return_value={
             "configured": ["chrome146"], "available": ["chrome146"], "missing": [],
         }), \
         patch.object(registration_preflight, "_sentinel_frame_version", return_value="sv-test"), \
         patch.object(registration_preflight, "auth_impersonate", return_value="chrome146"), \
         patch.object(registration_preflight, "current_auth_fingerprint", return_value={"impersonate": "chrome146"}):
        result = registration.registration_network_preflight("http://proxy.example:8080")

    assert result["ok"] is True


def test_sentinel_proxy_errors_redact_standard_and_provider_proxy_forms():
    legacy = "http://sg.cliproxy.io:443:user-region-JP:pass"
    standard = "http://user-region-JP:pass@sg.cliproxy.io:443"

    legacy_error = sentinel_tokens._redact_proxy_text(
        f"Unsupported proxy syntax in '{legacy}'",
        legacy,
    )
    standard_error = sentinel_tokens._redact_proxy_text(
        f"Proxy connection failed: {standard}",
        standard,
    )

    assert legacy_error == "Unsupported proxy syntax in 'http://***:***@sg.cliproxy.io:443'"
    assert standard_error == "Proxy connection failed: http://***:***@sg.cliproxy.io:443"
    assert "user-region-JP" not in legacy_error + standard_error
    assert "pass" not in legacy_error + standard_error


def test_cli_registration_proxy_supports_legacy_key_then_default():
    args = SimpleNamespace(proxy=None, proxy_explicit=False, proxy_pool="")
    with patch.object(
        cli,
        "CFG",
        {"registration_proxy": "http://legacy.example:8080", "proxy": {"default": "http://default.example:8080"}},
    ):
        cli._apply_registration_proxy_defaults(args)
    assert args.proxy == "http://legacy.example:8080"

    with patch.object(cli, "CFG", {"proxy": {"default": "http://default.example:8080"}}):
        cli._apply_registration_proxy_defaults(args)
    assert args.proxy == "http://default.example:8080"


def test_cli_explicit_proxy_pool_does_not_inherit_configured_registration_proxy():
    args = SimpleNamespace(
        proxy="http://default.example:8080",
        proxy_explicit=False,
        proxy_pool="http://explicit-a.example:8080\nhttp://explicit-b.example:8080",
    )
    with patch.object(cli, "CFG", {"proxy": {"registration": "http://registration.example:8080"}}):
        cli._apply_registration_proxy_defaults(args)
        pool = cli._proxy_pool_values(args)

    assert args.proxy is None
    assert pool == ["http://explicit-a.example:8080", "http://explicit-b.example:8080"]


def test_desktop_registration_proxy_resolver_has_no_payment_fallback():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Helpers.cs").read_text(encoding="utf-8")
    method = source.split("private string GetRegistrationProxy()", 1)[1].split(
        "private List<string> GetRegistrationProxyPool()", 1
    )[0]

    registration = method.index('settingsService.GetString("proxy.registration")')
    legacy = method.index('settingsService.GetString("registration_proxy")')
    default = method.index('settingsService.GetString("proxy.default")')
    assert registration < legacy < default
    assert "paypal" not in method.lower()
    assert "LocalNonPaymentProxy" in method


def test_cli_registration_proxy_prefers_healthiest_via_tracker():
    """P2-1: with >1 candidate, the single-account path picks the healthiest
    proxy (per the shared ProxyHealthTracker), not always the first."""
    import tempfile
    from pathlib import Path

    from sms_tool.proxy_health import ProxyHealthTracker

    tracker = ProxyHealthTracker({}, path=Path(tempfile.mkdtemp()) / "h.json")
    bad, good = "http://bad:8080", "http://good:8080"
    for _ in range(3):
        tracker.record(bad, ok=False, error="x")

    config = {"proxy": {"pool": [bad, good]}}
    with patch.object(cli, "CFG", config):
        picked = cli._configured_registration_proxy(tracker=tracker)

    assert picked == good


def test_cli_registration_proxy_fresh_tracker_keeps_input_order():
    """P2-1: with no health data, rank() preserves input order, so the first
    configured proxy still wins (behaviour unchanged for a fresh run)."""
    import tempfile
    from pathlib import Path

    from sms_tool.proxy_health import ProxyHealthTracker

    tracker = ProxyHealthTracker({}, path=Path(tempfile.mkdtemp()) / "h.json")
    bad, good = "http://bad:8080", "http://good:8080"
    config = {"proxy": {"pool": [bad, good]}}
    with patch.object(cli, "CFG", config):
        picked = cli._configured_registration_proxy(tracker=tracker)

    assert picked == bad


def test_cli_registration_proxy_single_candidate_skips_rank():
    """P2-1: a single candidate returns directly without touching the tracker
    (no file I/O for the common single-proxy case)."""
    import tempfile
    from pathlib import Path

    from sms_tool.proxy_health import ProxyHealthTracker

    tracker = ProxyHealthTracker({}, path=Path(tempfile.mkdtemp()) / "h.json")
    only = "http://only:8080"
    config = {"proxy": {"pool": [only]}}
    with patch.object(cli, "CFG", config):
        picked = cli._configured_registration_proxy(tracker=tracker)

    assert picked == only


# ─────────────────────────── P2-4: rank() cold start ───────────────────────────
#
# rank() used to score success/(success+failure), which (a) let a proxy with a
# single lucky success outrank a proven one and (b) collapsed every never-used
# proxy onto 0.0 so a newly added endpoint could only win once an incumbent had
# already failed. It now uses an additively smoothed rate.


def _tracker():
    import tempfile

    from sms_tool.proxy_health import ProxyHealthTracker

    return ProxyHealthTracker({}, path=Path(tempfile.mkdtemp()) / "h.json")


def _record(tracker, proxy, *, ok_count=0, fail_count=0):
    for _ in range(ok_count):
        tracker.record(proxy, ok=True)
    for _ in range(fail_count):
        tracker.record(proxy, ok=False, error="boom")


def test_rank_cold_start_keeps_config_order():
    probes = ["http://a:8080", "http://b:8080", "http://c:8080"]
    assert _tracker().rank(probes) == probes


def test_rank_puts_proven_ahead_of_unproven():
    tracker = _tracker()
    proven, fresh = "http://proven:8080", "http://fresh:8080"
    _record(tracker, proven, ok_count=10)
    assert tracker.rank([fresh, proven])[0] == proven


def test_rank_puts_unproven_ahead_of_a_failed_one():
    # The audit's actual complaint: a newly added endpoint must get its turn
    # before one that has already failed (2 failures -- below the cooldown
    # threshold, so this isolates the rate, not the cooldown tier).
    tracker = _tracker()
    fresh, failed = "http://fresh:8080", "http://failed:8080"
    _record(tracker, failed, fail_count=2)
    assert tracker.rank([failed, fresh])[0] == fresh


def test_rank_one_lucky_success_does_not_outrank_a_proven_endpoint():
    # Regression guard. Raw ratio: 1/0 -> 1.0 beats 8/2 -> 0.8, so a single
    # fluke pinned traffic to an untested endpoint. Smoothed: 8/2 -> 0.75
    # beats 1/0 -> 0.67.
    tracker = _tracker()
    lucky, proven = "http://lucky:8080", "http://proven:8080"
    _record(tracker, lucky, ok_count=1)
    _record(tracker, proven, ok_count=8, fail_count=2)
    assert tracker.rank([lucky, proven])[0] == proven


def test_rank_cooling_proxy_goes_last_even_though_it_has_successes():
    tracker = _tracker()
    cooling, clean = "http://cooling:8080", "http://clean:8080"
    _record(tracker, cooling, ok_count=5, fail_count=3)  # 3 failures arms the cooldown
    _record(tracker, clean, ok_count=1)
    assert tracker.rank([cooling, clean])[0] == clean


def test_rank_dedupes_repeated_candidates():
    assert _tracker().rank(["http://a:8080", "http://a:8080"]) == ["http://a:8080"]


def test_rank_falls_back_to_the_endpoint_row_for_an_unknown_session():
    # A brand-new sticky session on a known endpoint inherits that endpoint's
    # record instead of being treated as a clean slate. u1/u2 are different
    # usernames, so their per-session keys differ and only the endpoint row
    # can carry the failure over. (A URL fragment would NOT exercise this --
    # urlsplit drops it, so the session keys would collide.)
    tracker = _tracker()
    _record(tracker, "http://u1:p@host.test:8080", fail_count=2)
    ranked = tracker.rank(["http://u2:p@host.test:8080", "http://clean.test:8080"])
    assert ranked[0] == "http://clean.test:8080"

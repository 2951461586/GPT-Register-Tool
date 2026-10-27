"""支付资格: the batch's environment layer (egress, preflight cache, budgets).

Split out of ``test_payment_qualification.py`` on 2026-10-27 so the probe-contract
suite stays focused. Everything here is about *why a run could not answer* rather
than about what the answer means:

* ``egress_denied`` — the exit refused the target host (measured: every proxylane
  port answered ``chatgpt.com`` and returned ``503 Target host denied`` for
  ``api.stripe.com``). It must not render as the generic 检测失败, because the
  operator's fix is a different exit, not a different account or method.
* the preflight cache — three retries in two minutes must not send 9×3 requests
  through a pool that already gave its answer.
* the per-account egress budget — ``--refresh-timeout`` is a *per-request*
  timeout; the account-level bound is that value × ``max_proxy_attempts``.
"""

from __future__ import annotations

import pytest

from sms_tool.payment import qualification as pq
from sms_tool.payment import qualification_batch as pqb
from sms_tool.payment.qualification_states import QUALIFICATION_ELIGIBLE

ELIGIBLE_RESULT = {
    "ok": True,
    "payment_method": "gcash",
    "eligible": True,
    "conclusive": True,
    "classification": "eligible",
    "decision": "payment_method_available",
    "status": "completed",
    "currency": "PHP",
    "amount": 0,
}


class _Clock:
    """Minimal ``time`` stand-in so the budget/cache tests own the clock."""

    def __init__(self, monotonic):
        self.monotonic = monotonic


# ── egress_denied: the exit refused the host, the account is not the problem ──
# Measured 2026-10-27: every proxylane exit answered chatgpt.com but returned
# ``Tunnel connection failed: 503 Target host denied`` for api.stripe.com. That
# was rendered as the generic 检测失败, i.e. indistinguishable in the grid from a
# dead token or a method that is simply unavailable.

ACL_ERROR = (
    "HTTPSConnectionPool(host='api.stripe.com', port=443): Max retries exceeded with url: "
    "/v1/payment_pages/cs_live_x/init (Caused by ProxyError('Unable to connect to proxy', "
    "OSError('Tunnel connection failed: 503 Target host denied')))"
)


def test_proxy_acl_denial_is_egress_denied_not_probe_failed():
    from sms_tool.payment.qualification_states import (
        QUALIFICATION_EGRESS_DENIED,
        qualification_label,
        qualification_state,
    )

    result = {"status": "failed", "error": ACL_ERROR, "error_code": "stripe_init_transport_failed"}

    assert qualification_state(result) == QUALIFICATION_EGRESS_DENIED
    assert qualification_label(result, "paypal") == "出口被拒"


def test_a_plain_transport_failure_stays_probe_failed():
    from sms_tool.payment.qualification_states import (
        QUALIFICATION_EGRESS_DENIED,
        QUALIFICATION_PROBE_FAILED,
        qualification_label,
        qualification_state,
    )

    result = {"status": "failed", "error": "ReadTimeout: HTTPSConnectionPool(host='chatgpt.com')"}

    assert qualification_state(result) == QUALIFICATION_PROBE_FAILED
    assert qualification_state(result) != QUALIFICATION_EGRESS_DENIED
    assert qualification_label(result, "paypal") == "检测失败"


def test_egress_denial_wins_over_generic_failure_in_the_aggregate():
    from sms_tool.payment import qualification as pq
    from sms_tool.payment.qualification_states import (
        QUALIFICATION_EGRESS_DENIED,
        QUALIFICATION_PROBE_FAILED,
    )

    rows = [
        {"state": QUALIFICATION_PROBE_FAILED, "error": "read timeout"},
        {"state": QUALIFICATION_EGRESS_DENIED, "error": ACL_ERROR},
    ]

    state = pq._aggregate_state(rows)
    assert state == QUALIFICATION_EGRESS_DENIED
    assert pq._aggregate_label(state, rows) == "出口被拒"


def test_conclusive_verdict_still_outranks_an_egress_denial():
    from sms_tool.payment import qualification as pq
    from sms_tool.payment.qualification_states import (
        QUALIFICATION_EGRESS_DENIED,
        QUALIFICATION_ELIGIBLE,
    )

    rows = [
        {"state": QUALIFICATION_ELIGIBLE, "eligible": True},
        {"state": QUALIFICATION_EGRESS_DENIED, "error": ACL_ERROR},
    ]

    assert pq._aggregate_state(rows) == QUALIFICATION_ELIGIBLE


def test_egress_denied_counts_as_an_environment_failure_for_the_breaker():
    from sms_tool.payment import qualification_batch as pqb
    from sms_tool.payment.qualification_states import QUALIFICATION_EGRESS_DENIED

    probe = {"payment_qualification_state": QUALIFICATION_EGRESS_DENIED, "error": ACL_ERROR}
    assert pqb._env_failure_class(probe) == "network"


# ── preflight cache: three retries in two minutes must not send 27 请求 ──────


@pytest.fixture(autouse=True)
def _clear_stripe_preflight_cache():
    from sms_tool.payment import qualification_batch as pqb

    pqb._stripe_preflight_cache_clear()
    yield
    pqb._stripe_preflight_cache_clear()


def test_preflight_verdict_is_cached_per_candidate_set(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    probed: list[str] = []

    def probe(proxy, timeout):
        probed.append(proxy)
        return (503, "Tunnel connection failed: 503 Target host denied")

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    first = pqb._stripe_preflight(["http://u:p@a:1", "http://u:p@b:2"], probe=probe, source="dedicated")
    # Same set, different order + different source label: only the set changes the key.
    second = pqb._stripe_preflight(["http://u:p@b:2", "http://u:p@a:1"], probe=probe, source="dedicated")

    assert len(probed) == 2, "each candidate is probed exactly once"
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["ok"] is False
    assert second["first_failure"] == first["first_failure"]

    # A different exit pool is a different key: it must be probed again.
    pqb._stripe_preflight(["http://u:p@c:3"], probe=probe, source="dedicated")
    assert len(probed) == 3


def test_preflight_cache_expires_after_the_ttl(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    clock = {"now": 1000.0}
    monkeypatch.setattr(pqb, "time", _Clock(lambda: clock["now"]))
    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    monkeypatch.setattr(pqb, "_stripe_preflight_cache_seconds", lambda: 300.0)
    probed: list[str] = []

    def probe(proxy, timeout):
        probed.append(proxy)
        return (404, "")

    pqb._stripe_preflight(["http://a:1"], probe=probe, source="fallback")
    clock["now"] += 299
    assert pqb._stripe_preflight(["http://a:1"], probe=probe, source="fallback")["cached"] is True
    clock["now"] += 2
    assert pqb._stripe_preflight(["http://a:1"], probe=probe, source="fallback")["cached"] is False
    assert len(probed) == 2


def test_preflight_cache_can_be_disabled(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    monkeypatch.setattr(pqb, "_stripe_preflight_cache_seconds", lambda: 0.0)
    probed: list[str] = []

    def probe(proxy, timeout):
        probed.append(proxy)
        return (404, "")

    pqb._stripe_preflight(["http://a:1"], probe=probe, source="fallback")
    second = pqb._stripe_preflight(["http://a:1"], probe=probe, source="fallback")

    assert second["cached"] is False
    assert len(probed) == 2


def test_preflight_cache_holds_no_credentials(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    pqb._stripe_preflight(
        ["http://user:secret@a:1"],
        probe=lambda proxy, timeout: (503, "Tunnel connection failed: 503 Target host denied"),
        source="dedicated",
    )

    blob = repr(pqb._STRIPE_PREFLIGHT_CACHE)
    assert "secret" not in blob
    assert "user:" not in blob
    # The evidence (host/status) is still there for the operator-facing detail.
    assert "a:1" in blob


# ── account budget: --refresh-timeout is per request, so bound the account ──


def test_account_budget_stops_rotation_with_its_own_error_code(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    clock = {"now": 0.0}
    attempts: list[tuple[str | None, int]] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        attempts.append((proxy, timeout))
        clock["now"] += 100.0  # this attempt consumed most of the budget
        return {"ok": False, "status_code": 0, "error": "connection reset by peer"}

    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pqb,
        "_routing_inputs",
        lambda proxy, pool: (["http://a:1", "http://b:2", "http://c:3"], None),
    )

    result = pqb._probe_with_rotation(
        "run",
        {"email": "a@example.com"},
        None,
        None,
        45,
        "auto",
        "US",
        1,
        deadline=135.0,
        clock=lambda: clock["now"],
    )

    # First attempt: full 45 s. Second: capped by the 35 s left. Then the budget
    # is gone, so the third exit is never dialled.
    assert attempts == [("http://a:1", 45), ("http://b:2", 35)]
    assert result["error_code"] == "account_budget_exhausted"
    assert result["proxy_attempts"] == 2


def test_account_budget_already_spent_makes_no_request(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    attempts: list[str | None] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        attempts.append(proxy)
        return {}

    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(pqb, "_routing_inputs", lambda proxy, pool: (["http://a:1"], None))

    result = pqb._probe_with_rotation(
        "run",
        {"email": "a@example.com"},
        None,
        None,
        45,
        "auto",
        "US",
        1,
        deadline=0.0,
        clock=lambda: 10.0,
    )

    assert attempts == []
    assert result["error_code"] == "account_budget_exhausted"
    assert result["proxy_attempts"] == 0


def test_refresh_timeout_is_the_per_request_timeout_and_the_budget_unit(monkeypatch):
    """``--refresh-timeout`` keeps its per-request meaning; the account gets × attempts."""
    from sms_tool.payment import qualification_batch as pqb

    captured = {}

    def fake_rotation(run_id, account, *args, **kwargs):
        captured["deadline"] = kwargs.get("deadline")
        return dict(
            ELIGIBLE_RESULT,
            payment_qualification_state=QUALIFICATION_ELIGIBLE,
            payment_qualification="可用",
        )

    monkeypatch.setattr(pqb, "_load_accounts", lambda *a, **k: [{"email": "a@example.com"}])
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "off")
    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "_probe_with_rotation", fake_rotation)
    monkeypatch.setattr(pqb, "_persist", lambda email, probe: True)
    monkeypatch.setattr(pqb, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(pqb, "time", _Clock(lambda: 1000.0))

    pqb.refresh_payment_qualification(emails=["a@example.com"], timeout=45)

    assert captured["deadline"] == 1000.0 + 45 * 3


# ── 2026-10-27 17:45 实跑的四个回归 ─────────────────────────────────────────
# A 3-account batch produced: sa*** 可用·PayPal（自定义）, jo*** 出口被拒,
# bo*** 检测失败(风控). The batch line said "完成 3 个账号…检测失败 1" (2 counted),
# the account badge lost 风控拦截, the warning cut the ACL phrase off, and two
# accounts burnt 3 attempts each on a pool the preflight had already proven dead.

STATES = {
    "eligible": QUALIFICATION_ELIGIBLE,
    "egress_denied": "egress_denied",
    "probe_failed": "probe_failed",
}


def test_summary_counts_egress_denied_so_the_totals_add_up():
    rows = [
        {"ok": True, "payment_qualification_state": QUALIFICATION_ELIGIBLE, "persisted": True},
        {"ok": False, "payment_qualification_state": "egress_denied", "persisted": True},
        {"ok": False, "payment_qualification_state": "probe_failed", "persisted": True},
    ]

    summary = pqb._summary(rows, method="auto")

    assert summary["egress_denied"] == 1
    assert summary["probe_failed"] == 1
    counted = (
        summary["eligible"]
        + summary["ineligible"]
        + summary["auth_invalid"]
        + summary["egress_denied"]
        + summary["probe_failed"]
        + summary["unknown"]
    )
    assert counted == summary["total"] == 3


def test_batch_completed_detail_names_egress_denials(monkeypatch):
    events: list[dict] = []
    results = {
        "a@example.com": dict(
            ELIGIBLE_RESULT, payment_qualification_state=QUALIFICATION_ELIGIBLE, payment_qualification="可用"
        ),
        "b@example.com": {
            "ok": False,
            "payment_qualification_state": "egress_denied",
            "payment_qualification": "出口被拒",
        },
        "c@example.com": {
            "ok": False,
            "payment_qualification_state": "probe_failed",
            "payment_qualification": "风控拦截",
        },
    }

    monkeypatch.setattr(pqb, "_load_accounts", lambda *a, **k: [{"email": e} for e in results])
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "off")
    monkeypatch.setattr(pqb, "_probe_with_rotation", lambda run_id, account, *a, **k: results[account["email"]])
    monkeypatch.setattr(pqb, "_persist", lambda email, probe: True)
    monkeypatch.setattr(
        pqb,
        "_emit",
        lambda run_id, stage, status, **kwargs: events.append({"stage": stage, "detail": kwargs.get("detail", "")}),
    )

    summary = pqb.refresh_payment_qualification(emails=list(results))

    assert summary["egress_denied"] == 1
    completed = [event for event in events if event["stage"] == "batch_completed"][0]
    assert "出口被拒 1" in completed["detail"]
    assert "检测失败 1" in completed["detail"]


def test_aggregate_badge_reports_risk_block_not_the_generic_failure():
    rows = [
        {
            "method": "paypal",
            "state": "probe_failed",
            "error": "checkout HTTP 400 code=Our systems have detected unusual activity. Please try again later.",
        }
    ]

    assert pq._aggregate_label("probe_failed", rows) == "风控拦截"
    plain = [{"method": "paypal", "state": "probe_failed", "error": "ReadTimeout: connection timed out"}]
    assert pq._aggregate_label("probe_failed", plain) == "检测失败"


ACL_CHAIN = (
    "ProxyError: HTTPSConnectionPool(host='api.stripe.com', port=443): Max retries exceeded with url: / "
    "(Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 503 "
    "Target host denied')))"
)


def test_stripe_reason_quotes_the_acl_phrase_instead_of_the_wrapper():
    reason = pqb._stripe_reason_text(ACL_CHAIN)

    assert "Target host denied" in reason
    assert "Tunnel connection failed" in reason
    # The old prefix cut produced a dangling "(Caused by ProxyError" and dropped
    # exactly the phrase above.
    assert not reason.endswith("ProxyError")
    assert len(reason) <= 120


def test_stripe_reason_cuts_long_text_on_a_word_boundary():
    reason = pqb._stripe_reason_text("x" * 40 + " " + "y" * 200)

    assert reason.endswith("…")
    assert "  " not in reason
    assert len(reason) <= 121


def test_rotation_stops_after_egress_denied_when_the_pool_is_proven_dead(monkeypatch):
    attempts: list[str | None] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        attempts.append(proxy)
        return {
            "ok": False,
            "payment_qualification_state": "egress_denied",
            "error": "Tunnel connection failed: 503 Target host denied",
        }

    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(pqb, "_routing_inputs", lambda proxy, pool: (["http://a:1", "http://b:2", "http://c:3"], None))

    result = pqb._probe_with_rotation(
        "run", {"email": "a@example.com"}, None, None, 45, "auto", "US", 1, stripe_unreachable=True
    )

    assert attempts == ["http://a:1"], "a proven-dead pool must not be re-dialled"
    assert result["rotation_stopped"] == "stripe_unreachable"


def test_rotation_still_rotates_on_egress_denied_when_stripe_was_not_preflighted(monkeypatch):
    attempts: list[str | None] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        attempts.append(proxy)
        return {"ok": False, "payment_qualification_state": "egress_denied", "error": "forbidden ip"}

    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(pqb, "_routing_inputs", lambda proxy, pool: (["http://a:1", "http://b:2", "http://c:3"], None))

    pqb._probe_with_rotation("run", {"email": "a@example.com"}, None, None, 45, "auto", "US", 1)

    assert attempts == ["http://a:1", "http://b:2", "http://c:3"], "a mixed pool may still have a working exit"


def test_risk_block_keeps_rotating_even_when_stripe_is_unreachable(monkeypatch):
    attempts: list[str | None] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        attempts.append(proxy)
        return {
            "ok": False,
            "payment_qualification_state": "probe_failed",
            "error": "checkout HTTP 400 code=Our systems have detected unusual activity.",
        }

    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 3)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(pqb, "_routing_inputs", lambda proxy, pool: (["http://a:1", "http://b:2", "http://c:3"], None))

    pqb._probe_with_rotation(
        "run", {"email": "a@example.com"}, None, None, 45, "auto", "US", 1, stripe_unreachable=True
    )

    # The risk refusal happens on chatgpt.com, so another exit can change it.
    assert attempts == ["http://a:1", "http://b:2", "http://c:3"]


def test_cli_summary_prints_the_egress_denied_counter(capsys):
    from sms_tool.commands import payment as pay

    pay._print_payment_qualification_summary(
        {
            "results": [
                {"ok": True, "payment_qualification": "可用", "probe": {}},
                {"ok": False, "payment_qualification": "出口被拒", "probe": {"error": "Target host denied"}},
                {"ok": False, "payment_qualification": "检测失败", "probe": {"error": "read timeout"}},
            ],
            "eligible": 1,
            "ineligible": 0,
            "auth_invalid": 0,
            "egress_denied": 1,
            "probe_failed": 1,
        }
    )

    out = capsys.readouterr().out
    assert "出口被拒 1" in out
    assert "检测失败 1" in out


# ── rotation really moves: one candidate, one dialled exit ───────────────────
# Regression (2026-10-27, second pass). The batch resolves the egress order
# itself: ``_routing_inputs`` puts the dedicated pool first, then the desktop
# route, and ``_probe_with_rotation`` hands each candidate to the single-account
# probe as ``proxy=``. That probe also promotes the dedicated pool to
# ``explicit`` (the fix from the same day, so the desktop's registration exit
# cannot outrank the operator's payment exit) — which demoted the *candidate*
# behind ``dedicated[0]``. Every "rotation" then re-dialled the same exit while
# ``proxy_attempts`` kept incrementing, and the row attributed the failure to
# the exit it never used. Measured with a two-exit dedicated pool: attempts 1
# and 2 both probed ``ded:1``, and ``proxy_host`` said ``ded:2``.


def test_rotation_dials_the_candidate_it_rotated_to(monkeypatch):
    import sms_tool.config as config_module

    probed: list[str] = []

    def fake_probe(**kwargs):
        probed.append(kwargs["proxy"])
        return {
            "ok": False,
            "status": "unknown",
            "eligible": None,
            "conclusive": False,
            "error": "connection reset by peer",
            "error_code": "checkout_transport_failed",
        }

    dedicated = ["http://ded:1", "http://ded:2"]
    # Hermetic: the shared config would otherwise contribute the promotion lane
    # from the seeded example file (harmless after ``explicit``, but noisy).
    monkeypatch.setattr(config_module, "CFG", {})
    monkeypatch.setattr(pq, "configured_qualification_proxy_pool", lambda config=None: dedicated)
    monkeypatch.setattr(pq, "payment_method_capability_probe", fake_probe)
    monkeypatch.setattr(pqb, "configured_qualification_proxy_pool", lambda: dedicated)
    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 2)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: None)

    probe = pqb._probe_with_rotation(
        "run",
        {"email": "a@example.com", "access_token": "AT"},
        None,
        None,
        5,
        "direct_card",
        "US",
        1,
    )

    assert probed == ["http://ded:1", "http://ded:2"]
    # Attribution must describe the exit that was actually used, not the one the
    # rotation intended (the desktop renders this next to the failure).
    assert probe["proxy_host"] == "ded:2"
    assert probe["proxy_attempts"] == 2

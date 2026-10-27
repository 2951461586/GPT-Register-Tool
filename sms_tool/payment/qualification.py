"""Read-only payment-qualification probe for one saved account.

What "支付资格" means here
-------------------------
Whether this account can *reach a zero-due checkout for a payment method that the
egress country actually supports* — the same observation the payment lane makes
before it starts a payment, stopped before any payment-method creation,
confirmation or approval. Nothing is charged and no payment method is created:
the probe creates a Checkout and calls Stripe init, then classifies the answer
(``payment.capability.build_capability_probe_result``).

Country is the whole game (2026-10-26)
--------------------------------------
The first version defaulted to GCash and sent the method's own country (PH) as
``billing_details.country`` no matter where the account/exit was. OpenAI answers
that with::

    400 checkout returned HTTP 400: Billing country must match request country.

Measured field note (``.workbuddy-ai/memory/2026-09-26.md`` §11): the request
country follows the **egress**, and a rotating endpoint that flips to VN produces
exactly this 400 for an IN method — "需每号探测，否则出 Billing country must match
400". So the probe now:

1. resolves the account's *effective* country — measured exit first (that is
   what the server compares), then ``registration_country``;
2. selects candidate methods **from the catalogue for that country** (PH →
   GCash/GrabPay/direct card, VN → MoMo, IN → UPI, ID → GoPay/QRIS, …) instead of
   forcing one method on every account;
3. passes ``billing_country`` / ``currency`` / ``payment_locale`` / browser
   locale+timezone explicitly, so the contract matches the request;
4. skips a method whose catalogue country differs from the exit country instead
   of paying for a guaranteed 400, and reports that as ``无可用方式`` with
   ``method_country_mismatch``.

Why it is a separate module from ``promotion_batch``
----------------------------------------------------
The plan/优惠 probe answers "what plan is this account on"; this one answers "can
it pay". They fail differently too: a promotion 401 says the AT is dead, a
checkout ``Billing country must match`` says the exit is wrong. Keeping them
apart keeps each failure contract single-purpose.

Layering
--------
This module lives in the **payment lane** on purpose: it calls the shared
checkout capability probe, and ``scripts/lane_coupling_ratchet.py`` freezes which
non-payment modules may import payment code. Keeping it under ``payment/`` means
the batch can call it without adding a new outside importer, and the CLI boundary
lives in ``commands/payment.py`` (already in that inventory).

Cross-directory imports stay function-local where a module-level edge would grow
a frozen pair (``scripts/import_layer_ratchet.py``); the account payload arrives
with its token already attached, so nothing here reads the account store.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .capability import payment_method_capability_probe
from .catalog import PAYMENT_METHODS
from .qualification_states import (
    COUNTRY_MISMATCH_DECISION,
    QUALIFICATION_AUTH_INVALID,
    QUALIFICATION_ELIGIBLE,
    QUALIFICATION_INELIGIBLE,
    QUALIFICATION_PROBE_FAILED,
    QUALIFICATION_UNKNOWN,
    is_country_mismatch,
    method_label,
    qualification_label,
    qualification_state,
)

logger = logging.getLogger(__name__)

#: ``auto`` = every catalogue method whose country matches the exit country.
#: Kept as the default so an operator does not have to know which method a
#: PH/VN/IN account can use — the previous hardcoded ``gcash`` is exactly what
#: produced the 400s on non-PH accounts.
DEFAULT_QUALIFICATION_METHOD = "auto"

#: Methods whose flow is **not** tied to a local billing country: a redirect
#: wallet agreement (PayPal) and a card checkout work from any exit as long as
#: ``billing_details.country`` matches the request — which is exactly what this
#: probe now sets. Their catalogue country is only the payment lane's *default*
#: billing country, so excluding them for a JP/KR exit (no local wallet method in
#: the catalogue) would report "无可用方式" for accounts that can in fact pay.
COUNTRY_AGNOSTIC_METHODS: tuple[str, ...] = ("paypal", "direct_card")

#: A method whose catalogue country differs from the exit is skipped, not probed.
_REASON_COUNTRY_MISMATCH = "method_country_mismatch"
_REASON_NO_METHOD = "no_method_for_country"
_REASON_COUNTRY_UNKNOWN = "country_unknown"
_REASON_UNKNOWN_METHOD = "unknown_method"


# ── configuration ────────────────────────────────────────────────────────────
def _config_section(config: Any = None) -> Mapping[str, Any]:
    if config is None:
        from ..config import CFG

        config = CFG
    try:
        section = config.get("payment_qualification") if isinstance(config, Mapping) else None
    except Exception:
        section = None
    return section if isinstance(section, Mapping) else {}


def configured_qualification_method(config: Any = None) -> str:
    """Read ``payment_qualification.method`` (``auto`` or a comma list)."""
    value = _config_section(config).get("method")
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    return text or DEFAULT_QUALIFICATION_METHOD


def configured_qualification_country(config: Any = None) -> str:
    """Read ``payment_qualification.country`` (empty = detect per account)."""
    return str(_config_section(config).get("country") or "").strip().upper()


def parse_method_selection(value: Any) -> list[str]:
    """Normalise ``"gcash,momo"`` / ``["gcash", "momo"]`` / ``""`` to a list.

    ``auto`` (and an empty value) yields an empty list, which callers read as
    "derive from the country".
    """
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        text = str(value or "")
        raw = text.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",")
    methods: list[str] = []
    for item in raw:
        method = item.strip().lower().replace("-", "_")
        if method and method != "auto" and method not in methods:
            methods.append(method)
    return methods


# ── catalogue / country ──────────────────────────────────────────────────────
def _catalog_country(method: str) -> str:
    spec = PAYMENT_METHODS.get(method)
    return str(getattr(spec, "country", "") or "").upper()


def methods_for_country(country: str) -> list[str]:
    """Candidates for an exit country: local methods first, then agnostic ones.

    Every tier is included: the probe is one Checkout + Stripe init, read-only,
    and "can this account pay with BLIK from a Polish exit" is a legitimate
    question even though the batch extractor for it is not shipped.

    Country-agnostic methods are appended (deduplicated) so an exit with no local
    wallet in the catalogue — JP has none — still gets a real answer instead of a
    blanket "无可用方式".
    """
    target = str(country or "").strip().upper()
    if not target:
        return []
    local = [key for key, spec in PAYMENT_METHODS.items() if str(getattr(spec, "country", "")).upper() == target]
    for method in COUNTRY_AGNOSTIC_METHODS:
        if method in PAYMENT_METHODS and method not in local:
            local.append(method)
    return local


def _measured_country(proxy: str | None) -> str:
    """Exit country **measured** through ``proxy``, or ``""``.

    The hint is deliberately not passed: ``geo.resolver.resolve`` trusts a known
    credential region and returns a hint-only ``ProxyGeo`` without touching the
    network, which is exactly the ``Billing country must match request country``
    failure this probe exists to prevent. ``need_timezone=True`` also forces a
    probe past a cached country-only/hint answer; the shared resolver still
    caches the real measurement per proxy.
    """
    if not proxy:
        return ""
    try:
        from ..geo import resolve_proxy_geo

        geo = resolve_proxy_geo(proxy, hint="", need_timezone=True)
    except Exception:
        return ""
    if str(getattr(geo, "source", "") or "") == "hint":
        return ""
    return str(getattr(geo, "country", "") or "").strip().upper()


def resolve_qualification_country(
    account: Mapping[str, Any],
    proxy: str | None,
    explicit: str = "",
) -> tuple[str, str]:
    """Return ``(country, source)`` for the checkout request.

    Precedence: explicit input → **measured exit** → ``registration_country`` →
    ``identity_context.country``. The measured exit wins over the stored country
    because the server compares ``billing_details.country`` against the request's
    egress, which is why a flapping endpoint produced the 400.
    """
    code = str(explicit or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return code, "explicit"
    measured = _measured_country(proxy)
    if measured:
        return measured, "measured"
    stored = str(account.get("registration_country") or "").strip().upper()
    if len(stored) == 2 and stored.isalpha():
        return stored, "registration_country"
    context = account.get("identity_context")
    if isinstance(context, Mapping):
        identity_country = str(context.get("country") or "").strip().upper()
        if len(identity_country) == 2 and identity_country.isalpha():
            return identity_country, "identity_context"
    return "", "unknown"


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in str(cookie_header or "").split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == name.lower():
            return value.strip().strip('"')
    return ""


def _device_id_and_cookie(account: Mapping[str, Any]) -> tuple[str, str]:
    """Return the device id **and** the cookie header the Checkout must present.

    Measured 2026-10-27 (first live run of this probe): sending only
    ``oai-did=<id>`` while the access token belonged to a real browser session
    produced ``400 Our systems have detected unusual activity``. A Checkout is a
    same-site action: it needs the session cookies the token was issued with.
    The account payload already stores them (``cookie_header``), so the probe
    presents that header verbatim and keeps the ``oai-did`` it contains — the
    Sentinel pair is device-bound, so the header and ``OAI-Device-Id`` must agree.
    """
    cookie = str(account.get("cookie_header") or "").strip()
    if not cookie:
        session = account.get("auth_session")
        token = ""
        if isinstance(session, Mapping):
            token = str(session.get("sessionToken") or session.get("session_token") or "").strip()
        if token:
            cookie = f"__Secure-next-auth.session-token={token}"
    context = account.get("identity_context")
    context_device = str(context.get("device_id") or "").strip() if isinstance(context, Mapping) else ""
    device_id = (
        _cookie_value(cookie, "oai-did")
        or context_device
        or str(account.get("device_id") or "").strip()
        or str(uuid.uuid4())
    )
    if not _cookie_value(cookie, "oai-did"):
        cookie = f"{cookie}; oai-did={device_id}" if cookie else f"oai-did={device_id}"
    return device_id, cookie


def _auth_context(account: Mapping[str, Any]) -> dict[str, str]:
    device_id, cookie = _device_id_and_cookie(account)
    return {"device_id": device_id, "oai_did": device_id, "cookie_header": cookie}


# ── one method ───────────────────────────────────────────────────────────────
def _probe_kwargs(method: str, country: str) -> dict[str, Any]:
    """Contract fields for one method, sourced from the canonical catalogues."""
    from ..checkout_contract import browser_profile_for_country, PAYMENT_METHOD_PROFILES
    from .wire import CURRENCY_MAP

    spec = PAYMENT_METHOD_PROFILES[method]
    browser = browser_profile_for_country(country)
    # A country-agnostic method billed into the exit country must use that
    # country's currency, not the catalogue's home-country default (direct_card
    # is PH/PHP; sending PHP with billing JP/GB/VN is a guaranteed mismatch).
    # PayPal's dedicated probe already derives currency from ``checkout_country``.
    currency = CURRENCY_MAP.get(country, spec.currency) if method in COUNTRY_AGNOSTIC_METHODS else spec.currency
    return {
        "payment_method": method,
        "billing_country": country,
        "checkout_country": country,
        "currency": currency,
        "payment_locale": spec.payment_locale,
        "browser_locale": browser.browser_locale,
        "browser_timezone": browser.browser_timezone,
    }


def _scrub(error: Any, *proxies: str) -> str:
    from ..phone_proxy import redact_proxy_url

    text = str(error or "")[:300]
    for candidate in proxies:
        if candidate:
            text = text.replace(candidate, redact_proxy_url(candidate, empty_placeholder=""))
    return text


def _method_row(method: str, result: Mapping[str, Any]) -> dict[str, Any]:
    state = qualification_state(result)
    decision = str(result.get("decision") or "")
    if not decision and is_country_mismatch(result.get("error"), result.get("error_code")):
        # The server's 400 carries no ``decision``; normalise it so the aggregate
        # label can say "地区不符" instead of a generic "无可用方式".
        decision = COUNTRY_MISMATCH_DECISION
    row = {
        "method": method,
        "method_label": method_label(method),
        "state": state,
        "label": qualification_label(result, method),
        "eligible": state == QUALIFICATION_ELIGIBLE,
        "conclusive": state in {QUALIFICATION_ELIGIBLE, QUALIFICATION_INELIGIBLE},
        "decision": decision,
        "error": str(result.get("error") or ""),
        "error_code": str(result.get("error_code") or ""),
        "error_stage": str(result.get("error_stage") or ""),
        "amount": result.get("amount"),
        "currency": str(result.get("currency") or ""),
        "payment_method_types": list(result.get("payment_method_types") or []),
    }
    return row


def _skip_row(method: str, reason_code: str, reason: str) -> dict[str, Any]:
    return {
        "method": method,
        "method_label": method_label(method),
        # A method that cannot apply to this country is a *conclusive* "no" for
        # this account, not a transport failure -- the operator asked whether it
        # can pay, and the answer is no.
        "state": QUALIFICATION_INELIGIBLE,
        "label": "无可用方式",
        "eligible": False,
        "conclusive": True,
        "decision": reason_code,
        "error": reason,
        "error_code": reason_code,
        "error_stage": "country_selection",
        "amount": None,
        "currency": "",
        "payment_method_types": [],
    }


def _unknown_method_row(method: str) -> dict[str, Any]:
    """A typo/config error must be a row, not a ``KeyError`` that kills the batch."""
    return {
        "method": method,
        "method_label": method_label(method),
        "state": QUALIFICATION_UNKNOWN,
        "label": "未知方式",
        "eligible": False,
        "conclusive": False,
        "decision": _REASON_UNKNOWN_METHOD,
        "error": f"未知支付方式：{method}",
        "error_code": _REASON_UNKNOWN_METHOD,
        "error_stage": "method_selection",
        "amount": None,
        "currency": "",
        "payment_method_types": [],
    }


# ── aggregate ────────────────────────────────────────────────────────────────
def _aggregate_state(rows: Sequence[Mapping[str, Any]]) -> str:
    states = [str(row.get("state") or "") for row in rows]
    for candidate in (
        QUALIFICATION_ELIGIBLE,
        QUALIFICATION_INELIGIBLE,
        QUALIFICATION_AUTH_INVALID,
        QUALIFICATION_PROBE_FAILED,
    ):
        if candidate in states:
            return candidate
    return QUALIFICATION_UNKNOWN


def _aggregate_label(state: str, rows: Sequence[Mapping[str, Any]]) -> str:
    if state == QUALIFICATION_ELIGIBLE:
        names = [str(row.get("method_label") or "") for row in rows if row.get("eligible")]
        return "可用·" + ", ".join(name for name in names if name)
    if state == QUALIFICATION_INELIGIBLE:
        decisions = {
            str(row.get("decision") or "") for row in rows if str(row.get("state") or "") == QUALIFICATION_INELIGIBLE
        }
        if decisions and decisions <= {_REASON_COUNTRY_MISMATCH, _REASON_NO_METHOD, "checkout_country_mismatch"}:
            return "无可用方式·地区不符"
        return "无可用方式"
    if state == QUALIFICATION_AUTH_INVALID:
        return "AT失效"
    if state == QUALIFICATION_PROBE_FAILED:
        return "检测失败"
    return "未确定"


def _first_error(rows: Sequence[Mapping[str, Any]]) -> str:
    for row in rows:
        error = str(row.get("error") or "")
        if error and not row.get("eligible"):
            return error
    return ""


# ── public probe ─────────────────────────────────────────────────────────────
def check_payment_qualification(
    account: Any,
    proxy: str | None = None,
    timeout: int = 45,
    *,
    method: str = "",
    country: str = "",
    proxy_pool: str | list[str] | None = None,
    probe: Any = None,
    last_eligible_only: bool | None = None,
    probe_all_methods: bool | None = None,
) -> dict[str, Any]:
    """Probe one account's checkout capability, country-aware, and aggregate.

    ``probe`` is an injection seam; it defaults to
    ``payment.capability.payment_method_capability_probe`` and is called with the
    full contract (``billing_country`` included) so a test can assert the request
    country the backend would have sent.

    ``last_eligible_only`` short-circuits after the first eligible method. The
    default is to stop there, because every candidate is its own Checkout create
    and the create budget is finite. ``probe_all_methods`` (config
    ``payment_qualification.probe_all_methods`` or the explicit argument)
    restores the full grid for diagnosis.
    """
    data = dict(account) if isinstance(account, Mapping) else {}
    selection = parse_method_selection(method)
    if last_eligible_only is None:
        if probe_all_methods is None:
            probe_all_methods = bool(_config_section().get("probe_all_methods", False))
        last_eligible_only = not probe_all_methods
    resolved_proxy, normalized_proxy, proxy_source = _select_proxy(data, proxy, proxy_pool)
    resolved_country, country_source = resolve_qualification_country(data, normalized_proxy, country)
    token = str(data.get("access_token") or "").strip()

    def failure(reason: str, state: str) -> dict[str, Any]:
        return _failure(
            reason,
            state=state,
            method=",".join(selection) or DEFAULT_QUALIFICATION_METHOD,
            proxy_source=proxy_source,
            country=resolved_country,
            country_source=country_source,
        )

    if not token:
        return failure("missing_access_token", QUALIFICATION_AUTH_INVALID)
    if not resolved_country:
        return failure(_REASON_COUNTRY_UNKNOWN, QUALIFICATION_UNKNOWN)

    candidates, skipped = _select_candidates(selection, resolved_country)
    if not candidates and not skipped:
        return failure(_REASON_NO_METHOD, QUALIFICATION_INELIGIBLE)

    probe_fn = probe or payment_method_capability_probe
    rows: list[dict[str, Any]] = list(skipped)
    for candidate in candidates:
        result = _run_method(probe_fn, token, data, candidate, resolved_country, normalized_proxy, timeout)
        rows.append(_method_row(candidate, result))
        if last_eligible_only and rows[-1]["eligible"]:
            break

    state = _aggregate_state(rows)
    label = _aggregate_label(state, rows)
    eligible = [str(row["method"]) for row in rows if row.get("eligible")]
    return {
        "ok": state == QUALIFICATION_ELIGIBLE,
        "conclusive": state in {QUALIFICATION_ELIGIBLE, QUALIFICATION_INELIGIBLE},
        "payment_method": ",".join(eligible) or ",".join(str(row["method"]) for row in rows),
        "payment_methods": rows,
        "eligible_methods": eligible,
        "payment_qualification_state": state,
        "payment_qualification": label,
        "country": resolved_country,
        "country_source": country_source,
        "proxy_source": proxy_source,
        "error": _scrub(_first_error(rows), str(proxy or ""), str(resolved_proxy or ""), normalized_proxy),
    }


def _select_candidates(
    selection: Sequence[str],
    country: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split the requested methods into probed candidates and skipped rows."""
    requested = list(selection) if selection else methods_for_country(country)
    candidates: list[str] = []
    skipped: list[dict[str, Any]] = []
    for method in requested:
        if method not in PAYMENT_METHODS:
            skipped.append(_unknown_method_row(method))
            continue
        method_country = _catalog_country(method)
        if method in COUNTRY_AGNOSTIC_METHODS:
            # Probed with ``billing_country = country`` (see ``_probe_kwargs``),
            # so the request cannot trip the server's country check.
            candidates.append(method)
            continue
        if method_country and method_country != country:
            skipped.append(
                _skip_row(
                    method,
                    _REASON_COUNTRY_MISMATCH,
                    f"该方法只支持 {method_country} 出口（当前 {country}）",
                )
            )
            continue
        candidates.append(method)
    return candidates, skipped


def _run_method(
    probe_fn: Any,
    token: str,
    account: Mapping[str, Any],
    method: str,
    country: str,
    proxy: str,
    timeout: int,
) -> dict[str, Any]:
    try:
        kwargs = _probe_kwargs(method, country)
        result = probe_fn(
            access_token=token,
            auth_context=_auth_context(account),
            proxy=proxy or None,
            timeout=timeout,
            **kwargs,
        )
    except Exception as exc:
        return {
            "payment_method": method,
            "status": "failed",
            "error": _scrub(f"{type(exc).__name__}: {exc}", proxy),
            "error_code": "qualification_probe_raised",
            "error_stage": "capability_probe",
            "eligible": None,
            "conclusive": False,
        }
    if not isinstance(result, dict):
        return {
            "payment_method": method,
            "status": "failed",
            "error": "qualification_probe_invalid_result",
            "error_code": "qualification_probe_invalid_result",
            "error_stage": "capability_probe",
            "eligible": None,
            "conclusive": False,
        }
    payload = dict(result)
    if payload.get("error"):
        payload["error"] = _scrub(payload["error"], proxy)
    return payload


def _select_proxy(
    account: Mapping[str, Any],
    explicit: str | None,
    pool: str | list[str] | None = None,
) -> tuple[str | None, str, str]:
    """Return ``(resolved_proxy, normalized_proxy, source)``.

    The ``promotion`` lane is deliberate: qualification is a read-only
    post-registration probe, and that lane carries the operator's "reuse the
    signup egress" decision (``account_health.use_registration_affinity``).
    """
    from ..config import CFG
    from ..phone_proxy import normalize_proxy_url
    from ..proxy.routing import select_operation_proxy_candidate

    selected = select_operation_proxy_candidate(
        dict(account),
        operation="promotion",
        explicit=explicit,
        pool=pool,
        config=CFG,
    )
    resolved = selected.proxy if selected else None
    return resolved, normalize_proxy_url(resolved or ""), (selected.source if selected else "direct")


def _failure(
    reason: str,
    *,
    state: str,
    method: str,
    proxy_source: str,
    country: str = "",
    country_source: str = "unknown",
) -> dict[str, Any]:
    return {
        "ok": False,
        "conclusive": state in {QUALIFICATION_ELIGIBLE, QUALIFICATION_INELIGIBLE},
        "payment_method": method,
        "payment_methods": [],
        "eligible_methods": [],
        "payment_qualification_state": state,
        "payment_qualification": qualification_label(
            {"status": "failed", "error": reason, "error_code": reason}, method
        ),
        "country": country,
        "country_source": country_source,
        "proxy_source": proxy_source,
        "error": reason,
    }


__all__ = [
    "COUNTRY_AGNOSTIC_METHODS",
    "DEFAULT_QUALIFICATION_METHOD",
    "check_payment_qualification",
    "configured_qualification_country",
    "configured_qualification_method",
    "methods_for_country",
    "parse_method_selection",
    "resolve_qualification_country",
]

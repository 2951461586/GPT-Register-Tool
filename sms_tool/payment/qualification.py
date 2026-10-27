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
    QUALIFICATION_EGRESS_DENIED,
    QUALIFICATION_ELIGIBLE,
    QUALIFICATION_INELIGIBLE,
    QUALIFICATION_PROBE_FAILED,
    QUALIFICATION_UNKNOWN,
    is_country_mismatch,
    is_risk_blocked,
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
_REASON_CURRENCY_UNKNOWN = "currency_unknown"

#: Which transport answers a method. Only methods with the same kind can share
#: one Checkout: PayPal and GCash each own a probe (``_paypal_capability_probe`` /
#: ``run_gcash_provider``), every other method goes through the generic
#: Stripe-capability transport.
_PROBE_KIND_PAYPAL = "paypal"
_PROBE_KIND_GCASH = "gcash"
_PROBE_KIND_STRIPE = "stripe"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def configured_qualification_proxy_pool(config: Any = None) -> list[str]:
    """Dedicated egress for the qualification probe (empty = promotion fallback).

    Accepted keys, in order:

    * ``payment_qualification.proxy_pool``
    * ``proxy.lanes.payment_qualification``

    An explicitly configured pool wins over the desktop's registration-pool
    argument because a ChatGPT-only exit can reach ``chatgpt.com`` but still
    deny ``api.stripe.com`` at the proxy tunnel (measured 2026-10-27).
    """
    from ..config import CFG
    from ..proxy.routing import canonical_lane_pool, parse_lane_proxy_pool

    source = config if isinstance(config, Mapping) else CFG
    dedicated = parse_lane_proxy_pool(_config_section(source).get("proxy_pool"))
    if dedicated:
        return dedicated
    return canonical_lane_pool(source, "payment_qualification") or []


def configured_max_proxy_attempts(config: Any = None) -> int:
    """``payment_qualification.max_proxy_attempts`` (default 3, clamped 1..32)."""
    try:
        value = int(_config_section(config).get("max_proxy_attempts", 3))
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 32))


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
def _probe_kind(method: str) -> str:
    """Transport that would answer ``method`` (see the ``_PROBE_KIND_*`` notes)."""
    if method == "paypal":
        return _PROBE_KIND_PAYPAL
    if method == "gcash":
        return _PROBE_KIND_GCASH
    return _PROBE_KIND_STRIPE


def _contract_currency(method: str, country: str) -> str:
    """Currency the Checkout for ``method`` from ``country`` must bill in.

    Empty means "we do not know", and that is a real answer: ``direct_card`` /
    ``paypal`` are country-agnostic, so their currency follows the exit country
    through :data:`payment.wire.CURRENCY_MAP`. That map covers the catalog
    markets, not every country a residential pool can leave from, and the old
    code silently billed the catalogue default (``PHP`` for ``direct_card``)
    against, say, a ``MX`` billing country -- a guaranteed rejection reported as
    a generic probe failure. Callers skip (``currency_unknown``) instead.
    """
    spec = PAYMENT_METHODS[method]
    if method in COUNTRY_AGNOSTIC_METHODS:
        from .wire import CURRENCY_MAP

        return str(CURRENCY_MAP.get(str(country or "").strip().upper()) or "").upper()
    return str(getattr(spec, "currency", "") or "").upper()


def _contract_key(method: str, country: str) -> tuple[str, str, str]:
    """Identity of the Checkout request: same key ⇒ the same create payload.

    ``CheckoutRequestContract.checkout_payload()`` carries the billing
    country/currency, plan, entry point and promo campaign -- **not** the payment
    method (that only selects how the answer is classified, plus the Stripe
    locale). So two methods with the same kind, country and currency send the
    same create, and one answer serves both (see ``_derive_row``).
    """
    return (_probe_kind(method), str(country or "").strip().upper(), _contract_currency(method, country))


def _probe_kwargs(method: str, country: str) -> dict[str, Any]:
    """Contract fields for one method, sourced from the canonical catalogues."""
    from ..checkout_contract import browser_profile_for_country, PAYMENT_METHOD_PROFILES

    spec = PAYMENT_METHOD_PROFILES[method]
    browser = browser_profile_for_country(country)
    # A country-agnostic method billed into the exit country must use that
    # country's currency, not the catalogue's home-country default (direct_card
    # is PH/PHP; sending PHP with billing JP/GB/VN is a guaranteed mismatch).
    # PayPal's dedicated probe already derives currency from ``checkout_country``.
    # ``_select_candidates`` refuses an agnostic method whose currency is unknown,
    # so the ``or spec.currency`` arm is only reached by non-agnostic methods.
    currency = _contract_currency(method, country) or spec.currency
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
        # ``oaics`` custom checkout never calls Stripe init; a Stripe
        # ``cs_*`` checkout does. The badge must not conflate the two.
        "checkout_kind": str(result.get("checkout_kind") or result.get("probe_checkout_kind") or ""),
        # ``http_status`` used to be dropped here, which left the batch's
        # "an HTTP answer proves the exit worked, so do not rotate" rule
        # (``_retryable_transport``) with no status to read -- it only ever saw a
        # missing key. ``retryable`` is the probe's own verdict and is what the
        # operator needs to tell "try another exit" from "this method is done".
        "http_status": _as_int(result.get("http_status")),
        "retryable": bool(result.get("retryable")),
        # ``checkout`` = this row cost its own Checkout; ``shared_checkout`` = the
        # row was derived from another method's identical request.
        "evidence_source": "checkout",
    }
    return row


def _method_token(value: Any) -> str:
    """Canonical comparison token for one method name/id (``Kakao-Pay`` → ``kakao_pay``)."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _method_inventory(result: Mapping[str, Any]) -> frozenset[str]:
    """Payment methods a completed probe proved the Checkout *publishes*.

    Empty means the answer carries no method list (a failure, or an ``oaics``
    custom checkout that only returns channel ids we cannot name), so nothing can
    be inferred from it and the next candidate must be probed for real.
    """
    tokens: set[str] = set()
    for key in ("payment_method_types", "custom_payment_methods"):
        for item in result.get(key) or []:
            # ``checkout_contract`` already normalised these tokens with its alias
            # table when it built the evidence, so the same rewrite is enough here
            # and this module needs no extra import edge for it.
            token = _method_token(item)
            if token:
                tokens.add(token)
    return frozenset(tokens)


def _derive_row(method: str, base: Mapping[str, Any], base_method: str) -> dict[str, Any] | None:
    """Answer ``method`` from the Checkout that already answered ``base_method``.

    The create payload is method-independent (``_contract_key``), so the second
    create a per-method loop would send is byte-identical to the first: measured
    2026-10-27, a PH account probed ``grabpay`` and ``direct_card`` with the same
    ``billing_details {country: PH, currency: PHP}`` body. ``build_capability_probe_result``
    already reduced the verdict to two parts -- *contract-level* (currency, zero
    due) and *method availability* (is the method in the published list) -- and
    only the second differs between methods. So: copy the contract verdict, then
    replace the availability.

    ``None`` means "no inventory in the evidence, probe it yourself".
    """
    inventory = _method_inventory(base)
    if not inventory:
        return None
    stripe_type = _method_token(getattr(PAYMENT_METHODS[method], "stripe_type", method))
    row = _method_row(method, base)
    row["evidence_source"] = "shared_checkout"
    row["derived_from"] = base_method
    if stripe_type in inventory:
        return row
    row.update(
        {
            "state": QUALIFICATION_INELIGIBLE,
            "label": "无可用方式",
            "eligible": False,
            # The published list is an answer: the method is not offered, which is
            # a conclusive "no" for this account, not a transport failure.
            "conclusive": True,
            "decision": "payment_method_unavailable",
            "error": f"该出口的 Checkout 未发布 {method_label(method)}",
            "error_code": "payment_method_unavailable",
            "error_stage": "method_selection",
            "retryable": False,
        }
    )
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
def _currency_unknown_row(method: str, country: str) -> dict[str, Any]:
    """A method we cannot bill from this exit: no contract, so nothing learned."""
    return {
        "method": method,
        "method_label": method_label(method),
        "state": QUALIFICATION_UNKNOWN,
        "label": "币种未知",
        "eligible": False,
        "conclusive": False,
        "decision": _REASON_CURRENCY_UNKNOWN,
        "error": f"{country} 不在货币映射表内，无法构造合法的 billing 币种",
        "error_code": _REASON_CURRENCY_UNKNOWN,
        "error_stage": "country_selection",
        "amount": None,
        "currency": "",
        "payment_method_types": [],
        "checkout_kind": "",
        "http_status": 0,
        "retryable": False,
        "evidence_source": "country_selection",
    }


def _aggregate_state(rows: Sequence[Mapping[str, Any]]) -> str:
    states = [str(row.get("state") or "") for row in rows]
    for candidate in (
        QUALIFICATION_ELIGIBLE,
        QUALIFICATION_INELIGIBLE,
        QUALIFICATION_AUTH_INVALID,
        # Ahead of ``probe_failed``: an egress denial is the more specific
        # explanation when the batch also carries generic failures.
        QUALIFICATION_EGRESS_DENIED,
        QUALIFICATION_PROBE_FAILED,
    ):
        if candidate in states:
            return candidate
    return QUALIFICATION_UNKNOWN


def _aggregate_label(state: str, rows: Sequence[Mapping[str, Any]]) -> str:
    if state == QUALIFICATION_ELIGIBLE:
        eligible_rows = [row for row in rows if row.get("eligible")]
        names = [str(row.get("method_label") or "") for row in eligible_rows]
        label = "可用·" + ", ".join(name for name in names if name)
        kinds = {str(row.get("checkout_kind") or "") for row in eligible_rows if row.get("checkout_kind")}
        if len(kinds) == 1:
            kind = next(iter(kinds))
            if kind == "oaics":
                label += "（自定义）"
            elif kind == "stripe":
                label += "（Stripe）"
        return label
    if state == QUALIFICATION_INELIGIBLE:
        decisions = {
            str(row.get("decision") or "") for row in rows if str(row.get("state") or "") == QUALIFICATION_INELIGIBLE
        }
        if decisions and decisions <= {_REASON_COUNTRY_MISMATCH, _REASON_NO_METHOD, "checkout_country_mismatch"}:
            return "无可用方式·地区不符"
        return "无可用方式"
    if state == QUALIFICATION_AUTH_INVALID:
        return "AT失效"
    if state == QUALIFICATION_EGRESS_DENIED:
        return "出口被拒"
    if state == QUALIFICATION_PROBE_FAILED:
        # Mirror the per-method label: a risk refusal is not "检测失败". Measured
        # 2026-10-27, bo***@icloud.com failed only with ``400 unusual activity``
        # and the account badge still read 检测失败.
        if any(is_risk_blocked(row.get("error"), row.get("error_code")) for row in rows):
            return "风控拦截"
        return "检测失败"
    return "未确定"


def _first_error(rows: Sequence[Mapping[str, Any]]) -> str:
    for row in rows:
        error = str(row.get("error") or "")
        if error and not row.get("eligible"):
            return error
    return ""


def _first_status(rows: Sequence[Mapping[str, Any]]) -> int:
    """HTTP status of the first non-eligible row that carries one (0 = none)."""
    for row in rows:
        if row.get("eligible"):
            continue
        status = _as_int(row.get("http_status"))
        if status:
            return status
    return 0


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
    pinned_proxy: bool = False,
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

    ``pinned_proxy`` says "``proxy`` is already the head of a caller-resolved
    egress order, do not re-rank it". The batch sets it: it resolves the order
    once (``qualification_batch._routing_inputs``) and then rotates through that
    list, handing one candidate per attempt. Without it the dedicated pool —
    promoted to ``explicit`` below — outranked the candidate, so every rotation
    re-dialled ``dedicated[0]``: ``max_proxy_attempts`` became a no-op and the
    batch row attributed the failure to an exit it never used.
    """
    data = dict(account) if isinstance(account, Mapping) else {}
    selection = parse_method_selection(method)
    if last_eligible_only is None:
        if probe_all_methods is None:
            probe_all_methods = bool(_config_section().get("probe_all_methods", False))
        last_eligible_only = not probe_all_methods
    # 🔴 The dedicated qualification exit goes in as *explicit*, exactly like the
    # batch path (``qualification_batch._routing_inputs``): every CLI/desktop
    # caller also passes the registration proxy, and explicit input outranks the
    # pool in ``operation_proxy_candidates``. Treating the dedicated pool as the
    # `pool` argument demoted it behind the ChatGPT-only registration exit, so
    # the operator's Stripe-capable exit was tried last instead of first.
    dedicated = configured_qualification_proxy_pool()
    pool_input: Any
    if pinned_proxy and proxy:
        # The caller already ordered the candidates and handed this one, so it
        # belongs at the head; the dedicated pool stays behind it as a fallback.
        # ``_select_proxy`` only reads the head, so no operation pool is needed.
        explicit_input: Any = [str(proxy), *dedicated]
        pool_input = []
    else:
        explicit_input = dedicated if dedicated else proxy
        pool_input = (proxy_pool or proxy) if dedicated else proxy_pool
    resolved_proxy, normalized_proxy, proxy_source = _select_proxy(data, explicit_input, pool_input)
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
    # One Checkout per *contract*, not per method (see ``_derive_row``). The
    # evidence is keyed by ``_contract_key``; ``probe_all_methods`` is the
    # diagnostic grid, so there every method pays for its own create.
    allow_shared = not probe_all_methods
    evidence: dict[tuple[str, str, str], tuple[str, Mapping[str, Any]]] = {}
    derived_methods: set[str] = set()
    for candidate in candidates:
        if candidate in derived_methods:
            # Already answered from another method's identical Checkout.
            continue
        key = _contract_key(candidate, resolved_country)
        shared = evidence.get(key) if allow_shared else None
        derived = _derive_row(candidate, shared[1], shared[0]) if shared else None
        if derived is not None:
            rows.append(derived)
            stop = bool(derived["eligible"])
        else:
            result = _run_method(probe_fn, token, data, candidate, resolved_country, normalized_proxy, timeout)
            row = _method_row(candidate, result)
            rows.append(row)
            stop = bool(row["eligible"])
            if allow_shared and isinstance(result, Mapping) and _method_inventory(result):
                # One Checkout answers every method that shares its contract, so
                # derive those rows now -- before the first-eligible stop, otherwise
                # an eligible method would hide the sibling channels the same
                # create already published.
                evidence[key] = (candidate, dict(result))
                for later in candidates:
                    if later == candidate or later in derived_methods:
                        continue
                    if _contract_key(later, resolved_country) != key:
                        continue
                    sibling = _derive_row(later, result, candidate)
                    if sibling is not None:
                        rows.append(sibling)
                        derived_methods.add(later)
        if last_eligible_only and stop:
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
        # ``status_code`` feeds the batch's "an HTTP answer proves the exit was
        # fine, so do not rotate" rule. An eligible account has nothing to
        # explain, so it reports no error (a derived ``method_unavailable`` row
        # would otherwise look like the reason the account failed).
        "status_code": 0 if state == QUALIFICATION_ELIGIBLE else _first_status(rows),
        "error": (
            ""
            if state == QUALIFICATION_ELIGIBLE
            else _scrub(_first_error(rows), str(proxy or ""), str(resolved_proxy or ""), normalized_proxy)
        ),
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
        # A country-agnostic method is billed *into the exit country* (see
        # ``_probe_kwargs``), so its catalogue country must not skip it -- a JP
        # exit with no local wallet still gets a real PayPal/card answer.
        agnostic = method in COUNTRY_AGNOSTIC_METHODS
        if not agnostic and method_country and method_country != country:
            skipped.append(
                _skip_row(
                    method,
                    _REASON_COUNTRY_MISMATCH,
                    f"该方法只支持 {method_country} 出口（当前 {country}）",
                )
            )
            continue
        # An agnostic method bills the exit country's currency; when the exit
        # country is not in ``CURRENCY_MAP`` there is no valid contract to send
        # (the old code billed the catalogue default and paid for a guaranteed
        # rejection, reported as 检测失败). Report the gap instead.
        if not _contract_currency(method, country):
            skipped.append(_currency_unknown_row(method, country))
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
    explicit: str | list[str] | None,
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

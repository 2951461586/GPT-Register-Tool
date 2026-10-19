"""Stripe Checkout setup helpers shared by the ``ideal`` and ``twint`` extractors.

Why this exists
---------------
Batch 7 of ``plan-2026-09-17-protocol-payment-extractor-consolidation``: the two
files carried byte-identical ``stripe_init`` / ``stripe_elements_session_params``
/ ``payment_accept_language`` / ``saved_payment_value`` / ``random_runtime_version``
bodies. They moved here character for character; each extractor keeps a
same-named thin wrapper so call sites and patch targets are unchanged.

Every provider-specific piece is a parameter, never an import:

* ``saved_payment_value`` -- the env var is ``<PREFIX>_SAVED_PAYMENT_VALUE``;
* ``payment_accept_language`` -- the locale comes from the caller's
  ``payment_browser_locale()``; ideal and twint resolve different tables;
* ``stripe_init`` -- takes the session factory (``common/session_factory.py``),
  the user agent, the three locale callables, the Stripe API version and the
  extractor's own ``dump_http`` wrapper.

``endpoints`` and ``DEFAULT_TIMEOUT`` are imported here because they already are
the single authority (``common/endpoints.py``, ``common/timeouts.py``).

Rule 10: stdlib and ``common`` only, never ``sms_tool``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Mapping

from . import endpoints
from .timeouts import DEFAULT_TIMEOUT


def random_runtime_version(
    *,
    default: str,
    env_name: str = "PP_RUNTIME_VERSION",
    env: Mapping[str, str] | None = None,
) -> str:
    """Env override first, then the provider's pinned runtime version."""
    environ = os.environ if env is None else env
    return environ.get(env_name, "").strip() or default


def saved_payment_value(*, env_prefix: str, env: Mapping[str, str] | None = None) -> str:
    """``<PREFIX>_SAVED_PAYMENT_VALUE``, falling back to ``never``.

    The literal fallback is deliberately not the caller's default: both
    extractors hard-coded ``never`` on each side of the ``or``.
    """
    environ = os.environ if env is None else env
    return environ.get(f"{env_prefix}_SAVED_PAYMENT_VALUE", "never").strip() or "never"


def payment_accept_language(browser_locale: Callable[[], str]) -> str:
    """Accept-Language header derived from the browser locale."""
    locale = browser_locale()
    if locale.lower().startswith("en"):
        return "en-US,en;q=0.9"
    return f"{locale},{locale.split('-', 1)[0]};q=0.9,en;q=0.8"


def stripe_elements_session_params(
    ctx: Mapping[str, Any],
    *,
    elements_locale: Callable[[], str],
) -> dict[str, str]:
    """The ``elements_session_client[...]`` form body used to create a session."""
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(
            ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"
        ),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": str(ctx.get("locale") or elements_locale()),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


def stripe_init(
    cs_id: str,
    stripe_pk: str,
    proxy: str,
    *,
    new_session: Callable[[str], Any],
    user_agent: str,
    accept_language: str,
    browser_locale: Callable[[], str],
    browser_timezone: Callable[[], str],
    elements_locale: Callable[[], str],
    stripe_version: str,
    dump_http: Callable[..., None],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create the Stripe payment page's Elements session for one checkout."""
    stripe = new_session(proxy)
    stripe.headers.update({"User-Agent": user_agent, "Accept-Language": accept_language})
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": browser_locale(),
        "browser_timezone": browser_timezone(),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": elements_locale(),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": stripe_version,
    }
    url = endpoints.stripe_payment_page(cs_id, "init")
    resp = stripe.post(url, data=body, timeout=timeout)
    dump_http(resp, "stripe_init", body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"Stripe init 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json() or {}
    payload["client_stripe_js_id"] = stripe_js_id
    payload["_client_context"] = {"stripe_js_id": stripe_js_id}
    return payload


__all__ = [
    "payment_accept_language",
    "random_runtime_version",
    "saved_payment_value",
    "stripe_elements_session_params",
    "stripe_init",
]

"""Pure configuration readers split out of `pipeline.py` (2026-10-03).

No network, no session: the `upi.*` / `UPI_*` switches the UPI flow reads, plus
the two pure predicates the Increment 3 wrapper needs.  Moved verbatim;
`pipeline` re-exports every name so `pipeline.<name>` keeps resolving.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config import _load_json, _method_cfg
from .constants import DEFAULT_CONFIG_PATH
from .env import _env_bool, _env_int, _env_str


def _upi_link_unverified_contract(verification_code: str, verification: str) -> dict[str, Any]:
    """Failure fields for a link the verify gate refused to deliver.

    The gate can refuse for three different reasons and they must not collapse
    into one code:

    * ``mandate_not_signed`` -- SetupIntent stayed at ``requires_payment_method``,
      i.e. the UPI AutoPay mandate was never signed. On the ``cs_`` rail this is
      the measured norm (Stripe refuses third-party confirmation of a SetupIntent
      Checkout created; see ``constants.UPI_LOCAL_MANDATE_ENABLED``), so a rebuilt
      Checkout may land on the CPMT / OAICS rail -- mark it retryable and never
      call it a fake link.
    * ``payment_chain`` -- the intent passed but its URI addresses the ₹1999
      payment chain. That really is the wrong artifact.
    * anything else (``canceled`` / unknown / ``already_authorized``) -- terminal.
    """
    if verification_code == "mandate_not_signed":
        return {
            "error": f"UPI AutoPay 委托未签署（{verification}）；不是废链，是 mandate 未成立",
            "error_code": "mandate_not_signed",
            "error_stage": "mandate",
            "retryable": True,
        }
    if verification_code == "payment_chain":
        return {
            "error": f"UPI link 指向 ₹1999 付款链而非 ₹0 委托（{verification}）",
            "error_code": "payment_chain_link",
            "error_stage": "artifact",
            "retryable": False,
        }
    return {
        "error": f"UPI link 未通过核验（{verification}）",
        "error_code": "link_unverified",
        "error_stage": "artifact",
        "retryable": False,
    }


def _upi_repeat_tax_region(upi_cfg: Any) -> bool:
    """``upi.repeat_tax_region`` / ``UPI_REPEAT_TAX_REGION`` (default on).

    The reference measured (2026-09-21) that the ``init -> tax -> init -> tax``
    order before confirm is what makes approve pass reliably; the second pass is
    what this switch adds.
    """
    if isinstance(upi_cfg, Mapping) and "repeat_tax_region" in upi_cfg:
        return bool(upi_cfg.get("repeat_tax_region"))
    return _env_bool("UPI_REPEAT_TAX_REGION", True)


_UPI_PRECONFIRM_RETRY_CODES: frozenset[str] = frozenset(
    {
        "no_free_trial",
        "upi_not_available",
        "checkout_failed",
        "checkout_bad_response",
        "upi_checkout_not_active",
        "oaics_prerequisites_missing",
        "oaics_elements_failed",
        # P1-1: a known-bad 0-元 exit is refused *before* a Checkout exists, so
        # reopening on a fresh exit is exactly what the round loop is for.
        "zero_cache_bad_exit",
        # P1-3: the assigned exit is still inside a risk-block window. Retryable
        # so a caller that owns a pool can hand a different exit.
        "exit_health_blocked",
    }
)


def _upi_rounds(runtime_config: Any) -> int:
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
    upi_cfg = _method_cfg(cfg, "upi")
    raw = upi_cfg.get("rounds") if isinstance(upi_cfg, Mapping) else None
    if raw is None:
        return max(1, _env_int("UPI_ROUNDS", 1, 1))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _upi_round_retryable(result: Any) -> bool:
    if not isinstance(result, Mapping) or result.get("ok"):
        return False
    return str(result.get("error_code") or "") in _UPI_PRECONFIRM_RETRY_CODES


def _upi_zero_mode(upi_cfg: Any) -> str:
    """``upi.zero_mode`` / ``UPI_ZERO_MODE`` -- how a Checkout is made to bill 0.

    ``campaign`` (default) keeps the historical behaviour: only the
    ``plus-1-month-free`` promo campaign is injected.  ``trial`` adds a real
    ``subscription_data.trial_period_days``; ``momo`` also mirrors the MoMologic
    flow (trial + full billing profile).  Reference: ``upi_extract.py:3732
    zero_mode``.
    """
    if isinstance(upi_cfg, Mapping) and upi_cfg.get("zero_mode"):
        value = str(upi_cfg.get("zero_mode") or "").strip().lower()
    else:
        value = _env_str("UPI_ZERO_MODE", "campaign").strip().lower()
    return value if value in {"campaign", "trial", "momo"} else "campaign"


def _upi_trial_period_days() -> int:
    """``UPI_TRIAL_DAYS`` (default 30, clamped to 1..90)."""
    return max(1, min(90, _env_int("UPI_TRIAL_DAYS", 30, minimum=1)))


def _upi_zero_eligibility_precheck(upi_cfg: Any) -> bool:
    """``upi.zero_eligibility_precheck`` / ``UPI_ZERO_ELIGIBILITY_PRECHECK``."""
    if isinstance(upi_cfg, Mapping) and "zero_eligibility_precheck" in upi_cfg:
        return bool(upi_cfg.get("zero_eligibility_precheck"))
    return _env_bool("UPI_ZERO_ELIGIBILITY_PRECHECK", True)


def _upi_zero_negative_cache(upi_cfg: Any) -> bool:
    """``upi.zero_negative_cache`` / ``UPI_ZERO_NEGATIVE_CACHE``."""
    if isinstance(upi_cfg, Mapping) and "zero_negative_cache" in upi_cfg:
        return bool(upi_cfg.get("zero_negative_cache"))
    return _env_bool("UPI_ZERO_NEGATIVE_CACHE", True)


def _upi_zero_cache_scheduling(upi_cfg: Any) -> bool:
    """``upi.zero_cache_scheduling`` / ``UPI_ZERO_CACHE_SCHEDULING``.

    **Opt-in, default off** -- same default as the reference
    (``upi_extract.py:1955 zero_cache_scheduling_enabled``).  The cache is read
    and reported on every run either way; only *acting* on it needs the switch,
    because a 30-minute-old sample can be wrong about a rotating residential
    exit and skipping on it would waste a good exit.
    """
    if isinstance(upi_cfg, Mapping) and "zero_cache_scheduling" in upi_cfg:
        return bool(upi_cfg.get("zero_cache_scheduling"))
    return _env_bool("UPI_ZERO_CACHE_SCHEDULING", False)


def _upi_zero_cache_skip_bad(upi_cfg: Any) -> bool:
    """``upi.zero_cache_skip_bad`` / ``UPI_ZERO_CACHE_SKIP_BAD`` (default on).

    Only consulted when :func:`_upi_zero_cache_scheduling` is enabled: then an
    exit whose last sample produced a non-zero amount for this country is
    refused before a Checkout is created.
    """
    if isinstance(upi_cfg, Mapping) and "zero_cache_skip_bad" in upi_cfg:
        return bool(upi_cfg.get("zero_cache_skip_bad"))
    return _env_bool("UPI_ZERO_CACHE_SKIP_BAD", True)

"""Typed contract for the failure attributes carried on payment exceptions.

The protocol-payment chain reports failures through five optional attributes
attached to the exception object instead of through a common exception class:
``error_code``, ``error_stage``, ``retryable``, ``outcome_unknown`` and
``requires_reconciliation``.

Writers used plain attribute assignment on arbitrary exception types and
readers used ``getattr(exc, "...", default)``.  Both sides were stringly typed,
so a misspelled attribute name silently degraded to the default at the read
site — the failure mode this module removes:

* writers go through :func:`annotate_error`, which rejects unknown attribute
  names before it touches the exception;
* readers go through :func:`error_code_of`, :func:`error_stage_of`,
  :func:`retryable_of` or :func:`retryable_flag`, so the name and the default
  semantics live in exactly one place.

:class:`PaymentErrorInfo` documents the shape for type checkers and for the
exception classes that already declare these attributes in ``__init__``
(``checkout_contract``, ``payment_capability``, ``payment_egress``,
``wallet_provider``, ``regional_payment_adapter``, ``gcash_provider``,
``paypal_extract``, ``nexsms``, ``services/protocol-payment/direct_card``).

The readers deliberately preserve the historical default semantics rather than
normalising them: ``*_of`` uses ``or default`` (an empty string falls back),
while :func:`retryable_of` returns the raw value with no coercion so callers
that want tolerant parsing keep using their own ``_as_bool``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Attributes that make up the payment-failure contract.
PAYMENT_ERROR_ATTRS: frozenset[str] = frozenset(
    {
        "error_code",
        "error_stage",
        "retryable",
        "outcome_unknown",
        "requires_reconciliation",
    }
)


@runtime_checkable
class PaymentErrorInfo(Protocol):
    """Exception shape consumed by the payment classifiers and normalisers."""

    error_code: str
    error_stage: str
    retryable: bool


def annotate_error(exc: BaseException, **attributes: Any) -> BaseException:
    """Attach payment-failure attributes to ``exc`` and return it.

    ``None`` values are skipped, matching the call sites that only know some of
    the fields.  Assignment is best effort: an exception type may forbid
    attributes, and callers treat annotation as optional, so a refusal must not
    mask the original failure.
    """
    unknown = sorted(set(attributes) - PAYMENT_ERROR_ATTRS)
    if unknown:
        raise ValueError(
            "unknown payment error attribute(s): " + ", ".join(unknown)
        )
    for name, value in attributes.items():
        if value is None:
            continue
        try:
            setattr(exc, name, value)
        except (AttributeError, TypeError):
            continue
    return exc


def error_code_of(exc: BaseException, default: str = "") -> str:
    """Return the error code, falling back to ``default`` when absent or empty."""
    return str(getattr(exc, "error_code", "") or default)


def error_stage_of(exc: BaseException, default: str = "") -> str:
    """Return the error stage, falling back to ``default`` when absent or empty."""
    return str(getattr(exc, "error_stage", "") or default)


def retryable_of(exc: BaseException) -> Any:
    """Return the raw ``retryable`` flag — ``None`` when the exception carries none.

    The value is not coerced: callers that accept the loose truthy spellings
    (``"false"``, ``0``, ``"no"``) keep applying their own ``_as_bool``.
    """
    return getattr(exc, "retryable", None)


def retryable_flag(exc: BaseException, default: bool = False) -> bool:
    """Return ``retryable`` coerced to ``bool``, or ``default`` when absent."""
    return bool(getattr(exc, "retryable", default))


def outcome_unknown_of(exc: BaseException) -> Any:
    """Return the raw ``outcome_unknown`` flag — ``None`` when absent."""
    return getattr(exc, "outcome_unknown", None)


__all__ = [
    "PAYMENT_ERROR_ATTRS",
    "PaymentErrorInfo",
    "annotate_error",
    "error_code_of",
    "error_stage_of",
    "outcome_unknown_of",
    "retryable_flag",
    "retryable_of",
]

"""Contract tests for ``sms_tool/payment_errors.py``.

Why this file exists
--------------------
The protocol-payment chain reports failures through five optional attributes
attached to the exception object (``error_code``, ``error_stage``,
``retryable``, ``outcome_unknown``, ``requires_reconciliation``) instead of a
common exception class.  Before this module existed, writers did bare
``exc.error_stage = "..."`` and readers did ``getattr(exc, "...", default)`` on
both sides, so **a misspelled attribute name degraded silently to the default**
at the read site instead of failing.

The tests below pin the two properties that make the contract enforceable:

* :func:`annotate_error` rejects an unknown attribute name, so a typo fails at
  the writer instead of disappearing at the reader;
* the readers own the default semantics in exactly one place, so a call site
  cannot drift from the rest of the chain.

Everything here is pure: no network, no browser, no money, no SQLite.
"""

from __future__ import annotations

import pytest

from sms_tool.payment_errors import (
    PAYMENT_ERROR_ATTRS,
    PaymentErrorInfo,
    annotate_error,
    error_code_of,
    error_stage_of,
    outcome_unknown_of,
    retryable_flag,
    retryable_of,
)


# --------------------------------------------------------------------------
# annotate_error — the writer side
# --------------------------------------------------------------------------


def test_annotate_error_sets_the_declared_fields_and_returns_the_same_object():
    exc = RuntimeError("regional transport missing")

    returned = annotate_error(
        exc,
        error_code="regional_transport_unconfigured",
        error_stage="adapter_setup",
        retryable=False,
    )

    assert returned is exc, "annotate_error must return the exception for `raise`"
    # Assert against the instance __dict__: this proves the attribute was
    # actually installed, rather than read back through the same reader that
    # would happily return its default.
    assert vars(exc)["error_code"] == "regional_transport_unconfigured"
    assert vars(exc)["error_stage"] == "adapter_setup"
    # `False` is a real value, not an absent one — it must survive.
    assert vars(exc)["retryable"] is False


def test_annotate_error_skips_none_so_a_partial_call_site_does_not_fake_a_field():
    exc = ValueError("validation failed")

    annotate_error(exc, error_code=None, error_stage="validation")

    assert vars(exc)["error_stage"] == "validation"
    assert "error_code" not in vars(exc), "None must not create the attribute"


def test_annotate_error_rejects_an_unknown_attribute_name():
    """The typo guard: this is the whole point of routing writers through here."""
    exc = RuntimeError("boom")

    with pytest.raises(ValueError) as caught:
        annotate_error(exc, error_stege="validation")  # transposed on purpose

    assert "error_stege" in str(caught.value)
    assert not hasattr(exc, "error_stege")


def test_annotate_error_reports_every_unknown_name_sorted():
    with pytest.raises(ValueError) as caught:
        annotate_error(RuntimeError("boom"), zzz=1, aaa=2)

    assert "aaa, zzz" in str(caught.value)


def test_annotate_error_accepts_every_attribute_in_the_contract():
    exc = RuntimeError("boom")

    annotate_error(
        exc,
        error_code="c",
        error_stage="s",
        retryable=True,
        outcome_unknown=True,
        requires_reconciliation=True,
    )

    assert PAYMENT_ERROR_ATTRS == {
        "error_code",
        "error_stage",
        "retryable",
        "outcome_unknown",
        "requires_reconciliation",
    }


def test_annotate_error_is_best_effort_when_the_exception_forbids_attributes():
    """A hostile exception type must not mask the original failure.

    Note ``__slots__`` cannot seal a ``BaseException`` subclass — ``BaseException``
    already provides ``__dict__`` — so the guard is exercised with a type that
    actually refuses assignment.
    """

    class Sealed(Exception):
        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError(f"{name} is read-only")

    exc = Sealed("sealed")

    annotate_error(exc, error_stage="validation")  # must not raise

    assert error_stage_of(exc, "fallback") == "fallback"


# --------------------------------------------------------------------------
# readers — the reader side owns the defaults
# --------------------------------------------------------------------------


def test_error_code_of_falls_back_when_absent_or_empty():
    assert error_code_of(RuntimeError("boom")) == ""
    assert error_code_of(RuntimeError("boom"), "adapter") == "adapter"

    empty = annotate_error(RuntimeError("boom"), error_code="")
    assert error_code_of(empty, "adapter") == "adapter", "empty is treated as absent"


def test_error_code_of_coerces_a_non_string_value():
    exc = annotate_error(RuntimeError("boom"), error_code=418)

    assert error_code_of(exc) == "418"


def test_error_stage_of_falls_back_when_absent_or_empty():
    assert error_stage_of(RuntimeError("boom")) == ""
    assert error_stage_of(RuntimeError("boom"), "proxy_setup") == "proxy_setup"

    empty = annotate_error(RuntimeError("boom"), error_stage="")
    assert error_stage_of(empty, "proxy_setup") == "proxy_setup"


def test_retryable_of_returns_the_raw_value_without_coercion():
    """Callers keep their own tolerant parser; a coercing reader would break them."""
    assert retryable_of(RuntimeError("boom")) is None

    loose = annotate_error(RuntimeError("boom"), retryable="false")
    assert retryable_of(loose) == "false", "raw string must survive for _as_bool"

    strict = annotate_error(RuntimeError("boom"), retryable=False)
    assert retryable_of(strict) is False


def test_retryable_flag_coerces_and_honours_the_default():
    assert retryable_flag(RuntimeError("boom")) is False
    assert retryable_flag(RuntimeError("boom"), True) is True

    exc = annotate_error(RuntimeError("boom"), retryable=0)
    assert retryable_flag(exc, True) is False, "a present falsy value outranks default"

    loose = annotate_error(RuntimeError("boom"), retryable="false")
    assert retryable_flag(loose) is True, "documented bool() coercion, not _as_bool"


def test_outcome_unknown_of_returns_the_raw_flag():
    assert outcome_unknown_of(RuntimeError("boom")) is None

    exc = annotate_error(RuntimeError("boom"), outcome_unknown=True)
    assert outcome_unknown_of(exc) is True


# --------------------------------------------------------------------------
# the contract as a type
# --------------------------------------------------------------------------


def test_payment_error_info_matches_a_class_that_declares_the_fields():
    """The classes that set the fields in __init__ satisfy the protocol."""

    class Declared(RuntimeError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.error_code = "declared"
            self.error_stage = "adapter"
            self.retryable = True

    assert isinstance(Declared("boom"), PaymentErrorInfo)


def test_payment_error_info_rejects_a_bare_exception():
    assert not isinstance(RuntimeError("boom"), PaymentErrorInfo)


# --------------------------------------------------------------------------
# the call site this module was extracted from (pay_link/core.py)
# --------------------------------------------------------------------------


def test_planning_error_annotation_round_trips_like_the_call_site():
    """Mirror of ``pay_link/core.py``: annotate only when the stage is unknown."""
    exc = TypeError("bad option")

    if not error_stage_of(exc):
        annotate_error(
            exc,
            error_stage=("validation" if isinstance(exc, (ValueError, TypeError)) else "proxy_setup"),
        )

    assert error_stage_of(exc) == "validation"

    # A second pass must not overwrite a stage an inner layer already set.
    if not error_stage_of(exc):
        annotate_error(exc, error_stage="proxy_setup")

    assert error_stage_of(exc) == "validation"

"""Behaviour tests for ``sms_tool/pay_link/normalize.py`` (2026-09-03, round 7).

Why this file exists
--------------------
``pay_link/normalize.py`` (248 lines) is the **terminal-state and retry
decision layer** for the whole payment-link path, and the AST audit
(``zero-test-module-behavior-tests/scripts/coverage_audit.py``) reported
**zero direct calls** from the suite -- it only ever showed up as a patch
target.

That is the worst possible place to be blind, because two of its decisions
are exactly the ones that cost real money when they are wrong:

* ``cancelled`` / ``unknown`` must be **not retryable** -- retrying an
  outcome-unknown payment is how you double-charge somebody.
* ``timed_out`` must be **retryable** -- otherwise a network blip gets booked
  as a dead order.

Every function under test is pure: no network, no browser, no real money, no
SQLite. Per the playbook we assert the **contract** (return values), not
implementation details, and production code is untouched in this round.

⚠️ Several behaviours below are counter-intuitive but deliberate, and are
pinned as-is rather than "fixed":

* ``_as_bool`` returns ``None`` (not ``False``) for anything it does not
  recognise, so ``outcome_unknown="maybe"`` does **not** trigger the unknown
  state. An unparseable flag is treated as absent.
* ``_explicit_terminal_state`` scans ``status`` / ``state`` / ``error_code``
  alike, so an adapter that reports ``error_code="timeout"`` is classified as
  ``timed_out`` even with no ``status`` key at all.
* ``error_stage`` is **overwritten** (not ``setdefault``), and falls back to a
  default even when the adapter passed an empty string.

Two inconsistencies found while writing these tests are **pinned, not fixed**
(see their docstrings) because changing them is a behaviour change:

* ``_classify_exception`` does not treat ``KeyboardInterrupt`` as
  ``cancelled``, although ``_canonical_terminal_state`` does.
* ``_classify_exception``'s mro scan is a substring test for ``"timeout"``, so
  a class named ``ExtractorTimedOut`` is not recognised as ``timed_out``.
"""
from __future__ import annotations

import asyncio
import subprocess
import unittest

from sms_tool.pay_link.base import PaymentMethodSpec
from sms_tool.pay_link.normalize import (
    _canonical_terminal_state,
    _classify_exception,
    _explicit_terminal_state,
    _is_retryable_failure,
    _normalize_error_contract,
    _normalize_result,
    _normalized_contract_value,
    _result_terminal_state,
)


def _spec(
    key: str = "upi",
    label: str = "UPI",
    country: str = "IN",
    currency: str = "INR",
    adapter: str = "upi",
    validator: str = "http_url",
) -> PaymentMethodSpec:
    return PaymentMethodSpec(
        key=key,
        label=label,
        country=country,
        currency=currency,
        adapter=adapter,
        artifact_validator=validator,
    )


class NormalizedContractValueTests(unittest.TestCase):
    def test_non_strings_collapse_to_empty(self):
        for value in (None, 0, 1, True, {}, [], object()):
            with self.subTest(value=repr(value)):
                self.assertEqual(_normalized_contract_value(value), "")

    def test_separators_become_single_underscore(self):
        cases = {
            "Timed Out": "timed_out",
            "payment-outcome-unknown": "payment_outcome_unknown",
            "  CANCELLED  ": "cancelled",
            "requires action": "requires_action",
            "HTTP 429": "http_429",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalized_contract_value(raw), expected)

    def test_punctuation_only_value_collapses_to_empty(self):
        """A value with no alphanumerics normalises to '' -- not to '_'."""
        self.assertEqual(_normalized_contract_value("!!! --- ???"), "")

    def test_leading_and_trailing_underscores_are_stripped(self):
        self.assertEqual(_normalized_contract_value("__cancelled__"), "cancelled")


class CanonicalTerminalStateTests(unittest.TestCase):
    def test_cancelled_aliases(self):
        for raw in ("cancelled", "canceled", "Cancelled_By_User", "interrupted",
                    "KeyboardInterrupt", "Keyboard-Interrupt"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_terminal_state(raw), "cancelled")

    def test_cancelled_suffix(self):
        for raw in ("extraction_cancelled", "user_canceled"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_terminal_state(raw), "cancelled")

    def test_timed_out_aliases_and_suffix(self):
        for raw in ("timed_out", "timeout", "timeout_expired",
                    "extractor_timeout", "browser_timed_out"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_terminal_state(raw), "timed_out")

    def test_unknown_aliases_and_suffix(self):
        for raw in ("unknown", "outcome_unknown", "payment_outcome_unknown",
                    "indeterminate", "inconclusive", "link_outcome_unknown"):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_terminal_state(raw), "unknown")

    def test_success_like_statuses_are_not_terminal(self):
        for raw in ("completed", "success", "pending", "processing", "created", ""):
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_terminal_state(raw), "")

    def test_non_strings_are_not_terminal(self):
        self.assertEqual(_canonical_terminal_state(None), "")
        self.assertEqual(_canonical_terminal_state(124), "")

    def test_cancelled_wins_over_unknown_when_both_present_in_one_value(self):
        """"cancelled_unknown" ends with neither suffix; cancelled set is checked first."""
        # "cancelled_unknown" is not in any literal set and matches no suffix,
        # so it must fall through to '' -- the sets are not substring-matched.
        self.assertEqual(_canonical_terminal_state("cancelled_unknown"), "")


class ExplicitTerminalStateTests(unittest.TestCase):
    def test_outcome_unknown_flag_wins_over_everything(self):
        data = {"outcome_unknown": True, "status": "cancelled"}
        self.assertEqual(_explicit_terminal_state(data), "unknown")

    def test_requires_reconciliation_flag_also_means_unknown(self):
        self.assertEqual(_explicit_terminal_state({"requires_reconciliation": True}), "unknown")

    def test_unrecognised_flag_value_does_not_trigger_unknown(self):
        """⚠️ `_as_bool` returns None for 'maybe', so the flag is treated as absent."""
        self.assertEqual(_explicit_terminal_state({"outcome_unknown": "maybe"}), "")

    def test_false_flag_does_not_trigger_unknown(self):
        self.assertEqual(_explicit_terminal_state({"outcome_unknown": False}), "")

    def test_key_precedence_terminal_state_before_status(self):
        data = {"terminal_state": "timeout", "status": "cancelled"}
        self.assertEqual(_explicit_terminal_state(data), "timed_out")

    def test_error_code_is_scanned_too(self):
        """An adapter reporting only error_code='timeout' is still timed_out."""
        self.assertEqual(_explicit_terminal_state({"error_code": "timeout"}), "timed_out")

    def test_decision_key_is_scanned(self):
        self.assertEqual(_explicit_terminal_state({"decision": "cancelled"}), "cancelled")

    def test_exit_code_124_is_timed_out(self):
        self.assertEqual(_explicit_terminal_state({"exit_code": 124}), "timed_out")

    def test_exit_code_124_as_string_is_timed_out(self):
        self.assertEqual(_explicit_terminal_state({"exit_code": "124"}), "timed_out")

    def test_exit_codes_meaning_cancelled(self):
        for code in (-2, 130, -1073741510, 3221225786):
            with self.subTest(code=code):
                self.assertEqual(_explicit_terminal_state({"exit_code": code}), "cancelled")

    def test_unparseable_exit_code_is_ignored(self):
        self.assertEqual(_explicit_terminal_state({"exit_code": "not-a-number"}), "")

    def test_pending_without_artifact_is_unknown(self):
        for status in ("pending", "processing", "submitted",
                       "requires_action", "awaiting_confirmation"):
            with self.subTest(status=status):
                self.assertEqual(_explicit_terminal_state({"status": status}), "unknown")

    def test_pending_with_artifact_is_not_unknown(self):
        """A pending result that already produced a link is not outcome-unknown."""
        self.assertEqual(
            _explicit_terminal_state({"status": "pending", "url": "https://x.test/pay"}),
            "",
        )

    def test_ok_result_is_never_unknown(self):
        self.assertEqual(
            _explicit_terminal_state({"ok": True, "status": "processing"}),
            "",
        )

    def test_plain_failure_is_not_terminal(self):
        self.assertEqual(_explicit_terminal_state({"ok": False}), "")


class IsRetryableFailureTests(unittest.TestCase):
    def test_429_and_5xx_are_retryable(self):
        for code in (429, 500, 502, 503, 599):
            with self.subTest(code=code):
                self.assertTrue(_is_retryable_failure({"status_code": code}))

    def test_http_status_is_used_when_status_code_absent(self):
        self.assertTrue(_is_retryable_failure({"http_status": 503}))

    def test_4xx_other_than_429_is_not_retryable(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                self.assertFalse(_is_retryable_failure({"status_code": code}))

    def test_retryable_error_codes(self):
        for code in ("connection_error", "connect_timeout", "read_timeout",
                     "network_error", "proxy_error", "proxy_unavailable",
                     "rate_limited", "service_unavailable"):
            with self.subTest(code=code):
                self.assertTrue(_is_retryable_failure({"error_code": code}))

    def test_error_type_is_consulted_when_error_code_absent(self):
        self.assertTrue(_is_retryable_failure({"error_type": "Network Error"}))

    def test_unknown_error_code_is_not_retryable(self):
        self.assertFalse(_is_retryable_failure({"error_code": "invalid_adapter_result"}))

    def test_empty_payload_is_not_retryable(self):
        self.assertFalse(_is_retryable_failure({}))


class NormalizeErrorContractTests(unittest.TestCase):
    """The money-critical half: who is allowed to be retried."""

    def test_success_clears_retryable_and_error_stage(self):
        data = {"ok": True, "retryable": True, "error_stage": "adapter"}
        _normalize_error_contract(data)
        self.assertFalse(data["retryable"])
        self.assertEqual(data["error_stage"], "")

    def test_cancelled_is_never_retryable(self):
        data = {"ok": False, "status": "cancelled", "retryable": True}
        _normalize_error_contract(data)
        self.assertFalse(data["retryable"], "retrying a cancelled payment is a double-charge risk")

    def test_unknown_is_never_retryable(self):
        data = {"ok": False, "status": "unknown", "retryable": True}
        _normalize_error_contract(data)
        self.assertFalse(data["retryable"], "outcome unknown must never be retried blind")

    def test_requires_reconciliation_is_never_retryable(self):
        data = {"ok": False, "requires_reconciliation": True, "retryable": True}
        _normalize_error_contract(data)
        self.assertFalse(data["retryable"])

    def test_timed_out_is_retryable(self):
        data = {"ok": False, "status": "timeout"}
        _normalize_error_contract(data)
        self.assertTrue(data["retryable"], "a network blip must not be booked as a dead order")

    def test_explicit_retryable_wins_for_plain_failure(self):
        """A 503 would retry by default, but an explicit retryable=False must win."""
        data = {"ok": False, "retryable": False, "status_code": 503}
        _normalize_error_contract(data)
        self.assertFalse(data["retryable"])

    def test_retry_safe_is_the_second_spelling_of_retryable(self):
        data = {"ok": False, "retry_safe": True}
        _normalize_error_contract(data)
        self.assertTrue(data["retryable"])

    def test_retryable_string_yes_is_understood(self):
        data = {"ok": False, "retryable": "yes"}
        _normalize_error_contract(data)
        self.assertTrue(data["retryable"])

    def test_defaults_are_filled_in(self):
        data: dict = {"ok": False}
        _normalize_error_contract(data)
        self.assertEqual(data["error"], "payment-link extraction failed")
        self.assertEqual(data["error_code"], "payment_link_extraction_failed")

    def test_invalid_adapter_result_gets_the_contract_stage(self):
        data = {"ok": False, "error_code": "invalid_adapter_result"}
        _normalize_error_contract(data)
        self.assertEqual(data["error_stage"], "adapter_contract")

    def test_eligibility_codes_get_the_eligibility_stage(self):
        for code in ("checkout_not_zero_due", "nonzero_offer",
                     "paypal_payment_method_unavailable"):
            with self.subTest(code=code):
                data = {"ok": False, "error_code": code}
                _normalize_error_contract(data)
                self.assertEqual(data["error_stage"], "eligibility")

    def test_plain_failure_defaults_to_the_adapter_stage(self):
        data = {"ok": False, "error_code": "payment_link_extraction_failed"}
        _normalize_error_contract(data)
        self.assertEqual(data["error_stage"], "adapter")

    def test_explicit_stage_is_kept(self):
        data = {"ok": False, "error_stage": "normalization"}
        _normalize_error_contract(data)
        self.assertEqual(data["error_stage"], "normalization")

    def test_failed_step_is_an_accepted_stage_source(self):
        data = {"ok": False, "failed_step": "extracting"}
        _normalize_error_contract(data)
        self.assertEqual(data["error_stage"], "extracting")

    def test_blank_stage_falls_back_to_the_default(self):
        """⚠️ error_stage is overwritten, not setdefault -- an empty string does not stick."""
        data = {"ok": False, "error_stage": "   "}
        _normalize_error_contract(data)
        self.assertEqual(data["error_stage"], "adapter")


class NormalizeResultTests(unittest.TestCase):
    def test_success_is_decorated_with_spec_defaults(self):
        data = _normalize_result(_spec(), {"ok": True, "url": "https://x.test/pay"})
        self.assertTrue(data["ok"])
        self.assertEqual(data["payment_method"], "upi")
        self.assertEqual(data["method"], "upi")
        self.assertEqual(data["target_country"], "IN")
        self.assertEqual(data["currency"], "INR")
        self.assertEqual(data["link_type"], "upi_protocol")
        self.assertEqual(data["operation"], "extract_link")
        self.assertFalse(data["retryable"])
        self.assertEqual(data["error_stage"], "")

    def test_non_dict_result_becomes_an_invalid_contract_failure(self):
        data = _normalize_result(_spec(), "boom")
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "boom")
        self.assertEqual(data["error_code"], "invalid_adapter_result")
        self.assertEqual(data["error_stage"], "adapter_contract")

    def test_empty_dict_matches_the_non_dict_contract(self):
        """An adapter returning {} is the same failure as returning garbage.

        Only the human-readable ``error`` differs: ``{}`` gets the generic
        "invalid result contract" text, while a non-dict is stringified. Every
        machine-readable field is identical -- that is what callers branch on.
        """
        empty = _normalize_result(_spec(), {})
        garbage = _normalize_result(_spec(), "boom")
        for key in ("ok", "error_code", "error_stage", "retryable", "operation", "url"):
            with self.subTest(key=key):
                self.assertEqual(empty[key], garbage[key])
        self.assertEqual(empty["error"], "UPI extractor returned an invalid result contract")
        self.assertEqual(garbage["error"], "boom")

    def test_caller_dict_is_not_mutated(self):
        payload = {"ok": True, "url": "https://x.test/pay"}
        snapshot = dict(payload)
        _normalize_result(_spec(), payload)
        self.assertEqual(payload, snapshot)

    def test_url_fallback_chain(self):
        for key in ("long_url", "provider_redirect_url", "checkout_url", "upi_uri"):
            with self.subTest(key=key):
                data = _normalize_result(_spec(), {"ok": True, key: "https://x.test/pay"})
                self.assertEqual(data["url"], "https://x.test/pay")

    def test_url_is_empty_when_no_source_key_exists(self):
        """⚠️ url is assigned, not setdefault -- it is always present, possibly ''."""
        data = _normalize_result(_spec(), {"ok": True, "qr_data": "QR"})
        self.assertEqual(data["url"], "")

    def test_non_http_url_fails_the_http_validator(self):
        data = _normalize_result(_spec(), {"ok": True, "url": "ftp://x.test/pay"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_code"], "adapter_result_missing_artifact")
        self.assertEqual(data["error_stage"], "normalization")

    def test_qr_only_result_passes_the_url_or_qr_validator(self):
        spec = _spec(validator="url_or_qr")
        data = _normalize_result(spec, {"ok": True, "qr_data": "QR-BLOB"})
        self.assertTrue(data["ok"], f"unexpected failure: {data}")

    def test_completion_validator_needs_status_completed(self):
        spec = _spec(validator="completion")
        self.assertFalse(_normalize_result(spec, {"ok": True, "url": "https://x.test"})["ok"])
        self.assertTrue(_normalize_result(spec, {"ok": True, "status": "completed"})["ok"])

    def test_capability_probe_needs_no_artifact(self):
        """A capability probe produces no link by design; it must not fail for that."""
        data = _normalize_result(
            _spec(),
            {"ok": True, "operation": "payment_method_capability_probe"},
        )
        self.assertTrue(data["ok"], f"unexpected failure: {data}")

    def test_blik_completed_payment_needs_no_artifact(self):
        data = _normalize_result(
            _spec(key="blik", label="BLIK", country="PL", currency="PLN"),
            {
                "ok": True,
                "status": "completed",
                "operation": "execute_payment",
                "link_type": "blik_protocol_completed",
            },
        )
        self.assertTrue(data["ok"], f"unexpected failure: {data}")

    def test_blik_completed_matches_all_four_conditions(self):
        """Drop any one of the four and the artifact rule applies again."""
        spec = _spec(key="blik", label="BLIK", country="PL", currency="PLN")
        payload = {
            "ok": True,
            "status": "completed",
            "operation": "execute_payment",
            "link_type": "blik_protocol_completed",
        }
        for key, replacement in (("status", "pending"),
                                 ("operation", "extract_link"),
                                 ("link_type", "blik_protocol")):
            with self.subTest(key=key):
                broken = dict(payload, **{key: replacement})
                self.assertFalse(_normalize_result(spec, broken)["ok"])

    def test_blik_completed_only_applies_to_the_blik_key(self):
        """The same payload under a non-blik key is an ordinary missing-artifact failure."""
        payload = {
            "ok": True,
            "status": "completed",
            "operation": "execute_payment",
            "link_type": "blik_protocol_completed",
        }
        self.assertFalse(_normalize_result(_spec(), payload)["ok"])

    def test_cancelled_result(self):
        data = _normalize_result(_spec(), {"status": "cancelled"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["error_code"], "payment_link_cancelled")
        self.assertFalse(data["retryable"])

    def test_timed_out_result(self):
        data = _normalize_result(_spec(), {"status": "timeout"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "timed_out")
        self.assertEqual(data["error_code"], "payment_link_timed_out")
        self.assertTrue(data["retryable"])

    def test_unknown_result_requires_reconciliation(self):
        data = _normalize_result(_spec(), {"ok": False, "status": "pending"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "unknown")
        self.assertEqual(data["error_code"], "payment_outcome_unknown")
        self.assertTrue(data["requires_reconciliation"])
        self.assertFalse(data["retryable"])

    def test_result_with_artifact_but_no_ok_key_is_a_failure(self):
        """A result that never says "ok" is a failure even when it carries a link."""
        data = _normalize_result(_spec(), {"url": "https://x.test/pay"})
        self.assertFalse(data["ok"], f"unexpected success: {data}")
        self.assertEqual(data["error_code"], "invalid_adapter_result")

    def test_terminal_state_rewrites_a_generic_extraction_failure_code(self):
        """The override at the end only fires when the code is still the generic one.

        An adapter that reports both a terminal status and the catch-all
        ``payment_link_extraction_failed`` must get the specific code, because
        callers branch on ``error_code`` to decide whether to reconcile.
        """
        for payload, expected in (
            ({"status": "cancelled", "error_code": "payment_link_extraction_failed"},
             "payment_link_cancelled"),
            ({"status": "timeout", "error_code": "payment_link_extraction_failed"},
             "payment_link_timed_out"),
        ):
            with self.subTest(payload=payload):
                data = _normalize_result(_spec(), dict(payload))
                self.assertEqual(data["error_code"], expected)

    def test_explicit_terminal_overrides_the_generic_extraction_error(self):
        """A terminal state must not keep the generic 'extraction failed' code."""
        for raw, expected in (("cancelled", "payment_link_cancelled"),
                              ("timeout", "payment_link_timed_out")):
            with self.subTest(raw=raw):
                data = _normalize_result(_spec(), {"exit_code": 124 if raw == "timeout" else 130})
                self.assertEqual(data["error_code"], expected)


class ResultTerminalStateTests(unittest.TestCase):
    def test_completed_when_ok(self):
        self.assertEqual(_result_terminal_state({"ok": True}), "completed")

    def test_explicit_terminal_is_surfaced(self):
        self.assertEqual(_result_terminal_state({"ok": False, "status": "timeout"}), "timed_out")

    def test_plain_failure_defaults_to_failed(self):
        self.assertEqual(_result_terminal_state({"ok": False}), "failed")


class ClassifyExceptionTests(unittest.TestCase):
    def test_plain_exception_is_a_retryable_false_failure(self):
        state, code, retryable = _classify_exception(RuntimeError("boom"))
        self.assertEqual((state, code, retryable), ("failed", "payment_link_manager_failed", False))

    def test_cancelled_error(self):
        state, code, retryable = _classify_exception(asyncio.CancelledError())
        self.assertEqual((state, code, retryable), ("cancelled", "payment_link_cancelled", False))

    def test_timeout_error_is_retryable(self):
        state, code, retryable = _classify_exception(TimeoutError())
        self.assertEqual((state, code, retryable), ("timed_out", "payment_link_timed_out", True))

    def test_subprocess_timeout_is_retryable(self):
        exc = subprocess.TimeoutExpired(cmd="extract.py", timeout=30)
        state, _code, retryable = _classify_exception(exc)
        self.assertEqual(state, "timed_out")
        self.assertTrue(retryable)

    def test_class_name_containing_timeout_is_recognised(self):
        class ExtractorTimeout(Exception):
            pass

        state, code, _retryable = _classify_exception(ExtractorTimeout())
        self.assertEqual((state, code), ("timed_out", "payment_link_timed_out"))

    def test_class_spelled_timed_out_is_not_recognised(self):
        """⚠️ The mro scan is a **substring** test for "timeout", nothing smarter.

        A class named ``ExtractorTimedOut`` normalises to "extractortimedout",
        which does **not** contain "timeout" -- so it is classified as a plain
        failure, even though ``_canonical_terminal_state("timed_out")`` would
        happily call it timed_out. Pinned as-is: renaming the exception class
        is the fix, not changing the matcher.
        """

        class ExtractorTimedOut(Exception):
            pass

        state, code, _retryable = _classify_exception(ExtractorTimedOut())
        self.assertEqual((state, code), ("failed", "payment_link_manager_failed"))

    def test_keyboard_interrupt_is_not_cancelled_here(self):
        """⚠️ _classify_exception and _canonical_terminal_state disagree on KeyboardInterrupt.

        ``_canonical_terminal_state("keyboardinterrupt")`` returns "cancelled",
        but ``_classify_exception`` matches the mro against a much smaller set
        (``{"cancellederror", "cancelled_error", "canceled_error"}``) that does
        not include it. So a Ctrl-C during extraction is booked as a plain
        ``failed`` rather than ``cancelled`` -- and it is therefore **not**
        given the "never retry" protection that cancelled gets.

        Pinned as-is: this is an inconsistency, not a crash, and fixing it is a
        behaviour change that belongs in its own round.
        """
        state, code, retryable = _classify_exception(KeyboardInterrupt())
        self.assertEqual((state, code, retryable), ("failed", "payment_link_manager_failed", False))

    def test_exception_status_attribute_drives_the_state(self):
        class Boom(Exception):
            status = "cancelled"

        state, code, _retryable = _classify_exception(Boom())
        self.assertEqual((state, code), ("cancelled", "payment_link_cancelled"))

    def test_terminal_state_attribute_is_the_second_source(self):
        class Boom(Exception):
            terminal_state = "timed_out"

        state, code, retryable = _classify_exception(Boom())
        self.assertEqual((state, code, retryable), ("timed_out", "payment_link_timed_out", True))

    def test_custom_error_code_is_preserved(self):
        class Boom(Exception):
            error_code = "wallet_unavailable"

        _state, code, _retryable = _classify_exception(Boom())
        self.assertEqual(code, "wallet_unavailable")

    def test_code_attribute_is_the_second_code_source(self):
        class Boom(Exception):
            code = "wallet_unavailable"

        _state, code, _retryable = _classify_exception(Boom())
        self.assertEqual(code, "wallet_unavailable")

    def test_custom_code_survives_when_a_terminal_state_is_present(self):
        """⚠️ Mutation M35 caught this gap: ``custom_code`` only wins inside the
        explicit-state branch. An exception carrying ``error_code`` but no
        ``status`` / ``terminal_state`` never reaches it -- it falls through to
        the generic failed branch, which happens to preserve the code too, so
        the two paths look identical until the branch itself is mutated.
        """

        class Boom(Exception):
            status = "cancelled"
            error_code = "wallet_unavailable"

        state, code, retryable = _classify_exception(Boom())
        self.assertEqual((state, code, retryable), ("cancelled", "wallet_unavailable", False))

    def test_custom_code_alone_does_not_establish_a_terminal_state(self):
        """An error_code with no status is a plain failure, but keeps its code."""

        class Boom(Exception):
            error_code = "wallet_unavailable"

        state, code, retryable = _classify_exception(Boom())
        self.assertEqual((state, code, retryable), ("failed", "wallet_unavailable", False))

    def test_explicit_retryable_false_overrides_the_timeout_default(self):
        """⚠️ timed_out defaults to retryable, but an explicit False must win."""

        class Boom(Exception):
            terminal_state = "timed_out"
            retryable = False

        state, _code, retryable = _classify_exception(Boom())
        self.assertEqual(state, "timed_out")
        self.assertFalse(retryable)

    def test_unknown_and_cancelled_ignore_an_explicit_retryable_true(self):
        class Boom(Exception):
            terminal_state = "unknown"
            retryable = True

        state, _code, retryable = _classify_exception(Boom())
        self.assertEqual(state, "unknown")
        self.assertFalse(retryable, "outcome unknown must not be retried, even if asked")

    def test_outcome_unknown_attribute(self):
        class Boom(Exception):
            outcome_unknown = True

        state, code, retryable = _classify_exception(Boom())
        self.assertEqual((state, code, retryable), ("unknown", "payment_outcome_unknown", False))

    def test_retryable_attribute_is_honoured_for_plain_failures(self):
        class Boom(Exception):
            retryable = True

        state, code, retryable = _classify_exception(Boom())
        self.assertEqual((state, code, retryable), ("failed", "payment_link_manager_failed", True))


if __name__ == "__main__":
    unittest.main()

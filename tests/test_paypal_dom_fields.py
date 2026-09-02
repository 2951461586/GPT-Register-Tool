"""Behaviour tests for ``sms_tool/paypal/dom_fields.py`` (round-5 audit target).

Why this file exists
--------------------
``dom_fields`` is the money path's lowest layer: every PayPal field fill
ultimately goes through one of the primitives here.  The audit flagged the
"every selector fails → uniformly return ``False``" pattern as the place where
a PayPal front-end redesign would silently degrade into a no-op fill that still
looks successful to the caller.

These tests therefore pin down two things:

1. **What the failure path actually returns** (and that it returns it *silently*
   — no log line, no exception, no metric), so the caller contract is explicit
   and any future change to it is caught.
2. **The swallow semantics of the inner ``except`` blocks**, which is where
   "failure masquerading as success" is most concentrated.

Playwright is *not* installed/mocked: the test doubles below are hand-written
fakes that emulate only the Page/Locator surface these functions touch.  They
are stand-ins for the browser (an external dependency), not mocks of the code
under test — the real production functions execute top to bottom.
"""

from __future__ import annotations

import random

import pytest

from paypal_dom_fakes import FakeElement, FakePage, MissingElement
from sms_tool.paypal import dom_fields


def kinds(record):
    """Just the operation names from a recorded call log."""
    return [entry[0] for entry in record]


def payloads(record, kind):
    return [entry[1] for entry in record if entry[0] == kind and len(entry) > 1]


@pytest.fixture(autouse=True)
def _deterministic_random():
    """dom_fields sprinkles random delays; seed so runs are reproducible."""
    state = random.getstate()
    random.seed(20260902)
    yield
    random.setstate(state)


# ────────────────────────────── _value_matches ───────────────────────────────


def test_value_matches_exact_equality():
    assert dom_fields._value_matches("john@example.com", "john@example.com") is True


def test_value_matches_empty_expected_accepts_everything():
    """Empty expectation is a wildcard - used by the 'just clear it' callers."""
    assert dom_fields._value_matches("anything", "") is True
    assert dom_fields._value_matches("", "") is True
    assert dom_fields._value_matches(None, None) is True


def test_value_matches_ignores_surrounding_whitespace():
    assert dom_fields._value_matches("  4111 1111  ", "4111 1111") is True


def test_value_matches_accepts_masked_card_by_digit_suffix():
    """PayPal/Stripe echo back masked cards; the tail is the only checkable part."""
    assert dom_fields._value_matches("**** **** **** 4242", "4242") is True
    assert dom_fields._value_matches("card ending 4242", "4242") is True


def test_value_matches_rejects_different_digit_tail():
    assert dom_fields._value_matches("**** **** **** 4242", "1111") is False


def test_value_matches_is_case_insensitive_substring():
    assert dom_fields._value_matches("United States of America", "united states") is True


def test_value_matches_rejects_unrelated_values():
    assert dom_fields._value_matches("Berlin", "Hamburg") is False


def test_value_matches_is_idempotent():
    first = [dom_fields._value_matches("  ****4242 ", "4242") for _ in range(25)]
    assert len(set(first)) == 1


# ──────────────────────────── _locator_has_value ─────────────────────────────


def test_locator_has_value_true_when_value_stuck():
    assert dom_fields._locator_has_value(FakeElement(value="4242"), "4242") is True


def test_locator_has_value_false_when_value_did_not_stick():
    assert dom_fields._locator_has_value(FakeElement(value=""), "4242") is False


def test_locator_has_value_reports_success_when_reading_the_value_raises():
    """AUDIT POINT: an unreadable control is reported as 'value is fine'.

    ``except Exception: return True`` means a detached frame / timed-out read
    turns into a successful verification, so ``_fill_with_fallback`` returns
    True for a fill that was never confirmed.
    """
    el = FakeElement(value="", fail={"input_value"})
    assert dom_fields._locator_has_value(el, "4242") is True


def test_locator_has_value_swallows_error_even_for_empty_expectation():
    assert dom_fields._locator_has_value(FakeElement(fail={"input_value"}), "") is True


# ──────────────────────────── _set_field_value ───────────────────────────────


def test_set_field_value_dispatches_input_change_blur_after_fill():
    el = FakeElement(visible=True, value="")
    dom_fields._set_field_value(el, "4242")
    assert payloads(el.record, "dispatch") == ["input", "change", "blur"]


def test_set_field_value_falls_back_to_js_setter_when_fill_raises():
    el = FakeElement(visible=True, value="", fail={"fill"})
    dom_fields._set_field_value(el, "4242")
    assert el.evaluated, "the raw JS value-setter path must be exercised"


def test_set_field_value_lets_the_js_setter_exception_escape():
    """AUDIT POINT: the fallback is the one *unwrapped* call in this function.

    ``locator.fill`` is wrapped in ``try/except`` (line ~39) but its JS-setter
    fallback (line ~42) is not, so a page where both paths throw escapes into
    the caller instead of being contained like every other step here.
    """
    el = FakeElement(visible=True, value="", fail={"fill", "evaluate"})
    with pytest.raises(RuntimeError, match="evaluate failed"):
        dom_fields._set_field_value(el, "4242")


def test_set_field_value_containment_is_recovered_by_the_fallback_wrappers():
    """_fill_with_fallback re-catches it, so end users only see a bare False."""
    el = FakeElement(visible=True, value="", fail={"fill", "evaluate"})
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "4242") is False


def test_set_field_value_retries_with_keyboard_when_fill_did_not_stick():
    """Fill reports success but the value never lands -> press + type fallback.

    This is why a *successful* fill is not the same as a *verified* fill: the
    retry path is the only thing that turns a lying ``fill`` into a real value.
    """
    el = FakeElement(visible=True, value="")
    el.fill = lambda value, timeout=None: None  # pretend it worked, change nothing
    dom_fields._set_field_value(el, "4242")
    assert "press" in kinds(el.record)
    assert "type" in kinds(el.record)
    assert el.value == "4242"


def test_set_field_value_verification_uses_the_typed_value():
    """When both fill and typing fail the value genuinely stays blank."""
    el = FakeElement(visible=True, value="", fail={"type", "press"})
    el.fill = lambda value, timeout=None: None
    dom_fields._set_field_value(el, "4242")
    assert el.value == ""
    assert payloads(el.record, "dispatch") == ["input", "change", "blur"]


def test_set_field_value_swallows_errors_from_the_fallback_typing():
    """Both the Control+A press and the typing are wrapped in bare excepts."""
    el = FakeElement(visible=True, value="")
    el.fill = lambda value, timeout=None: None
    el.press = lambda key, timeout=None: (_ for _ in ()).throw(RuntimeError("boom"))
    el.type = lambda text, timeout=None, delay=None: (_ for _ in ()).throw(RuntimeError("boom"))
    dom_fields._set_field_value(el, "4242")  # must not raise
    assert el.value == ""


def test_set_field_value_still_dispatches_events_when_dispatch_raises():
    el = FakeElement(visible=True, value="4242")
    el.dispatch_event = lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
    dom_fields._set_field_value(el, "4242")  # must not raise


def test_set_field_value_is_idempotent_on_recorded_side_effects():
    a, b = FakeElement(visible=True, value=""), FakeElement(visible=True, value="")
    dom_fields._set_field_value(a, "4242")
    dom_fields._set_field_value(b, "4242")
    assert kinds(a.record) == kinds(b.record)


# ──────────────────────────── _click_with_fallback ───────────────────────────


def test_click_with_fallback_returns_false_when_no_selector_matches(caplog):
    page = FakePage(locators={})  # every selector resolves to MissingElement
    with caplog.at_level("DEBUG"):
        assert dom_fields._click_with_fallback(page, ["#a", "#b", "#c"]) is False
    assert caplog.text == ""  # AUDIT POINT: total failure is completely silent


def test_click_with_fallback_returns_false_for_empty_selector_list():
    assert dom_fields._click_with_fallback(FakePage(), []) is False


def test_click_with_fallback_clicks_the_first_visible_selector():
    target = FakeElement(visible=True)
    page = FakePage(locators={"#b": target})
    assert dom_fields._click_with_fallback(page, ["#a", "#b", "#c"]) is True
    assert ("click",) in target.record


def test_click_with_fallback_stops_after_the_first_success():
    first, second = FakeElement(visible=True), FakeElement(visible=True)
    page = FakePage(locators={"#a": first, "#b": second})
    assert dom_fields._click_with_fallback(page, ["#a", "#b"]) is True
    assert first.record and second.record == []


def test_click_with_fallback_continues_past_a_selector_that_raises():
    """A raising selector must not abort the search over the remaining ones."""
    boom, good = FakeElement(visible=True, fail={"click"}), FakeElement(visible=True)
    page = FakePage(locators={"#a": boom, "#b": good})
    assert dom_fields._click_with_fallback(page, ["#a", "#b"]) is True
    assert ("click",) in good.record


def test_click_with_fallback_returns_false_when_the_click_itself_fails():
    page = FakePage(locators={"#a": FakeElement(visible=True, fail={"click"})})
    assert dom_fields._click_with_fallback(page, ["#a"]) is False


def test_click_with_fallback_is_idempotent():
    page = FakePage(locators={"#a": FakeElement(visible=True)})
    results = [dom_fields._click_with_fallback(page, ["#a"]) for _ in range(5)]
    assert results == [True] * 5


# ──────────────────────────── _fill_with_fallback ────────────────────────────


def test_fill_with_fallback_returns_false_when_nothing_is_visible(caplog):
    page = FakePage(locators={})
    with caplog.at_level("DEBUG"):
        assert dom_fields._fill_with_fallback(page, ["#a", "#b"], "4242") is False
    assert caplog.text == ""


def test_fill_with_fallback_returns_true_when_value_verifies():
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "4242") is True
    assert el.value == "4242"


def test_fill_with_fallback_returns_false_when_value_does_not_stick():
    """The fill 'succeeds' but verification fails -> reported as a hard False.

    This is the one place the layer *does* catch a broken fill, so it is worth
    pinning: regressing it (e.g. relaxing the verify) would silently reintroduce
    fake-success fills.
    """
    el = FakeElement(visible=True, value="", fail={"type", "press"})
    el.fill = lambda value, timeout=None: None  # silently drops the value
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "4242") is False


def test_fill_with_fallback_recovers_when_only_the_keyboard_retry_works():
    """Fill is a no-op but typing lands -> still a success, value verified."""
    el = FakeElement(visible=True, value="")
    el.fill = lambda value, timeout=None: None
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "4242") is True
    assert el.value == "4242"


def test_fill_with_fallback_treats_empty_value_as_success_on_first_visible_field():
    el = FakeElement(visible=True, value="something")
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "") is True


def test_fill_with_fallback_never_raises_when_every_operation_explodes():
    el = FakeElement(visible=True, value="",
                     fail={"click", "fill", "press", "type", "dispatch_event",
                           "scroll_into_view_if_needed", "input_value", "evaluate"})
    page = FakePage(locators={"#a": el})
    assert dom_fields._fill_with_fallback(page, ["#a"], "4242") is False


# ──────────────────────────── _fill_dom_id / _ids ────────────────────────────


def test_fill_dom_id_returns_true_when_the_page_evaluate_echoes_the_value():
    page = FakePage(evaluate=lambda script, arg: arg["value"])
    assert dom_fields._fill_dom_id(page, "cardNumber", "4242") is True


def test_fill_dom_id_returns_false_when_element_is_absent():
    page = FakePage(evaluate=lambda script, arg: None)
    assert dom_fields._fill_dom_id(page, "nope", "4242") is False


def test_fill_dom_id_returns_false_when_the_dom_value_diverges():
    """The JS already mutated the input, yet the caller is told it failed."""
    page = FakePage(evaluate=lambda script, arg: "truncated")
    assert dom_fields._fill_dom_id(page, "cardNumber", "4242") is False


def test_fill_dom_id_accepts_masked_echo_from_paypal():
    page = FakePage(evaluate=lambda script, arg: "**** **** **** 4242")
    assert dom_fields._fill_dom_id(page, "cardNumber", "4242") is True


def test_fill_dom_id_searches_frames_when_the_top_page_has_no_element():
    top = FakePage(evaluate=lambda script, arg: None)
    frame = FakePage(evaluate=lambda script, arg: arg["value"])
    top.frames = [frame]
    assert dom_fields._fill_dom_id(top, "cardNumber", "4242") is True
    assert frame.evaluate_calls, "frame scope must be probed"


def test_fill_dom_id_swallows_evaluate_errors_from_a_frame():
    def _boom(script, arg):
        raise RuntimeError("frame detached")

    top = FakePage(evaluate=_boom)
    frame = FakePage(evaluate=lambda script, arg: arg["value"])
    top.frames = [frame]
    assert dom_fields._fill_dom_id(top, "cardNumber", "4242") is True


def test_fill_dom_id_handles_a_page_without_a_frames_attribute():
    class BarePage:
        def evaluate(self, script, arg=None):
            return arg["value"]

    assert dom_fields._fill_dom_id(BarePage(), "cardNumber", "4242") is True


def test_fill_dom_ids_short_circuits_on_the_first_success():
    calls = []

    def _eval(script, arg):
        calls.append(arg["id"])
        return arg["value"] if arg["id"] == "second" else None

    page = FakePage(evaluate=_eval)
    assert dom_fields._fill_dom_ids(page, ["first", "second", "third"], "4242") is True
    assert calls == ["first", "second"]


def test_fill_dom_ids_returns_false_for_an_empty_id_list():
    assert dom_fields._fill_dom_ids(FakePage(), [], "4242") is False


def test_fill_dom_ids_returns_false_when_every_id_is_absent():
    page = FakePage(evaluate=lambda script, arg: None)
    assert dom_fields._fill_dom_ids(page, ["a", "b"], "4242") is False


# ──────────────────────────── _field_has_any_value ───────────────────────────


def test_field_has_any_value_true_for_the_first_non_empty_id():
    page = FakePage(evaluate=lambda script, arg: "4242" if arg == "b" else None)
    assert dom_fields._field_has_any_value(page, ["a", "b"]) is True


def test_field_has_any_value_treats_whitespace_only_as_present():
    """Unlike _visible_field_has_value, this one does NOT re-trim in Python.

    It relies entirely on the in-page ``String(el.value || "").trim()``.  Any
    scope whose ``evaluate`` is not the real JS snippet (a stub, a cached
    shim, a non-conforming embed) can therefore report a blank field as
    filled.  Pinned as-is so the inconsistency is visible.
    """
    page = FakePage(evaluate=lambda script, arg: "   ")
    assert dom_fields._field_has_any_value(page, ["a"]) is True


def test_field_has_any_value_distinguishes_blank_from_missing_element():
    """None (element absent) is falsy -> keeps searching; '' is the JS contract."""
    page = FakePage(evaluate=lambda script, arg: None)
    assert dom_fields._field_has_any_value(page, ["a"]) is False


def test_field_has_any_value_false_when_every_scope_raises():
    def _boom(script, arg):
        raise RuntimeError("boom")

    page = FakePage(evaluate=_boom)
    assert dom_fields._field_has_any_value(page, ["a"]) is False


def test_field_has_any_value_false_for_empty_id_list():
    assert dom_fields._field_has_any_value(FakePage(), []) is False


# ──────────────────────────── _visible_field_has_value ───────────────────────


def test_visible_field_has_value_matches_expected_value():
    page = FakePage(locators={"#email": FakeElement(visible=True, value="a@b.com")})
    assert dom_fields._visible_field_has_value(page, ["#email"], "a@b.com") is True


def test_visible_field_has_value_accepts_any_non_empty_value_without_expectation():
    page = FakePage(locators={"#email": FakeElement(visible=True, value="whatever")})
    assert dom_fields._visible_field_has_value(page, ["#email"]) is True


def test_visible_field_has_value_false_when_only_blank_values_are_present():
    page = FakePage(locators={"#email": FakeElement(visible=True, value="   ")})
    assert dom_fields._visible_field_has_value(page, ["#email"]) is False


def test_visible_field_has_value_false_when_the_field_is_hidden():
    page = FakePage(locators={"#email": FakeElement(visible=False, value="a@b.com")})
    assert dom_fields._visible_field_has_value(page, ["#email"], "a@b.com") is False


def test_visible_field_has_value_false_on_value_mismatch():
    page = FakePage(locators={"#email": FakeElement(visible=True, value="other@x.com")})
    assert dom_fields._visible_field_has_value(page, ["#email"], "a@b.com") is False


# ──────────────────────────── _fill_by_label_fallback ────────────────────────


def test_fill_by_label_fallback_fills_through_get_by_label():
    el = FakeElement(visible=True, value="")
    page = FakePage(labels={"Email": el})
    assert dom_fields._fill_by_label_fallback(page, ["Email"], "a@b.com") is True
    assert el.value == "a@b.com"


def test_fill_by_label_fallback_falls_back_to_the_generic_js_scanner():
    page = FakePage(labels={}, evaluate=lambda script, arg: arg["value"])
    assert dom_fields._fill_by_label_fallback(page, ["Email"], "a@b.com") is True


def test_fill_by_label_fallback_returns_false_when_js_scanner_finds_nothing():
    page = FakePage(labels={}, evaluate=lambda script, arg: None)
    assert dom_fields._fill_by_label_fallback(page, ["Email"], "a@b.com") is False


def test_fill_by_label_fallback_swallows_js_errors_and_returns_false():
    def _boom(script, arg):
        raise RuntimeError("boom")

    page = FakePage(labels={}, evaluate=_boom)
    assert dom_fields._fill_by_label_fallback(page, ["Email"], "a@b.com") is False


def test_fill_by_label_fallback_still_runs_the_generic_scanner_with_no_labels():
    """With an empty label list the JS scanner is still invoked.

    In-page, ``wanted`` becomes ``[]`` so every candidate scores 0 and nothing
    is selected -> null.  Pinned here via a scanner that emulates that outcome.
    """
    seen = []

    def _scanner(script, arg):
        seen.append(arg["labels"])
        return None  # real JS: no wanted labels => no candidate scores > 0

    page = FakePage(evaluate=_scanner)
    assert dom_fields._fill_by_label_fallback(page, [], "a@b.com") is False
    assert seen == [[]]


# ──────────────────────────── _fill_by_visible_label_text ────────────────────


def test_fill_by_visible_label_text_true_when_js_echoes_the_value():
    page = FakePage(evaluate=lambda script, arg: arg["value"])
    assert dom_fields._fill_by_visible_label_text(page, "First name", "John") is True


def test_fill_by_visible_label_text_false_when_no_label_matches():
    page = FakePage(evaluate=lambda script, arg: None)
    assert dom_fields._fill_by_visible_label_text(page, "First name", "John") is False


def test_fill_by_visible_label_text_passes_label_and_value_to_the_page():
    seen = []
    page = FakePage(evaluate=lambda script, arg: seen.append(arg) or arg["value"])
    dom_fields._fill_by_visible_label_text(page, "  CITY  ", "Berlin")
    assert seen == [{"label": "  CITY  ", "value": "Berlin"}]


# ──────────────────────────── _fill_visible_input ────────────────────────────


def test_fill_visible_input_types_and_verifies():
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={"#card": el})
    assert dom_fields._fill_visible_input(page, ["#card"], "4242") is True
    assert el.value == "4242"


def test_fill_visible_input_fires_all_four_sync_events():
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={"#card": el})
    dom_fields._fill_visible_input(page, ["#card"], "4242")
    assert payloads(el.record, "dispatch") == ["input", "change", "blur", "focusout"]


def test_fill_visible_input_returns_false_when_nothing_is_visible():
    assert dom_fields._fill_visible_input(FakePage(), ["#card"], "4242") is False


def test_fill_visible_input_is_deterministic_across_repeated_calls():
    page = FakePage(locators={"#card": FakeElement(visible=True, value="")})
    results = [dom_fields._fill_visible_input(page, ["#card"], "4242") for _ in range(4)]
    assert results == [True] * 4


# ──────────────────────────── _select_with_fallback ──────────────────────────


def test_select_with_fallback_returns_true_on_select_option_success():
    el = FakeElement(visible=True)
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "CA") is True
    assert el.selected == "CA"


def test_select_with_fallback_tries_every_label_before_giving_up():
    el = FakeElement(visible=True, fail={"select_option"})
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "CA",
                                            labels=["California"]) is False
    assert [v for kind, v in el.record if kind == "select_option"] == ["CA", "California"]


def test_select_with_fallback_falls_back_to_js_option_matching():
    el = FakeElement(visible=True, fail={"select_option"})
    el.evaluate = lambda script, arg: True
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "CA") is True


def test_select_with_fallback_returns_false_when_js_matching_finds_nothing():
    el = FakeElement(visible=True, fail={"select_option"})
    el.evaluate = lambda script, arg: False
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "CA") is False


def test_select_with_fallback_returns_false_for_an_empty_value_and_no_labels():
    el = FakeElement(visible=True, fail={"select_option"})
    el.evaluate = lambda script, arg: False
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "") is False


def test_select_with_fallback_never_raises_when_evaluate_explodes():
    el = FakeElement(visible=True, fail={"select_option"})
    el.evaluate = lambda script, arg: (_ for _ in ()).throw(RuntimeError("boom"))
    page = FakePage(locators={"#state": el})
    assert dom_fields._select_with_fallback(page, ["#state"], "CA") is False


# ──────────────────────────── _read_field_value ──────────────────────────────


def test_read_field_value_prefers_the_dom_id_path():
    page = FakePage(evaluate=lambda script, arg: "a@b.com" if arg == "email" else None)
    assert dom_fields._read_field_value(page, ["email"], ["#nope"]) == "a@b.com"


def test_read_field_value_falls_back_to_selectors_when_ids_are_blank():
    page = FakePage(
        locators={"#email": FakeElement(visible=True, value="  fallback@x.com  ")},
        evaluate=lambda script, arg: None,
    )
    assert dom_fields._read_field_value(page, ["email"], ["#email"]) == "fallback@x.com"


def test_read_field_value_returns_empty_string_when_nothing_is_found():
    page = FakePage(evaluate=lambda script, arg: None)
    assert dom_fields._read_field_value(page, ["email"], ["#email"]) == ""


def test_read_field_value_returns_whitespace_untrimmed_from_the_id_path():
    """The id path trusts the in-page ``.trim()`` and does not re-trim itself.

    A blank-looking value therefore passes the ``if value:`` gate and is
    returned verbatim, which propagates into ``_verify_checkout_fields`` as a
    "field is filled" verdict.  Pinned as-is.
    """
    page = FakePage(evaluate=lambda script, arg: "    ")
    assert dom_fields._read_field_value(page, ["email"], ["#email"]) == "    "


def test_read_field_value_does_trim_on_the_selector_path():
    """The selector path, by contrast, strips in Python - note the asymmetry."""
    page = FakePage(
        locators={"#email": FakeElement(visible=True, value="  a@b.com  ")},
        evaluate=lambda script, arg: None,
    )
    assert dom_fields._read_field_value(page, [], ["#email"]) == "a@b.com"


def test_read_field_value_swallows_errors_from_both_paths():
    def _boom(script, arg):
        raise RuntimeError("boom")

    page = FakePage(
        locators={"#email": FakeElement(visible=True, fail={"is_visible", "input_value"})},
        evaluate=_boom,
    )
    assert dom_fields._read_field_value(page, ["email"], ["#email"]) == ""


def test_read_field_value_is_idempotent():
    page = FakePage(evaluate=lambda script, arg: "a@b.com" if arg == "email" else None)
    values = [dom_fields._read_field_value(page, ["email"], ["#x"]) for _ in range(4)]
    assert values == ["a@b.com"] * 4


# ──────────────────────────── _type_human ────────────────────────────────────


def test_type_human_emits_one_type_call_per_character(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dom_fields.time, "sleep", lambda secs: sleeps.append(secs))
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={"#cvv": el})
    dom_fields._type_human(page, "#cvv", "123")
    assert payloads(el.record, "type") == ["1", "2", "3"]
    assert len(sleeps) == 3


def test_type_human_uses_the_configured_delay_range(monkeypatch):
    monkeypatch.setattr(dom_fields.time, "sleep", lambda secs: None)
    seen = []
    el = FakeElement(visible=True, value="")
    original_type = el.type

    def _type(text, timeout=None, delay=None):
        seen.append(delay)
        original_type(text, timeout=timeout, delay=delay)

    el.type = _type
    page = FakePage(locators={"#cvv": el})
    dom_fields._type_human(page, "#cvv", "ab", delay_range=(7, 7))
    assert seen == [7, 7]


def test_type_human_does_not_swallow_errors_unlike_its_siblings():
    """_type_human is the only primitive without a try/except - pin that down."""
    page = FakePage(locators={"#cvv": FakeElement(visible=True, fail={"click"})})
    with pytest.raises(RuntimeError):
        dom_fields._type_human(page, "#cvv", "123")


def test_type_human_on_an_empty_string_touches_nothing_but_the_click(monkeypatch):
    monkeypatch.setattr(dom_fields.time, "sleep", lambda secs: None)
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={"#cvv": el})
    dom_fields._type_human(page, "#cvv", "")
    assert kinds(el.record) == ["click"]


# ────────────────── cross-cutting: the silent-failure contract ───────────────


@pytest.mark.parametrize("call", [
    lambda: dom_fields._click_with_fallback(FakePage(), ["#a"]),
    lambda: dom_fields._fill_with_fallback(FakePage(), ["#a"], "v"),
    lambda: dom_fields._fill_dom_id(FakePage(), "a", "v"),
    lambda: dom_fields._fill_dom_ids(FakePage(), ["a"], "v"),
    lambda: dom_fields._fill_by_label_fallback(FakePage(), ["L"], "v"),
    lambda: dom_fields._fill_by_visible_label_text(FakePage(), "L", "v"),
    lambda: dom_fields._fill_visible_input(FakePage(), ["#a"], "v"),
    lambda: dom_fields._select_with_fallback(FakePage(), ["#a"], "v"),
])
def test_all_fill_primitives_report_failure_as_false_and_nothing_else(call, caplog):
    """AUDIT POINT: the whole layer fails identically and indistinguishably.

    Every primitive turns 'PayPal changed its markup' into the same bare
    ``False`` with no log record, so the orchestrator cannot tell a missing
    field apart from a typo in a selector.  Pinning it here means a future fix
    (e.g. logging or raising) will break these tests on purpose.
    """
    with caplog.at_level("DEBUG"):
        assert call() is False
    assert caplog.records == []


@pytest.mark.parametrize("call", [
    lambda: dom_fields._click_with_fallback(FakePage(), ["#a"]),
    lambda: dom_fields._fill_with_fallback(FakePage(), ["#a"], "v"),
    lambda: dom_fields._fill_dom_ids(FakePage(), ["a"], "v"),
    lambda: dom_fields._fill_visible_input(FakePage(), ["#a"], "v"),
])
def test_fill_primitives_are_idempotent_on_a_dead_page(call):
    assert [call() for _ in range(3)] == [False, False, False]

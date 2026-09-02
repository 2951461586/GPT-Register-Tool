"""Shared Playwright test doubles for the PayPal DOM/flow tests.

Hand-written fakes for the small Page/Locator surface touched by
``sms_tool.paypal.dom_fields`` and its callers.  They stand in for the
browser (an external dependency) so the real production code runs top to
bottom without a network or a browser process.
"""

from __future__ import annotations


# ───────────────────────── test doubles (fake browser) ────────────────────────


class FakeElement:
    """A stand-in for a Playwright Locator that has been narrowed with .first."""

    def __init__(self, visible=True, value="", fail=(), record=None):
        self.visible = visible
        self.value = value
        self.fail = set(fail)
        self.record = record if record is not None else []
        self.selected = None
        self.evaluated = []

    def _maybe_raise(self, name):
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    # -- reads ---------------------------------------------------------------
    def is_visible(self, timeout=None):
        self._maybe_raise("is_visible")
        return self.visible

    def input_value(self, timeout=None):
        self._maybe_raise("input_value")
        return self.value

    def is_checked(self):
        return False

    # -- writes --------------------------------------------------------------
    def click(self, timeout=None):
        self.record.append(("click",))
        self._maybe_raise("click")

    def scroll_into_view_if_needed(self, timeout=None):
        self.record.append(("scroll",))
        self._maybe_raise("scroll_into_view_if_needed")

    def fill(self, value, timeout=None):
        self.record.append(("fill", value))
        self._maybe_raise("fill")
        self.value = value

    def press(self, key, timeout=None):
        self.record.append(("press", key))
        self._maybe_raise("press")

    def type(self, text, timeout=None, delay=None):
        self.record.append(("type", text))
        self._maybe_raise("type")
        self.value = text

    def dispatch_event(self, name):
        self.record.append(("dispatch", name))
        self._maybe_raise("dispatch_event")

    def select_option(self, value=None, timeout=None):
        self.record.append(("select_option", value))
        self._maybe_raise("select_option")
        self.selected = value

    def check(self, timeout=None):
        self.record.append(("check",))

    # -- JS escape hatch -----------------------------------------------------
    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        self._maybe_raise("evaluate")
        return False


class _FirstProxy:
    """Emulates ``page.locator(sel)``.

    A real Playwright Locator exposes ``.first`` *and* the element methods
    (``inner_text``, ``is_visible``, ...) directly on itself, so unknown
    attributes are forwarded to the wrapped element.
    """

    def __init__(self, element):
        self.first = element

    def __getattr__(self, name):
        return getattr(self.first, name)


class MissingElement(FakeElement):
    """What Playwright gives you for a selector that matches nothing.

    ``is_visible`` returns False (Playwright semantics); every write raises
    because there is nothing to write to.
    """

    def __init__(self):
        super().__init__(visible=False, fail={"fill", "click", "type", "press",
                                              "select_option", "input_value",
                                              "scroll_into_view_if_needed"})


class FakePage:
    """Minimal Page double.

    *locators* maps selector -> FakeElement.  *labels* maps label text ->
    FakeElement and is served through ``get_by_label`` / ``get_by_placeholder``.
    *evaluate* is either a callable ``(script, arg) -> result`` or ``None``.
    """

    def __init__(self, locators=None, labels=None, frames=None, evaluate=None):
        self.frames = list(frames or [])
        self._locators = dict(locators or {})
        self._labels = dict(labels or {})
        self._evaluate = evaluate
        self.evaluate_calls = []

    def locator(self, selector):
        return _FirstProxy(self._locators.get(selector) or MissingElement())

    def get_by_label(self, text, exact=False):
        return _FirstProxy(self._labels.get(text) or MissingElement())

    def get_by_placeholder(self, text, exact=False):
        return _FirstProxy(self._labels.get(text) or MissingElement())

    def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        if self._evaluate is None:
            return None
        return self._evaluate(script, arg)

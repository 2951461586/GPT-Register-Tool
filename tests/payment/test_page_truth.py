"""Debug screenshots must ship machine-readable page truth (2026-09-22).

Why this exists
---------------
``runtime/.../debug/*.png`` is what an operator looks at when a PayPal or captcha
flow stalls, and it is read by eye: not greppable, not diffable between runs, and
misleading whenever the image disagrees with the DOM -- a compositing-stale frame,
a full-page capture taken mid-navigation, or an overlay already dismissed.

Every screenshot now gets a ``<name>.json`` sidecar. The tests below pin the two
things that make it trustworthy rather than decorative:

* it records that the truth was collected **before** the image, so a reader can
  tell which of the two is stale;
* it never raises. This runs on failure paths, where a diagnostic that throws
  would replace the real error with its own.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import page_truth


class _Page:
    """Minimal stand-in: ``evaluate`` returns whatever the script asked for."""

    def __init__(self, result=None, *, raises=None):
        self.result = result
        self.raises = raises
        self.scripts: list[str] = []

    def evaluate(self, script):
        self.scripts.append(script)
        if self.raises is not None:
            raise self.raises
        return self.result


TRUTH = {
    "url": "https://www.paypal.com/checkout",
    "title": "PayPal",
    "readyState": "complete",
    "visibleDialogs": ["cookie-banner"],
    "focusedElement": "email",
    "formFieldCount": 7,
}


class CollectTests(unittest.TestCase):
    def test_the_script_reads_the_fields_the_sidecar_promises(self):
        page = _Page(TRUTH)
        self.assertEqual(page_truth.collect_page_truth(page), TRUTH)
        self.assertEqual(len(page.scripts), 1)
        script = page.scripts[0]
        for field in ("readyState", "visibleDialogs", "focusedElement", "formFieldCount"):
            self.assertIn(field, script, "the injected script must actually collect this")

    def test_a_none_page_yields_no_truth(self):
        self.assertEqual(page_truth.collect_page_truth(None), {})

    def test_a_page_that_raises_yields_no_truth_instead_of_raising(self):
        page = _Page(raises=RuntimeError("target closed"))
        self.assertEqual(page_truth.collect_page_truth(page), {})

    def test_a_non_dict_result_is_discarded(self):
        self.assertEqual(page_truth.collect_page_truth(_Page("not a dict")), {})
        self.assertEqual(page_truth.collect_page_truth(_Page(None)), {})


class WriteTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = Path(self._dir.name) / "debug"

    def test_the_sidecar_is_written_next_to_the_screenshot_name(self):
        path = page_truth.write_page_truth(_Page(TRUTH), self.out, "04_email_filled")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "04_email_filled.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in TRUTH.items():
            self.assertEqual(payload[key], value)

    def test_the_sidecar_records_that_it_precedes_the_screenshot(self):
        path = page_truth.write_page_truth(_Page(TRUTH), self.out, "step")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(payload["capturedBeforeScreenshot"], True)
        self.assertIn("capturedAt", payload)

    def test_the_missing_directory_is_created(self):
        self.assertFalse(self.out.exists())
        path = page_truth.write_page_truth(_Page(TRUTH), self.out, "step")
        self.assertTrue(path.parent.is_dir())

    def test_an_unreadable_page_writes_no_file(self):
        self.assertIsNone(page_truth.write_page_truth(_Page(raises=RuntimeError("x")), self.out, "s"))
        self.assertFalse((self.out / "s.json").exists())

    def test_an_unwritable_directory_returns_none_instead_of_raising(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
            self.assertIsNone(page_truth.write_page_truth(_Page(TRUTH), self.out, "s"))

    def test_the_sidecar_uses_lf_endings(self):
        """Windows text mode would rewrite them and defeat the run-to-run diff."""
        path = page_truth.write_page_truth(_Page(TRUTH), self.out, "step")
        raw = path.read_bytes()
        self.assertNotIn(b"\r\n", raw)


class ScreenshotHelperTests(unittest.TestCase):
    """The wiring, not the module: ``_screenshot`` must emit both artifacts."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = Path(self._dir.name)

    def test_both_the_png_and_the_truth_sidecar_are_written(self):
        from sms_tool.paypal import session

        page = mock.Mock()
        page.evaluate.return_value = TRUTH
        session._screenshot(page, str(self.out), "03_create_account")

        page.screenshot.assert_called_once()
        self.assertTrue((self.out / "03_create_account.json").is_file())

    def test_truth_is_collected_before_the_image_is_captured(self):
        """Order matters: it is what makes a stale sidecar distinguishable."""
        from sms_tool.paypal import session

        order: list[str] = []
        page = mock.Mock()
        page.evaluate.side_effect = lambda _script: order.append("truth") or TRUTH
        page.screenshot.side_effect = lambda **_kwargs: order.append("png")

        session._screenshot(page, str(self.out), "step")
        self.assertEqual(order, ["truth", "png"])

    def test_a_broken_page_does_not_stop_the_screenshot(self):
        from sms_tool.paypal import session

        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("target closed")
        session._screenshot(page, str(self.out), "step")
        page.screenshot.assert_called_once()

    def test_disabled_screenshots_write_nothing(self):
        from sms_tool.paypal import session

        page = mock.Mock()
        session._screenshot(page, str(self.out), "step", enabled=False)
        page.screenshot.assert_not_called()
        self.assertEqual(list(self.out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

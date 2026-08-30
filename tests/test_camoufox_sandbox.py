"""Regression tests for the Camoufox content-sandbox workaround.

Symptom on affected Windows hosts: the Camoufox browser process starts, Juggler
reports "listening to the pipe", and then **every** ``new_page()`` (and
therefore Playwright's ``launch_persistent_context``) hangs forever with no
error.  The only hint is a graphics annotation in the browser log:
``RenderCompositorSWGL failed mapping default framebuffer, no dt``.

Root cause: Firefox cannot spawn renderer/content processes while the content
sandbox is enabled.  Setting ``MOZ_DISABLE_CONTENT_SANDBOX=1`` before the
browser is spawned fixes it.

The driver sets the variable **only around the launch** and restores the
caller's environment afterwards: the browser captures its environment at spawn
time and content processes inherit it from the browser process, so a
process-wide variable would be an unnecessary side effect for the other drivers.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from sms_tool.registration_drivers import external_sessions as es

ENV = es.MOZ_DISABLE_CONTENT_SANDBOX


class _RecordingCamoufox:
    """Stand-in for ``camoufox.sync_api.Camoufox`` capturing the env at spawn."""

    seen_env: str | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _RecordingCamoufox.seen_env = os.environ.get(ENV)

    def __enter__(self):
        context = MagicMock()
        context.pages = []
        context.new_page.return_value = MagicMock()
        return context

    def __exit__(self, *exc):
        return False


class CamoufoxContentSandboxTests(unittest.TestCase):
    def _launch(self, **driver_cfg):
        session = es.CamoufoxBrowserSession(
            config={"registration": {"drivers": {"camoufox": driver_cfg}}},
            headless=True,
            timeout_ms=5_000,
            locale="en-US",
            timezone_id="America/New_York",
            proxy=None,
        )
        import camoufox.sync_api

        with patch.object(camoufox.sync_api, "Camoufox", _RecordingCamoufox):
            session.__enter__()
        return {
            "during": _RecordingCamoufox.seen_env,
            "after": os.environ.get(ENV),
        }

    def setUp(self):
        self._saved = os.environ.get(ENV)
        os.environ.pop(ENV, None)

    def tearDown(self):
        os.environ.pop(ENV, None)
        if self._saved is not None:
            os.environ[ENV] = self._saved

    def test_workaround_applied_by_default_and_env_restored(self):
        result = self._launch()
        self.assertEqual(result["during"], "1")
        # Must not leak a process-wide variable to the other drivers.
        self.assertIsNone(result["after"])

    def test_blank_driver_config_still_gets_the_workaround(self):
        self.assertEqual(self._launch()["during"], "1")

    def test_existing_value_is_restored_not_cleared(self):
        os.environ[ENV] = "0"
        result = self._launch()
        self.assertEqual(result["during"], "1")
        self.assertEqual(result["after"], "0")

    def test_can_be_disabled_via_config(self):
        result = self._launch(disable_content_sandbox=False)
        self.assertIsNone(result["during"])
        self.assertIsNone(result["after"])


if __name__ == "__main__":
    unittest.main()

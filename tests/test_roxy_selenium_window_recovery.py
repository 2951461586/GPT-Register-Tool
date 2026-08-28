import unittest
from types import SimpleNamespace

from sms_tool.registration_drivers.roxy_selenium import _fetch_json_with_window_recovery


class NoSuchWindowException(Exception):
    pass


class InvalidSessionIdException(Exception):
    pass


class _RecoveringDriver:
    def __init__(self):
        self.current_window_handle = "closed"
        self.window_handles = ["closed", "callback"]
        self.urls = {
            "closed": "https://auth.openai.com/about-you",
            "callback": "https://chatgpt.com/",
        }
        self.switch_to = SimpleNamespace(window=self._switch)
        self.attempts = 0

    @property
    def current_url(self):
        if self.current_window_handle == "closed":
            raise NoSuchWindowException("no such window")
        return self.urls[self.current_window_handle]

    def _switch(self, handle):
        self.current_window_handle = handle

    def set_script_timeout(self, _seconds):
        return None

    def execute_async_script(self, _script, target):
        self.attempts += 1
        if self.attempts == 1:
            raise NoSuchWindowException("no such window")
        self.last_target = target
        return {"status": 200, "body": {"accessToken": "present"}}


class RoxySeleniumWindowRecoveryTests(unittest.TestCase):
    def test_fetch_recovers_chatgpt_callback_window_after_closed_target(self):
        driver = _RecoveringDriver()

        result = _fetch_json_with_window_recovery(driver, "/api/auth/session", timeout_ms=5000)

        self.assertEqual(result["status"], 200)
        self.assertEqual(driver.current_window_handle, "callback")
        self.assertEqual(driver.last_target, "/api/auth/session")
        self.assertEqual(driver.attempts, 2)

    def test_fetch_preserves_exception_class_and_redacts_proxy_message(self):
        driver = _RecoveringDriver()
        driver.window_handles = []

        def fail(_script, _target):
            raise InvalidSessionIdException(
                "invalid session id via http://user:secret@127.0.0.1:8080"
            )

        driver.execute_async_script = fail
        result = _fetch_json_with_window_recovery(
            driver,
            "/api/auth/session",
            proxy="http://user:secret@127.0.0.1:8080",
        )

        error = result["body"]["error"]
        self.assertIn("InvalidSessionIdException", error)
        self.assertIn("invalid session id", error)
        self.assertNotIn("user:secret", error)
        self.assertIn("***:***", error)


if __name__ == "__main__":
    unittest.main()

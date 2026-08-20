import json
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from sms_tool import sentinel_tokens


class _Cookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **_kwargs):
        self.values[name] = value

    def get_dict(self):
        return dict(self.values)


class _Session:
    def __init__(self):
        self.cookies = _Cookies()
        self.proxies = {}

    def post(self, _url, *, data, **_kwargs):
        flow = json.loads(data)["flow"]
        response = Mock()
        response.json.return_value = {
            "token": f"challenge-{flow}",
            "p": f"p-{flow}",
            "so": f"so-{flow}",
        }
        return response

    def get(self, *_args, **_kwargs):
        return Mock(status_code=200)


class SentinelTokenTests(unittest.TestCase):
    def setUp(self):
        sentinel_tokens.sentinel_metrics_snapshot(reset=True)
        sentinel_tokens._sentinel_provider_health.clear()

    def test_sentinel_frame_version_can_be_updated_from_environment(self):
        with patch.dict("os.environ", {"OPENAI_SENTINEL_VERSION": "next-version"}):
            self.assertEqual(sentinel_tokens._sentinel_frame_version(), "next-version")

    def test_http_extract_persist_false_does_not_write_shared_cache(self):
        with patch("sms_tool.sentinel_tokens.curl_requests.Session", _Session), \
             patch("sms_tool.sentinel_tokens._save_sentinel_cache") as save:
            result = sentinel_tokens._extract_sentinel_http(persist=False, device_id="did-fixed")

        save.assert_not_called()
        self.assertEqual(result["oai_did"], "did-fixed")
        self.assertEqual(json.loads(result["sentinel_token"])["id"], "did-fixed")
        self.assertEqual(json.loads(result["sentinel_authorize_continue_token"])["flow"], "authorize_continue")
        self.assertEqual(json.loads(result["sentinel_authorize_continue_so_token"])["flow"], "authorize_continue")
        self.assertEqual(json.loads(result["sentinel_oauth_token"])["id"], "did-fixed")

    def test_http_extract_requires_authorize_continue_challenge(self):
        class MissingAuthorize(_Session):
            def post(self, _url, *, data, **kwargs):
                response = super().post(_url, data=data, **kwargs)
                if json.loads(data)["flow"] == "authorize_continue":
                    response.json.return_value = {"token": "", "so": ""}
                return response
        with patch("sms_tool.sentinel_tokens.curl_requests.Session", MissingAuthorize):
            self.assertIsNone(sentinel_tokens._extract_sentinel_http(persist=False, device_id="did-fixed"))

    def test_quickjs_extract_persist_false_does_not_write_shared_cache(self):
        seen = []
        def token(_session, device_id, *, flow, **_kwargs):
            seen.append(_kwargs)
            return json.dumps({"p": "p", "t": "t", "c": "c", "id": device_id, "flow": flow})

        with patch("sms_tool.sentinel_tokens.curl_requests.Session", _Session), \
             patch("sms_tool.sentinel_quickjs.get_sentinel_token_via_quickjs", side_effect=token), \
             patch("sms_tool.sentinel_tokens._save_sentinel_cache") as save:
            result = sentinel_tokens._extract_sentinel_quickjs(persist=False, device_id="did-fixed")

        save.assert_not_called()
        self.assertEqual(result["oai_did"], "did-fixed")
        self.assertEqual(json.loads(result["sentinel_oauth_token"])["id"], "did-fixed")
        self.assertEqual(json.loads(result["sentinel_authorize_continue_token"])["id"], "did-fixed")
        self.assertEqual(json.loads(result["sentinel_authorize_continue_so_token"])["flow"], "authorize_continue")
        self.assertTrue(all(item["user_agent"].startswith("Mozilla/5.0 (Windows") for item in seen))
        self.assertTrue(all(item["navigator_platform"] == "Win32" for item in seen))

    def test_quickjs_extract_rejects_token_device_mismatch(self):
        def token(_session, device_id, *, flow, **_kwargs):
            return json.dumps({"p": "p", "t": "t", "c": "c", "id": "other", "flow": flow})

        with patch("sms_tool.sentinel_tokens.curl_requests.Session", _Session), \
             patch("sms_tool.sentinel_quickjs.get_sentinel_token_via_quickjs", side_effect=token):
            self.assertIsNone(sentinel_tokens._extract_sentinel_quickjs(persist=False, device_id="did-fixed"))

    def test_browser_collector_keeps_authorize_tokens_on_authorize_flow(self):
        class Page:
            def evaluate(self, script, args=None):
                if "SentinelSDK.init" in script:
                    return None
                if "document.cookie.match" in script:
                    return "did-fixed"
                flow = (args or {}).get("flow")
                return json.dumps({"p": "p", "t": "t", "c": "c", "so": "so", "id": "did-fixed", "flow": flow})
        ctx = Mock()
        ctx.cookies.return_value = [{"name": "oai-did", "value": "did-fixed"}]
        with patch("sms_tool.sentinel_tokens._save_sentinel_cache"):
            result = sentinel_tokens._collect_sentinel_tokens(Page(), ctx, persist=False)
        self.assertEqual(json.loads(result["sentinel_authorize_continue_token"])["flow"], "authorize_continue")
        self.assertEqual(json.loads(result["sentinel_authorize_continue_so_token"])["flow"], "authorize_continue")

    def test_force_fresh_propagates_non_persistent_extraction(self):
        expected = {"sentinel_token": "token", "oai_did": "did"}
        with patch.object(sentinel_tokens, "CFG", {"email_registration": {"sentinel_allow_http_fallback": True}}), \
             patch("sms_tool.sentinel_tokens._sentinel_mode", return_value="http"), \
             patch("sms_tool.sentinel_tokens._extract_sentinel_http", return_value=expected) as extract:
            result = sentinel_tokens._extract_sentinel(force_fresh=True, persist=False)

        self.assertIs(result, expected)
        extract.assert_called_once_with(None, persist=False)

    def test_auto_mode_does_not_fall_back_to_synthetic_http_pow(self):
        with patch("sms_tool.sentinel_tokens._sentinel_mode", return_value="auto"), \
             patch("sms_tool.sentinel_tokens._extract_sentinel_quickjs", return_value=None) as quickjs, \
             patch("sms_tool.sentinel_tokens._extract_sentinel_http") as http, \
             patch("sms_tool.sentinel_tokens._extract_sentinel_cloakbrowser") as browser:
            result = sentinel_tokens._extract_sentinel(
                proxy="socks5h://127.0.0.1:7897",
                force_fresh=True,
                persist=False,
            )

        self.assertIsNone(result)
        quickjs.assert_called_once_with("socks5h://127.0.0.1:7897", persist=False, device_id=None)
        http.assert_not_called()
        browser.assert_not_called()

    def test_fresh_extraction_is_bounded_to_two_concurrent_calls(self):
        active = 0
        max_active = 0
        state_lock = threading.Lock()
        two_started = threading.Event()
        release = threading.Event()

        def extract(_proxy=None, persist=True):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    two_started.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1
            return {"sentinel_token": "token", "persist": persist}

        gate = threading.BoundedSemaphore(2)
        with patch.object(sentinel_tokens, "CFG", {"email_registration": {"sentinel_allow_http_fallback": True}}), \
             patch.object(sentinel_tokens, "_sentinel_extraction_gate", gate), \
             patch("sms_tool.sentinel_tokens._sentinel_mode", return_value="http"), \
             patch("sms_tool.sentinel_tokens._extract_sentinel_http", side_effect=extract):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(sentinel_tokens._extract_sentinel, force_fresh=True, persist=False)
                    for _ in range(4)
                ]
                self.assertTrue(two_started.wait(timeout=1))
                self.assertEqual(max_active, 2)
                release.set()
                results = [future.result(timeout=2) for future in futures]

        self.assertEqual(len(results), 4)
        self.assertEqual(max_active, 2)

    def test_sentinel_concurrency_defaults_to_two_and_is_capped(self):
        with patch.object(sentinel_tokens, "CFG", {"email_registration": {}}):
            self.assertEqual(sentinel_tokens._sentinel_max_concurrency(), 2)
        with patch.object(sentinel_tokens, "CFG", {"email_registration": {"sentinel_max_concurrency": 99}}):
            self.assertEqual(sentinel_tokens._sentinel_max_concurrency(), 4)

    def test_cache_write_is_valid_json_after_parallel_updates(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sentinel_tokens, "SENTINEL_CACHE_FILE", Path(tmp) / "sentinel.json"):
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda i: sentinel_tokens._save_sentinel_cache({"sentinel_token": f"t-{i}"}), range(40)))

            cached = json.loads(sentinel_tokens.SENTINEL_CACHE_FILE.read_text(encoding="utf-8"))

        self.assertTrue(cached["sentinel_token"].startswith("t-"))
        self.assertIn("ts", cached)

    def test_auto_mode_records_quickjs_failure_without_synthetic_fallback(self):
        with patch.object(sentinel_tokens, "_sentinel_mode", return_value="auto"), \
             patch.object(sentinel_tokens, "_extract_sentinel_quickjs", return_value=None), \
             patch.object(sentinel_tokens, "_extract_sentinel_http"), \
             patch.object(sentinel_tokens, "_extract_sentinel_cloakbrowser") as browser:
            result = sentinel_tokens._extract_sentinel_uncached(persist=False)
        metrics = sentinel_tokens.sentinel_metrics_snapshot()
        self.assertIsNone(result)
        self.assertEqual(metrics["fallbacks"], 0)
        self.assertEqual(metrics["providers"]["quickjs"]["failure"], 1)
        self.assertNotIn("http", metrics["providers"])
        self.assertNotIn("secret", json.dumps(metrics))
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for sms_tool.omakse_client.

Tests cover:
  - Config resolution (base_url, proxy)
  - Link extraction job creation and polling
  - US protocol payment job creation and polling
  - Proxy test endpoint
  - Cancel endpoint
  - extract_links_for_account convenience wrapper
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from sms_tool import omakse_client


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


class ConfigResolutionTests(unittest.TestCase):
    """Test that config values are resolved correctly."""

    @patch.object(omakse_client, "_load_json")
    def test_resolve_base_url_from_config(self, mock_load):
        mock_load.return_value = {
            "omakse": {"base_url": "http://custom.example.com/"},
        }
        url = omakse_client._resolve_base_url()
        self.assertEqual(url, "http://custom.example.com")

    def test_resolve_base_url_explicit_overrides_config(self):
        url = omakse_client._resolve_base_url("http://override.example.com/")
        self.assertEqual(url, "http://override.example.com")

    @patch.object(omakse_client, "_load_json")
    def test_resolve_base_url_default(self, mock_load):
        mock_load.return_value = {}
        url = omakse_client._resolve_base_url()
        self.assertEqual(url, omakse_client.DEFAULT_BASE_URL)

    @patch.object(omakse_client, "_load_json")
    def test_resolve_proxy_from_config(self, mock_load):
        mock_load.return_value = {
            "omakse": {"proxy": "http://omakse-proxy:8080"},
        }
        proxy = omakse_client._resolve_proxy()
        self.assertEqual(proxy, "http://omakse-proxy:8080")

    @patch.object(omakse_client, "_load_json")
    def test_resolve_proxy_fallback_to_proxy_default(self, mock_load):
        mock_load.return_value = {
            "proxy": {"default": "http://global-proxy:8080"},
        }
        proxy = omakse_client._resolve_proxy()
        self.assertEqual(proxy, "http://global-proxy:8080")

    @patch.object(omakse_client, "_load_json")
    def test_resolve_proxy_explicit_overrides(self, mock_load):
        mock_load.return_value = {
            "omakse": {"proxy": "http://config-proxy:8080"},
            "proxy": {"default": "http://global-proxy:8080"},
        }
        proxy = omakse_client._resolve_proxy("http://explicit:9090")
        self.assertEqual(proxy, "http://explicit:9090")


class SessionCreationTests(unittest.TestCase):
    """Test that the requests session is configured correctly."""

    def test_session_has_headers(self):
        s = omakse_client._make_session()
        self.assertIn("User-Agent", s.headers)
        self.assertIn("Accept", s.headers)
        self.assertIn("Content-Type", s.headers)

    def test_session_with_proxy(self):
        s = omakse_client._make_session("http://test-proxy:8080")
        self.assertEqual(s.proxies["http"], "http://test-proxy:8080")
        self.assertEqual(s.proxies["https"], "http://test-proxy:8080")

    def test_session_without_proxy(self):
        s = omakse_client._make_session()
        self.assertEqual(s.proxies, {})


class LinkExtractionTests(unittest.TestCase):
    """Test link extraction API calls."""

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_create_extract_job_success(self, mock_proxy, mock_url):
        job_data = {"id": "job-123", "status": "running"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, job_data)
            result = omakse_client.create_extract_job(credentials="test-token")
            self.assertEqual(result["id"], "job-123")
            self.assertEqual(result["status"], "running")
            # Verify the POST was called with the right URL and body
            call_args = mock_post.call_args
            self.assertIn("/api/link-extract/jobs", call_args[0][0])
            body = call_args[1]["json"]
            self.assertEqual(body["credentials"], "test-token")
            self.assertEqual(body["promotionCountry"], "VN")
            self.assertEqual(body["providerCountry"], "US")

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_create_extract_job_failure(self, mock_proxy, mock_url):
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(400, {"detail": "Invalid credentials"})
            with self.assertRaises(RuntimeError) as ctx:
                omakse_client.create_extract_job(credentials="")
            self.assertIn("400", str(ctx.exception))
            self.assertIn("Invalid credentials", str(ctx.exception))

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_get_extract_job_success(self, mock_proxy, mock_url):
        job_data = {"id": "job-123", "status": "running", "counters": {"total": 5, "success": 2}}
        with patch.object(omakse_client.requests.Session, "get") as mock_get:
            mock_get.return_value = _FakeResponse(200, job_data)
            result = omakse_client.get_extract_job("job-123")
            self.assertEqual(result["id"], "job-123")
            self.assertEqual(result["counters"]["success"], 2)

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    @patch.object(omakse_client.time, "sleep")  # Skip actual sleeping
    def test_extract_links_polls_until_completed(self, mock_sleep, mock_proxy, mock_url):
        # Simulate: first call returns "running", second returns "completed"
        create_response = _FakeResponse(200, {"id": "job-abc", "status": "running"})
        poll_responses = [
            _FakeResponse(200, {"id": "job-abc", "status": "running", "counters": {"total": 1, "success": 0}, "logs": ["starting"]}),
            _FakeResponse(200, {"id": "job-abc", "status": "completed", "counters": {"total": 1, "success": 1}, "logs": ["starting", "done"], "links": ["https://paypal.com/..."]}),
        ]
        call_count = [0]

        def mock_get_side_effect(*args, **kwargs):
            resp = poll_responses[min(call_count[0], len(poll_responses) - 1)]
            call_count[0] += 1
            return resp

        with patch.object(omakse_client.requests.Session, "post", return_value=create_response):
            with patch.object(omakse_client.requests.Session, "get", side_effect=mock_get_side_effect):
                result = omakse_client.extract_links(credentials="test-token", poll_interval=0.01, max_poll_seconds=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], "job-abc")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["links"]), 1)

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    @patch.object(omakse_client.time, "sleep")
    def test_extract_links_already_terminal(self, mock_sleep, mock_proxy, mock_url):
        create_response = _FakeResponse(200, {"id": "job-done", "status": "completed", "links": ["link1"]})
        with patch.object(omakse_client.requests.Session, "post", return_value=create_response):
            with patch.object(omakse_client.requests.Session, "get") as mock_get:
                result = omakse_client.extract_links(credentials="test-token", poll_interval=0.01)
        # Should not poll since job is already terminal
        mock_get.assert_not_called()
        self.assertTrue(result["ok"])

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    @patch.object(omakse_client.time, "sleep")
    @patch.object(omakse_client.time, "time")
    def test_extract_links_timeout(self, mock_time, mock_sleep, mock_proxy, mock_url):
        # Simulate timeout: time.time always returns a value beyond deadline
        mock_time.side_effect = [0, 1000, 2000]  # start, first poll, second check
        create_response = _FakeResponse(200, {"id": "job-slow", "status": "running"})
        poll_response = _FakeResponse(200, {"id": "job-slow", "status": "running", "counters": {}, "logs": []})

        with patch.object(omakse_client.requests.Session, "post", return_value=create_response):
            with patch.object(omakse_client.requests.Session, "get", return_value=poll_response):
                result = omakse_client.extract_links(credentials="test-token", poll_interval=0.01, max_poll_seconds=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "running")


class USPaymentTests(unittest.TestCase):
    """Test US protocol payment API calls."""

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_run_us_payment_success(self, mock_proxy, mock_url):
        resp_data = {"job_id": "us-job-123", "status": "started"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.run_us_payment(ba_token="BA-test123", proxy="http://proxy:8080")
            self.assertTrue(result["ok"])
            self.assertEqual(result["job_id"], "us-job-123")
            self.assertEqual(result["status"], "started")
            # Verify body
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["baToken"], "BA-test123")
            self.assertEqual(body["proxy"], "http://proxy:8080")
            self.assertEqual(body["phoneCountry"], "US")

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_run_us_payment_failure(self, mock_proxy, mock_url):
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(400, {"detail": "Invalid BA token"})
            with self.assertRaises(RuntimeError) as ctx:
                omakse_client.run_us_payment(ba_token="", proxy="http://proxy:8080")
            self.assertIn("400", str(ctx.exception))

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_get_us_payment_status(self, mock_proxy, mock_url):
        resp_data = {"status": "running", "result": {"ok": None}, "queue": {"active": 1, "total": 1}}
        with patch.object(omakse_client.requests.Session, "get") as mock_get:
            mock_get.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.get_us_payment_status("us-job-123")
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["queue"]["active"], 1)

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_get_us_payment_logs(self, mock_proxy, mock_url):
        resp_data = {"lines": ["step1", "step2", "step3"]}
        with patch.object(omakse_client.requests.Session, "get") as mock_get:
            mock_get.return_value = _FakeResponse(200, resp_data)
            logs = omakse_client.get_us_payment_logs("us-job-123")
            self.assertEqual(len(logs), 3)
            self.assertEqual(logs[0], "step1")

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_get_us_payment_logs_error(self, mock_proxy, mock_url):
        with patch.object(omakse_client.requests.Session, "get") as mock_get:
            mock_get.return_value = _FakeResponse(500, {"detail": "Server error"})
            logs = omakse_client.get_us_payment_logs("us-job-123")
            self.assertEqual(logs, [])

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_cancel_us_payment(self, mock_proxy, mock_url):
        resp_data = {"ok": True, "message": "cancelled"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.cancel_us_payment("us-job-123")
            self.assertTrue(result["ok"])

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    @patch.object(omakse_client.time, "sleep")
    def test_run_us_payment_and_wait_completion(self, mock_sleep, mock_proxy, mock_url):
        # Simulate: start returns job_id, first poll returns running, second returns completed
        start_response = _FakeResponse(200, {"job_id": "us-wait-123", "status": "started"})
        status_responses = [
            _FakeResponse(200, {"status": "running", "result": {"ok": None}, "queue": {"active": 1, "total": 1}}),
            _FakeResponse(200, {"status": "completed", "result": {"ok": True, "url": "https://paypal.com/..."}, "queue": {"active": 0, "total": 1}}),
        ]
        logs_response = _FakeResponse(200, {"lines": ["log1", "log2"]})

        status_call_count = [0]

        def mock_get_side_effect(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            if "/status" in url:
                idx = min(status_call_count[0], len(status_responses) - 1)
                status_call_count[0] += 1
                return status_responses[idx]
            elif "/logs" in url:
                return logs_response
            return _FakeResponse(200, {})

        with patch.object(omakse_client.requests.Session, "post", return_value=start_response):
            with patch.object(omakse_client.requests.Session, "get", side_effect=mock_get_side_effect):
                result = omakse_client.run_us_payment_and_wait(
                    ba_token="BA-test",
                    proxy="http://proxy:8080",
                    poll_interval=0.01,
                    max_poll_seconds=10,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], "us-wait-123")
        self.assertEqual(result["status"], "completed")

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_run_us_payment_auto_client_id(self, mock_proxy, mock_url):
        resp_data = {"job_id": "us-auto-id", "status": "started"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.run_us_payment(ba_token="BA-test", proxy="http://proxy:8080")
            body = mock_post.call_args[1]["json"]
            # Client ID should be auto-generated
            self.assertTrue(body["clientId"])
            self.assertTrue(body["clientId"].startswith("us-"))
            self.assertEqual(result["client_id"], body["clientId"])

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_run_us_payment_custom_client_id(self, mock_proxy, mock_url):
        resp_data = {"job_id": "us-custom", "status": "started"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.run_us_payment(
                ba_token="BA-test",
                proxy="http://proxy:8080",
                client_id="my-custom-id",
            )
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["clientId"], "my-custom-id")


class ProxyTestTests(unittest.TestCase):
    """Test the proxy test endpoint."""

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_proxy_test_success(self, mock_proxy, mock_url):
        resp_data = {"ok": True, "ip": "1.2.3.4", "country": "US"}
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(200, resp_data)
            result = omakse_client.test_proxy("http://test-proxy:8080")
            self.assertTrue(result["ok"])
            self.assertEqual(result["ip"], "1.2.3.4")

    @patch.object(omakse_client, "_resolve_base_url", return_value="http://test.example.com")
    @patch.object(omakse_client, "_resolve_proxy", return_value="")
    def test_proxy_test_failure(self, mock_proxy, mock_url):
        with patch.object(omakse_client.requests.Session, "post") as mock_post:
            mock_post.return_value = _FakeResponse(400, {"detail": "Proxy unreachable"})
            result = omakse_client.test_proxy("http://bad-proxy:9999")
            self.assertFalse(result["ok"])
            self.assertIn("error", result)


class ExtractForAccountTests(unittest.TestCase):
    """Test the extract_links_for_account convenience wrapper."""

    @patch.object(omakse_client, "extract_links")
    def test_extract_with_explicit_token(self, mock_extract):
        mock_extract.return_value = {"ok": True, "links": ["link1"]}
        result = omakse_client.extract_links_for_account(access_token="explicit-token")
        self.assertTrue(result["ok"])
        mock_extract.assert_called_once()
        call_kwargs = mock_extract.call_args[1]
        self.assertEqual(call_kwargs["credentials"], "explicit-token")

    @patch.object(omakse_client, "extract_links")
    def test_extract_no_token_returns_error(self, mock_extract):
        result = omakse_client.extract_links_for_account(email="nonexistent@test.com")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        mock_extract.assert_not_called()

    @patch.object(omakse_client, "_load_json")
    @patch.object(omakse_client, "extract_links")
    def test_extract_from_session_file(self, mock_extract, mock_load_json):
        mock_load_json.return_value = {"access_token": "token-from-file"}
        mock_extract.return_value = {"ok": True, "links": []}
        result = omakse_client.extract_links_for_account(
            email="",
            session_file="/fake/path.json",
        )
        self.assertTrue(result["ok"])
        call_kwargs = mock_extract.call_args[1]
        self.assertEqual(call_kwargs["credentials"], "token-from-file")


if __name__ == "__main__":
    unittest.main()

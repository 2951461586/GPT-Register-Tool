import unittest
from unittest.mock import patch

from sms_tool import http_client


class HttpClientRetryTests(unittest.TestCase):
    def test_transient_retry_forces_fresh_connection(self):
        calls = []

        class FakeSession:
            def get(self, url, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise RuntimeError("connection reset")
                return "ok"

        with patch.object(http_client.time, "sleep", return_value=None):
            result = http_client.request_with_retry(
                FakeSession(),
                "get",
                "https://example.test",
                attempts=2,
                retry_delay=0,
                headers={"Accept": "application/json"},
            )

        self.assertEqual(result, "ok")
        self.assertNotIn("Connection", calls[0]["headers"])
        self.assertEqual(calls[1]["headers"]["Connection"], "close")
        self.assertEqual(calls[1]["headers"]["Accept"], "application/json")

    def test_403_opens_session_circuit_for_following_requests(self):
        class Response:
            status_code = 403
            headers = {"Retry-After": "12"}

        class FakeSession:
            def get(self, _url, **_kwargs):
                return Response()

        session = FakeSession()
        first = http_client.request_with_retry(session, "get", "https://example.test", attempts=1)
        self.assertEqual(first.status_code, 403)
        with self.assertRaises(http_client.SessionCircuitOpen):
            http_client.request_with_retry(session, "get", "https://example.test", attempts=1)
        http_client.clear_session_circuit(session)
        self.assertEqual(http_client.request_with_retry(session, "get", "https://example.test", attempts=1).status_code, 403)


if __name__ == "__main__":
    unittest.main()

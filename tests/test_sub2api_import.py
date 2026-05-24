import json
import unittest
from unittest.mock import patch

from sms_tool import sub2api_import
from sms_tool import import_targets


class Sub2ApiImportTests(unittest.TestCase):
    def test_build_sub2api_payload_uses_codex_session_import_shape(self):
        payload = sub2api_import._build_sub2api_payload(
            {
                "email": "paid@example.com",
                "access_token": "at_123",
                "refresh_token": "rt_123",
                "expires": "2026-05-24T10:00:00Z",
            },
            group_ids=[7],
            proxy_id=9,
            priority=1,
            concurrency=10,
        )

        self.assertEqual(payload["name"], "paid@example.com")
        self.assertEqual(payload["group_ids"], [7])
        self.assertEqual(payload["proxy_id"], 9)
        self.assertEqual(payload["priority"], 1)
        self.assertEqual(payload["concurrency"], 10)
        self.assertTrue(payload["auto_pause_on_expired"])
        self.assertTrue(payload["update_existing"])
        content = json.loads(payload["content"])
        self.assertEqual(content["access_token"], "at_123")
        self.assertEqual(content["refresh_token"], "rt_123")

    def test_upload_to_sub2api_resolves_group_and_posts_import_endpoint(self):
        calls = []

        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            calls.append((origin, path, token, method, body))
            if path == "/api/v1/admin/groups/all":
                return {"ok": True, "data": [{"id": 7, "name": "codex", "platform": "openai"}]}
            if path == "/api/v1/admin/accounts/import/codex-session":
                return {"ok": True, "status_code": 200, "data": {"total": 1, "created": 1, "updated": 0, "failed": 0}}
            return {"ok": False, "error": "unexpected"}

        with patch.object(sub2api_import, "_request_json", side_effect=fake_request):
            result = sub2api_import.upload_to_sub2api(
                {"email": "paid@example.com", "access_token": "at_123"},
                origin="https://sub.example",
                api_token="jwt-token",
                group_name="codex",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(calls[0][1], "/api/v1/admin/groups/all")
        self.assertEqual(calls[1][1], "/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(calls[1][2], "jwt-token")

    def test_import_target_dispatches_sub2api(self):
        with patch.object(import_targets, "import_sub2api_sessions", return_value={"ok": True}) as imported:
            result = import_targets.import_account_sessions(
                "sub2api",
                ["paid@example.com"],
                sub2api_url="https://sub.example",
                sub2api_token="jwt-token",
            )

        self.assertTrue(result["ok"])
        imported.assert_called_once()
        self.assertEqual(imported.call_args.args[0], ["paid@example.com"])

    def test_sk_api_key_with_login_config_uses_login_token(self):
        calls = []

        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            calls.append((path, token, body))
            if path == "/api/v1/auth/login":
                return {"ok": True, "data": {"access_token": "jwt-token"}}
            if path == "/api/v1/admin/groups/all":
                return {"ok": True, "data": [{"id": 3, "name": "GPT", "platform": "openai"}]}
            if path == "/api/v1/admin/accounts/import/codex-session":
                return {"ok": True, "status_code": 200, "data": {"total": 1, "created": 1, "updated": 0, "failed": 0}}
            return {"ok": False, "error": "unexpected"}

        with patch.object(sub2api_import, "_request_json", side_effect=fake_request):
            result = sub2api_import.upload_to_sub2api(
                {"email": "paid@example.com", "access_token": "at_123"},
                origin="https://sub.example",
                api_token="sk-not-admin-token",
                login_email="admin@example.com",
                login_password="password",
                group_ids="#3",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "/api/v1/auth/login")
        self.assertEqual(calls[1][1], "jwt-token")
        self.assertEqual(result["group_ids"], [3])

    def test_admin_api_key_uses_x_api_key_header(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = '{"code":0,"message":"success","data":[]}'

            def json(self):
                return {"code": 0, "message": "success", "data": []}

        def fake_request(method, url, headers=None, data=None, timeout=30, impersonate=None):
            captured.update(headers or {})
            return FakeResponse()

        with patch.object(sub2api_import.curl_requests, "request", side_effect=fake_request):
            result = sub2api_import._request_json(
                "https://sub.example",
                "/api/v1/admin/groups/all",
                token="admin-secret",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured.get("x-api-key"), "admin-secret")
        self.assertNotIn("Authorization", captured)

    def test_fetch_sub2api_auth_files_normalizes_error_account_for_401_filter(self):
        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            if path.startswith("/api/v1/admin/accounts"):
                return {
                    "ok": True,
                    "data": {
                        "items": [
                            {
                                "name": "bad@example.com",
                                "platform": "openai",
                                "type": "oauth",
                                "status": "error",
                                "error_message": "upstream returned 401 unauthorized",
                            }
                        ],
                        "total": 1,
                        "pages": 1,
                    },
                }
            return {"ok": False, "error": "unexpected"}

        with patch.object(sub2api_import, "_request_json", side_effect=fake_request):
            result = sub2api_import.fetch_sub2api_auth_files(api_url="https://sub.example/api/v1", api_token="jwt-token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["email"], "bad@example.com")
        self.assertEqual(result["files"][0]["probe"]["status_code"], 401)


if __name__ == "__main__":
    unittest.main()

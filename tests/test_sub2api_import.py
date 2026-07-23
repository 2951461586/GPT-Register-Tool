import json
import unittest
from unittest.mock import patch

from sms_tool import sub2api_import
from sms_tool import import_targets


class Sub2ApiImportTests(unittest.TestCase):
    def test_agent_identity_import_skips_destructive_post_import_probe(self):
        prepared = {
            "ok": True,
            "data": {"auth_mode": "agent_identity", "agent_identity": {"email": "free@example.com"}},
            "email": "free@example.com",
            "path": "agent-free.json",
            "mode": "agent_identity_json",
            "auth_mode": "agent_identity",
            "source_path": "agent-free.json",
            "source_mode": "existing_agent_identity",
            "refresh_token_status": "not_required",
            "warnings": [],
        }
        upload_result = {"ok": True, "created": 1, "updated": 0, "failed": 0}

        with patch.object(sub2api_import, "_prepare_sub2api_import_data", return_value=prepared), \
             patch.object(sub2api_import, "upload_to_sub2api", return_value=upload_result) as upload, \
             patch.object(sub2api_import, "_record_sub2api_import"):
            result = sub2api_import.import_sub2api_session(
                email="free@example.com",
                api_url="https://sub.example",
                api_token="admin-secret",
                verify_after_import=True,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(upload.call_args.kwargs["verify_after_import"])
        self.assertIn("agent_identity_execution_probe_skipped", result["warnings"])

    def test_direct_agent_identity_upload_never_runs_account_test(self):
        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            if path == "/api/v1/admin/groups/all":
                return {"ok": True, "data": [{"id": 7, "name": "codex", "platform": "openai"}]}
            if path == "/api/v1/admin/accounts/import/codex-session":
                return {"ok": True, "status_code": 200, "data": {
                    "created": 1,
                    "updated": 0,
                    "failed": 0,
                    "items": [{"action": "created", "account_id": 42}],
                }}
            if path == "/api/v1/admin/accounts/42":
                return {"ok": True, "data": {"id": 42, "group_ids": [7], "status": "active"}}
            return {"ok": False, "error": "unexpected"}

        with (
            patch.object(sub2api_import, "_request_json", side_effect=fake_request),
            patch.object(sub2api_import, "_request_sub2api_test") as tested,
        ):
            result = sub2api_import.upload_to_sub2api(
                {"auth_mode": "agent_identity", "agent_identity": {"email": "free@example.com"}},
                origin="https://sub.example",
                api_token="jwt-token",
                group_name="codex",
                verify_after_import=True,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verification"]["structural_only"])
        tested.assert_not_called()

    def test_oauth_upload_can_still_run_account_test(self):
        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            if path == "/api/v1/admin/groups/all":
                return {"ok": True, "data": [{"id": 7, "name": "codex", "platform": "openai"}]}
            if path == "/api/v1/admin/accounts/import/codex-session":
                return {"ok": True, "status_code": 200, "data": {
                    "created": 1,
                    "updated": 0,
                    "failed": 0,
                    "items": [{"action": "created", "account_id": 42}],
                }}
            return {"ok": False, "error": "unexpected"}

        with (
            patch.object(sub2api_import, "_request_json", side_effect=fake_request),
            patch.object(sub2api_import, "_probe_sub2api_account", return_value={"ok": True}) as tested,
        ):
            result = sub2api_import.upload_to_sub2api(
                {"email": "paid@example.com", "access_token": "at"},
                origin="https://sub.example",
                api_token="jwt-token",
                group_name="codex",
                verify_after_import=True,
            )

        self.assertTrue(result["ok"])
        tested.assert_called_once_with("https://sub.example", "jwt-token", 42)

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
                verify_after_import=False,
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
                verify_after_import=False,
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

    def test_resolve_proxy_id_randomizes_configured_proxy_id_list(self):
        with patch.object(sub2api_import.random, "choice", return_value=4) as choice:
            proxy_id = sub2api_import._resolve_proxy_id(
                "https://sub.example",
                "admin-secret",
                proxy_id="1,2,3,4,5",
            )

        self.assertEqual(proxy_id, 4)
        choice.assert_called_once_with([1, 2, 3, 4, 5])

    def test_resolve_proxy_id_omits_proxy_when_not_configured(self):
        proxy_id = sub2api_import._resolve_proxy_id(
            "https://sub.example",
            "admin-secret",
        )

        self.assertIsNone(proxy_id)

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

    def test_auto_auth_mode_falls_back_to_oauth_on_agent_identity_403(self):
        """When auth_mode=auto and Agent Identity registration returns 403
        ('Agent registry is not enabled'), the import should fall back to
        oauth mode instead of failing."""
        source_data = {
            "email": "free@example.com",
            "access_token": "fake-at",
            "refresh_token": "fake-rt",
            "plan_type": "free",
        }

        def fake_load_cpa_source(email, session_file="", export_dir=""):
            return {"ok": True, "data": source_data, "path": "session.json", "mode": "codex_session_json"}

        agent_error = {
            "ok": False,
            "error": "agent_registration_http_403",
            "status_code": 403,
            "message": "Agent registry is not enabled.",
        }

        with patch.object(sub2api_import, "_load_cpa_source", side_effect=fake_load_cpa_source), \
             patch.object(sub2api_import, "_load_direct_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "load_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "create_agent_identity", return_value=agent_error), \
             patch.object(sub2api_import, "_write_cpa_json", return_value="export/free.json") as write_cpa:
            result = sub2api_import._prepare_sub2api_import_data(
                "free@example.com",
                auth_mode="auto",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_mode"], "oauth")
        self.assertEqual(result["mode"], "codex_session_json")
        self.assertTrue(any("agent_identity_fallback_to_oauth" in w for w in result["warnings"]))
        write_cpa.assert_called_once()

    def test_explicit_agent_identity_mode_falls_back_on_403_registry_disabled(self):
        """Even when auth_mode=agent_identity is explicitly set, a 403 'Agent
        registry is not enabled' error should fall back to oauth because the
        account permanently doesn't support Agent Registry."""
        source_data = {
            "email": "free@example.com",
            "access_token": "fake-at",
            "refresh_token": "fake-rt",
            "plan_type": "free",
        }

        def fake_load_cpa_source(email, session_file="", export_dir=""):
            return {"ok": True, "data": source_data, "path": "session.json", "mode": "codex_session_json"}

        agent_error = {
            "ok": False,
            "error": "agent_registration_http_403",
            "status_code": 403,
            "message": "Agent registry is not enabled.",
        }

        with patch.object(sub2api_import, "_load_cpa_source", side_effect=fake_load_cpa_source), \
             patch.object(sub2api_import, "_load_direct_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "load_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "create_agent_identity", return_value=agent_error), \
             patch.object(sub2api_import, "_write_cpa_json", return_value="export/free.json") as write_cpa:
            result = sub2api_import._prepare_sub2api_import_data(
                "free@example.com",
                auth_mode="agent_identity",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_mode"], "oauth")
        self.assertTrue(any("agent_identity_fallback_to_oauth" in w for w in result["warnings"]))
        write_cpa.assert_called_once()

    def test_explicit_agent_identity_mode_does_not_fall_back_on_non_403_error(self):
        """When auth_mode=agent_identity is explicitly set and the error is NOT
        a 403 registry-disabled error, the error should be returned directly
        without falling back to oauth."""
        source_data = {
            "email": "free@example.com",
            "access_token": "fake-at",
            "refresh_token": "fake-rt",
            "plan_type": "free",
        }

        def fake_load_cpa_source(email, session_file="", export_dir=""):
            return {"ok": True, "data": source_data, "path": "session.json", "mode": "codex_session_json"}

        agent_error = {
            "ok": False,
            "error": "agent_registration_request_failed",
        }

        with patch.object(sub2api_import, "_load_cpa_source", side_effect=fake_load_cpa_source), \
             patch.object(sub2api_import, "_load_direct_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "load_agent_identity", return_value={"ok": False}), \
             patch.object(sub2api_import, "create_agent_identity", return_value=agent_error):
            result = sub2api_import._prepare_sub2api_import_data(
                "free@example.com",
                auth_mode="agent_identity",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "agent_registration_request_failed")


if __name__ == "__main__":
    unittest.main()

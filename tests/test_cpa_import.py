import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import cpa_import


class CpaImportTests(unittest.TestCase):
    def test_build_cpa_payload_accepts_at_only_json(self):
        payload = cpa_import._build_cpa_payload(
            {
                "email": "paid@example.com",
                "access_token": "at_123",
                "session_token": "st_123",
                "account_id": "acc_123",
                "plan_type": "plus",
            }
        )

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["type"], "codex")
        self.assertEqual(data["access_token"], "at_123")
        self.assertEqual(data["session_token"], "st_123")
        self.assertEqual(data["account_id"], "acc_123")
        self.assertNotIn("refresh_token", data)

    def test_import_cpa_session_uses_existing_session_json_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session_file = tmp_path / "session_paid@example.com.json"
            export_dir = tmp_path / "codex_exports"
            session_file.write_text(
                json.dumps(
                    {
                        "email": "paid@example.com",
                        "access_token": "at_123",
                        "session_token": "st_123",
                        "account_id": "acc_123",
                        "plan_type": "plus",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(cpa_import, "_resolve_cpa_config", return_value=("https://cpa.example/v0/management/auth-files", "token")):
                with patch.object(cpa_import, "upload_to_cpa", return_value={"ok": True, "mode": "multipart", "status_code": 200, "filename": "codex-paid@example.com-plus.json"}) as upload:
                    with patch.object(cpa_import, "get_account_record", return_value={}):
                        with patch.object(cpa_import, "upsert_account", return_value=True):
                            result = cpa_import.import_cpa_session(
                                email="paid@example.com",
                                session_file=str(session_file),
                                export_dir=str(export_dir),
                                api_url="https://cpa.example/v0/management",
                                api_token="token",
                            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["export"]["mode"], "at_json")
        self.assertEqual(result["export"]["refresh_token_status"], "no_rt")
        self.assertEqual(upload.call_count, 1)
        uploaded_payload = upload.call_args.args[0]
        self.assertEqual(uploaded_payload["access_token"], "at_123")
        self.assertEqual(uploaded_payload["session_token"], "st_123")
        self.assertNotIn("refresh_token", uploaded_payload)

    def test_existing_cpa_source_prefers_k12_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp)
            (export_dir / "codex-paid@example.com-plus.json").write_text(
                json.dumps({"email": "paid@example.com", "access_token": "plus_at"}),
                encoding="utf-8",
            )
            k12_path = export_dir / "codex-paid@example.com-k12.json"
            k12_path.write_text(
                json.dumps({"email": "paid@example.com", "access_token": "k12_at", "account_id": "ws-1"}),
                encoding="utf-8",
            )

            found = cpa_import._existing_cpa_json_with_access_token("paid@example.com", str(export_dir))

        self.assertEqual(found, str(k12_path))

    def test_classify_cpa_auth_file_detects_401_probe(self):
        self.assertEqual(
            cpa_import.classify_cpa_auth_file({"probe": {"status_code": 401}, "email": "a@liziai.cloud"}),
            "token_invalid",
        )
        self.assertEqual(
            cpa_import.classify_cpa_auth_file({"status": "active", "email": "a@liziai.cloud"}),
            "active",
        )

    def test_probe_cpa_codex_quota_formats_available_quota(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"status_code": 200, "body": '{"quota":{"remaining":7,"limit":10}}'}

        with patch.object(cpa_import.curl_requests, "post", return_value=FakeResponse()):
            result = cpa_import.probe_cpa_codex_quota(
                {"email": "ok@example.com", "auth_index": "abc123"},
                api_url="https://cpa.example/v0/management/auth-files",
                api_token="token",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["quota_status"], "7/10")

    def test_probe_local_codex_quota_uses_saved_access_token(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"quota": {"remaining": 3, "limit": 5}}

        with patch.object(cpa_import.curl_requests, "get", return_value=FakeResponse()) as get:
            result = cpa_import.probe_local_codex_quota(
                {"email": "ok@example.com", "access_token": "at_123", "chatgpt_account_id": "acc_123"},
                proxy="http://proxy.example:8080",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["quota_status"], "3/5")
        call = get.call_args
        self.assertEqual(call.args[0], cpa_import.CODEX_USAGE_URL)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer at_123")
        self.assertEqual(call.kwargs["headers"]["Chatgpt-Account-Id"], "acc_123")
        self.assertEqual(call.kwargs["proxies"]["https"], "http://proxy.example:8080")

    def test_refresh_local_quota_statuses_persists_local_result(self):
        with patch.object(cpa_import, "get_account_record", return_value={"email": "ok@example.com", "access_token": "at_123"}), \
             patch.object(cpa_import, "probe_local_codex_quota", return_value={"ok": True, "quota_status": "可用"}), \
             patch.object(cpa_import, "mark_quota_status", return_value=True) as marked:
            result = cpa_import.refresh_local_quota_statuses(["ok@example.com"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["success"], 1)
        marked.assert_called_once()
        self.assertEqual(marked.call_args.args[0], "ok@example.com")
        self.assertEqual(marked.call_args.args[1], "可用")

    def test_refresh_local_quota_statuses_relogin_on_401_then_reprobe(self):
        with patch.object(
            cpa_import,
            "get_account_record",
            side_effect=[
                {"email": "ok@example.com", "access_token": "old_at", "json_path": "session.json"},
                {"email": "ok@example.com", "access_token": "new_at", "json_path": "session.json"},
            ],
        ), \
             patch.object(
                 cpa_import,
                 "probe_local_codex_quota",
                 side_effect=[
                     {"ok": False, "status": "token_invalid", "quota_status": "401失效"},
                     {"ok": True, "status": "active", "quota_status": "可用"},
                 ],
             ) as probe, \
             patch.object(cpa_import, "relogin_codex_account", return_value={"ok": True, "mode": "web_session"}) as relogin, \
             patch.object(cpa_import, "mark_quota_status", return_value=True) as marked:
            result = cpa_import.refresh_local_quota_statuses(["ok@example.com"], relogin_on_401=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["quota_status"], "可用")
        self.assertEqual(probe.call_count, 2)
        relogin.assert_called_once()
        self.assertEqual(relogin.call_args.kwargs["mode"], "auto")
        self.assertEqual(marked.call_args.args[1], "可用")

    def test_refresh_local_quota_statuses_marks_deactivated_after_relogin_failure(self):
        with patch.object(cpa_import, "get_account_record", return_value={"email": "bad@example.com", "access_token": "old_at"}), \
             patch.object(cpa_import, "probe_local_codex_quota", return_value={"ok": False, "status": "token_invalid", "quota_status": "401失效"}), \
             patch.object(cpa_import, "relogin_codex_account", return_value={"ok": False, "error": "account_deactivated"}), \
             patch.object(cpa_import, "mark_quota_status", return_value=True) as marked:
            result = cpa_import.refresh_local_quota_statuses(["bad@example.com"], relogin_on_401=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["quota_status"], "账号停用")
        self.assertEqual(marked.call_args.args[1], "账号停用")

    def test_relogin_codex_account_auto_uses_web_session_first(self):
        with patch.object(cpa_import, "relogin_web_session_account", return_value={"ok": True, "mode": "web_session"}) as web, \
             patch.object(cpa_import, "relogin_local_codex_account") as oauth:
            result = cpa_import.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "web_session")
        web.assert_called_once()
        oauth.assert_not_called()

    def test_relogin_codex_account_auto_falls_back_to_codex_oauth(self):
        with patch.object(cpa_import, "relogin_web_session_account", return_value={"ok": False, "mode": "web_session", "error": "missing_session_cookie"}), \
             patch.object(cpa_import, "relogin_local_codex_account", return_value={"ok": True, "mode": "codex_oauth_pkce"}) as oauth:
            result = cpa_import.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "codex_oauth_pkce")
        self.assertEqual(result["web_session_attempt"]["error"], "missing_session_cookie")
        oauth.assert_called_once()

    def test_relogin_codex_account_web_session_only_does_not_fallback(self):
        with patch.object(cpa_import, "relogin_web_session_account", return_value={"ok": False, "mode": "web_session", "error": "missing_session_cookie"}), \
             patch.object(cpa_import, "relogin_local_codex_account") as oauth:
            result = cpa_import.relogin_codex_account({"email": "ok@example.com"}, mode="web_session")

        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "web_session")
        oauth.assert_not_called()

    def test_refresh_cpa_quota_statuses_persists_matching_email(self):
        fetch_result = {"ok": True, "files": [{"email": "ok@example.com", "auth_index": "idx1"}]}
        with patch.object(cpa_import, "fetch_cpa_auth_files", return_value=fetch_result), \
             patch.object(cpa_import, "probe_cpa_codex_quota", return_value={"ok": True, "status": "active", "quota_status": "??"}), \
             patch.object(cpa_import, "mark_quota_status", return_value=True) as marked:
            result = cpa_import.refresh_cpa_quota_statuses(["ok@example.com"], api_url="https://cpa.example", api_token="token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["success"], 1)
        marked.assert_called_once()
        self.assertEqual(marked.call_args.args[0], "ok@example.com")
        self.assertEqual(marked.call_args.args[1], "??")

    def test_refresh_cpa_quota_statuses_marks_missing_email_not_imported(self):
        fetch_result = {"ok": True, "files": [{"email": "other@example.com", "auth_index": "idx1"}]}
        with patch.object(cpa_import, "fetch_cpa_auth_files", return_value=fetch_result), \
             patch.object(cpa_import, "probe_cpa_codex_quota") as probe, \
             patch.object(cpa_import, "mark_quota_status", return_value=True) as marked:
            result = cpa_import.refresh_cpa_quota_statuses(["missing@example.com"], api_url="https://cpa.example", api_token="token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["quota_status"], "未导入CPA")
        probe.assert_not_called()
        marked.assert_called_once()
        self.assertEqual(marked.call_args.args[0], "missing@example.com")
        self.assertEqual(marked.call_args.args[1], "未导入CPA")

    def test_fetch_cpa_auth_files_uses_config_fallback(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"files": [{"email": "ok@example.com", "auth_index": "idx1"}]}

        with patch.object(cpa_import, "_resolve_cpa_config", return_value=("https://cpa.example/v0/management", "token")), \
             patch.object(cpa_import.curl_requests, "get", return_value=FakeResponse()) as get:
            result = cpa_import.fetch_cpa_auth_files()

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(get.call_args.args[0], "https://cpa.example/v0/management/auth-files")

    def test_probe_cpa_codex_quota_detects_invalidated_token(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "status_code": 401,
                    "body": {
                        "error": {
                            "message": "Your authentication token has been invalidated. Please try signing in again."
                        }
                    },
                }

        with patch.object(cpa_import.curl_requests, "post", return_value=FakeResponse()) as posted:
            result = cpa_import.probe_cpa_codex_quota(
                {"email": "bad@example.com", "auth_index": "abc123"},
                api_url="https://cpa.example/v0/management/auth-files",
                api_token="token",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "token_invalid")
        self.assertEqual(result["status_code"], 401)
        call = posted.call_args
        self.assertEqual(call.args[0], "https://cpa.example/v0/management/api-call")
        self.assertEqual(call.kwargs["json"]["authIndex"], "abc123")
        self.assertEqual(call.kwargs["json"]["url"], cpa_import.CODEX_USAGE_URL)


if __name__ == "__main__":
    unittest.main()

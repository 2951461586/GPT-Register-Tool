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

    def test_classify_cpa_auth_file_detects_401_probe(self):
        self.assertEqual(
            cpa_import.classify_cpa_auth_file({"probe": {"status_code": 401}, "email": "a@liziai.cloud"}),
            "token_invalid",
        )
        self.assertEqual(
            cpa_import.classify_cpa_auth_file({"status": "active", "email": "a@liziai.cloud"}),
            "active",
        )

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

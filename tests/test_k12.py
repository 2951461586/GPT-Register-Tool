import unittest
from unittest.mock import Mock, patch
import base64
import json

from sms_tool import k12


def _jwt(payload):
    def part(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{part({'alg':'none'})}.{part(payload)}."


class K12Tests(unittest.TestCase):
    def test_parse_workspace_ids_dedupes_default_and_commas(self):
        self.assertEqual(
            k12.parse_workspace_ids("ws-a, ws-b\nws-a"),
            ["ws-a", "ws-b"],
        )
        self.assertEqual(k12.parse_workspace_ids(""), [k12.DEFAULT_WORKSPACE_ID])

    def test_normalize_route(self):
        self.assertEqual(k12.normalize_k12_route("accept"), "accept")
        self.assertEqual(k12.normalize_k12_route("join"), "accept")
        self.assertEqual(k12.normalize_k12_route("anything"), "request")

    def test_post_workspace_invite_uses_expected_protocol(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"ok":true}'
        session = Mock()
        session.post.return_value = response

        with patch("sms_tool.k12.curl_requests.Session", return_value=session):
            result = k12._post_workspace_invite(
                {"email": "a@example.com", "access_token": "at_123", "device_id": "did-1"},
                "workspace-1",
                route="request",
                proxy="http://127.0.0.1:8080",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(session.proxies, {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"})
        args, kwargs = session.post.call_args
        self.assertIn("/backend-api/accounts/workspace-1/invites/request", args[0])
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer at_123")
        self.assertEqual(kwargs["headers"]["oai-device-id"], "did-1")
        self.assertEqual(kwargs["data"], "")

    def test_post_workspace_invite_retries_and_refreshes_token(self):
        fail = Mock()
        fail.status_code = 401
        fail.text = '{"error":"expired"}'
        ok = Mock()
        ok.status_code = 200
        ok.text = '{"ok":true}'
        session = Mock()
        session.post.side_effect = [fail, ok]
        account = {
            "email": "a@example.com",
            "access_token": "old_at",
            "device_id": "did-1",
            "cookie_header": "session=1",
        }

        with patch("sms_tool.k12.curl_requests.Session", return_value=session), \
             patch("sms_tool.k12._refresh_access_token_from_cookie", side_effect=lambda acc, **_: acc.update({"access_token": "new_at"}) or "new_at"), \
             patch("sms_tool.k12.time.sleep"):
            result = k12._post_workspace_invite(
                account,
                "workspace-1",
                route="request",
                max_retries=1,
                retry_backoff=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args.kwargs["headers"]["authorization"], "Bearer new_at")

    def test_extract_invite_url_and_workspace_id(self):
        url = k12._extract_invite_url('open https://chatgpt.com/k12-invite?foo=1&wId=ws-123&amp;x=2 now')
        self.assertIn("k12-invite", url)
        self.assertEqual(k12._workspace_id_from_invite_url(url, ["fallback"]), "ws-123")

    def test_session_file_account_merges_mailbox_from_sqlite(self):
        with patch("sms_tool.k12._load_seed_session", return_value=(
            {"email": "a@example.com", "access_token": "at_session", "device_id": "did-session"},
            "session.json",
        )), patch("sms_tool.k12.get_account_record", return_value={
            "raw_json": '{"mailbox":{"email":"a@example.com","provider":"chatai","refresh_token":"rt","token":"client"}}',
            "json_path": "session.json",
        }):
            accounts = k12.load_k12_accounts(session_file="session.json")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["mailbox"]["provider"], "chatai")
        self.assertEqual(accounts[0]["mailbox"]["refresh_token"], "rt")

    def test_run_k12_marks_requested_before_auto_accept_failure(self):
        with patch("sms_tool.k12._post_workspace_invite", return_value={
            "ok": True,
            "email": "a@example.com",
            "workspace_id": "ws-1",
            "route": "request",
            "status": 200,
        }), patch("sms_tool.k12._accept_invite_after_request", return_value={
            "ok": False,
            "error": "missing_mailbox_credentials",
        }), patch("sms_tool.k12.mark_k12_status") as mark:
            result = k12.run_k12_batch(
                [{"email": "a@example.com", "access_token": "at"}],
                ["ws-1"],
                route="request",
                workers=1,
                auto_accept=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(mark.call_args_list[0].kwargs["status"], "k12_requested")

    def test_run_k12_marks_token_invalidated_as_at_invalid(self):
        with patch("sms_tool.k12._post_workspace_invite", return_value={
            "ok": False,
            "email": "a@example.com",
            "workspace_id": "ws-1",
            "route": "request",
            "status": 401,
            "body": '{"error":{"code":"token_invalidated"}}',
        }), patch("sms_tool.k12.mark_k12_status") as mark:
            result = k12.run_k12_batch(
                [{"email": "a@example.com", "access_token": "bad"}],
                ["ws-1"],
                route="request",
                workers=1,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(mark.call_args.kwargs["status"], "at_invalid")

    def test_build_k12_cpa_json_matches_reference_shape(self):
        access = _jwt({"exp": 1782973350, "https://api.openai.com/profile": {"email": "a@example.com"}})
        personal_id = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "personal"}})
        data = k12.build_k12_cpa_json(
            {
                "email": "a@example.com",
                "access_token": access,
                "raw": {
                    "email": "A@Example.com",
                    "id_token": personal_id,
                    "oauth_refresh_token": "rt.1.refresh",
                },
            },
            "workspace-1",
            now_iso="2026-07-02T09:13:42+08:00",
        )

        self.assertEqual(list(data.keys()), [
            "access_token",
            "account_id",
            "disabled",
            "email",
            "expired",
            "id_token",
            "last_refresh",
            "refresh_token",
            "type",
        ])
        self.assertEqual(data["account_id"], "workspace-1")
        self.assertEqual(data["email"], "A@Example.com")
        self.assertEqual(data["refresh_token"], "rt.1.refresh")
        self.assertEqual(data["last_refresh"], "2026-07-02T09:13:42+08:00")
        self.assertEqual(k12._token_account_id(data["id_token"]), "workspace-1")

    def test_run_k12_exports_cpa_json_after_joined(self):
        with patch("sms_tool.k12._post_workspace_invite", return_value={
            "ok": True,
            "email": "a@example.com",
            "workspace_id": "ws-1",
            "route": "accept",
            "status": 200,
        }), patch("sms_tool.k12.export_k12_cpa_json", return_value={
            "ok": True,
            "path": r"F:\out\codex-a@example.com-k12.json",
            "account_id": "ws-1",
        }) as export, patch("sms_tool.k12.mark_k12_status") as mark:
            result = k12.run_k12_batch(
                [{"email": "a@example.com", "access_token": "at", "raw": {"oauth_refresh_token": "rt.1.refresh"}}],
                ["ws-1"],
                route="accept",
                workers=1,
                export_dir=r"F:\out",
            )

        self.assertTrue(result["ok"])
        export.assert_called_once()
        self.assertEqual(result["results"][0]["cpa_export"]["path"], r"F:\out\codex-a@example.com-k12.json")
        self.assertEqual(mark.call_args.kwargs["result"]["cpa_export"]["account_id"], "ws-1")


if __name__ == "__main__":
    unittest.main()

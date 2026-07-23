import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.public import SealedBox
from nacl.signing import SigningKey

from sms_tool import agent_identity, registration, sub2api_import


def _jwt(claims):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class AgentIdentityTests(unittest.TestCase):
    def test_create_agent_identity_builds_valid_sub2api_payload_without_retaining_at(self):
        access_token = _jwt({
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "chatgpt_plan_type": "free",
            },
            "https://api.openai.com/profile": {"email": "free@example.com"},
        })
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"agent_runtime_id": "runtime-free"}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

        class FakeSession:
            def post(self, url, **kwargs):
                return fake_post(url, **kwargs)

            def close(self):
                pass

        with patch.object(agent_identity, "_create_agent_registration_session", return_value=FakeSession()):
            result = agent_identity.create_agent_identity({"access_token": access_token})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["auth_mode"], "agent_identity")
        self.assertEqual(result["data"]["agent_identity"]["agent_runtime_id"], "runtime-free")
        self.assertEqual(result["data"]["agent_identity"]["account_id"], "acct-free")
        self.assertTrue(agent_identity.validate_agent_identity(result["data"])["ok"])
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {access_token}")
        self.assertNotIn("capabilities", captured["json"])
        self.assertNotIn("ttl", captured["json"])
        self.assertNotIn(access_token, json.dumps(result))

    def test_agent_identity_round_trip_uses_dedicated_file(self):
        private_key = agent_identity.Ed25519PrivateKey.generate()
        private_der = private_key.private_bytes(
            agent_identity.serialization.Encoding.DER,
            agent_identity.serialization.PrivateFormat.PKCS8,
            agent_identity.serialization.NoEncryption(),
        )
        data = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-free",
                "agent_private_key": base64.b64encode(private_der).decode("ascii"),
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "email": "free@example.com",
                "plan_type": "free",
                "task_id": "must-not-be-exported",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            written = agent_identity.write_agent_identity(data, export_dir=directory)
            loaded = agent_identity.load_agent_identity("free@example.com", export_dir=directory)

            self.assertTrue(written["ok"])
            self.assertTrue(loaded["ok"])
            self.assertEqual(Path(written["path"]).name, "agent-free@example.com.json")
            self.assertEqual(loaded["data"]["agent_identity"]["agent_runtime_id"], "runtime-free")
            self.assertEqual(loaded["data"]["auth_mode"], "agent_identity")
            self.assertNotIn("task_id", loaded["data"]["agent_identity"])

    def test_provision_agent_identity_persists_new_identity(self):
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-new",
                "agent_private_key": "private-key",
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "email": "free@example.com",
                "plan_type": "free",
            },
        }
        source = {"email": "free@example.com", "access_token": "header.payload.signature"}
        with (
            patch.object(agent_identity, "build_codex_json", return_value=({"email": "free@example.com", "plan_type": "free"}, [])),
            patch.object(agent_identity, "load_agent_identity", return_value={"ok": False}),
            patch.object(agent_identity, "create_agent_identity", return_value={"ok": True, "data": auth_json, "warnings": []}) as created,
            patch.object(agent_identity, "register_agent_identity_task") as register_task,
            patch.object(agent_identity, "write_agent_identity", return_value={"ok": True, "path": "agent.json", "data": auth_json}) as written,
        ):
            result = agent_identity.provision_agent_identity(source)

        self.assertTrue(result["ok"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["path"], "agent.json")
        created.assert_called_once()
        self.assertNotIn("task_id", auth_json["agent_identity"])
        register_task.assert_not_called()
        written.assert_called_once_with(auth_json, export_dir="")

    def test_signup_provision_result_never_contains_private_key(self):
        provisioned = {
            "ok": True,
            "path": "agent.json",
            "reused": False,
            "data": {
                "auth_mode": "agentIdentity",
                "agent_identity": {
                    "agent_runtime_id": "runtime-new",
                    "agent_private_key": "private-key",
                    "plan_type": "free",
                },
            },
        }
        with (
            patch.dict(registration.CFG, {"agent_identity": {"register_on_free_signup": True, "registration_timeout": 30}}),
            patch.object(agent_identity, "provision_agent_identity", return_value=provisioned),
        ):
            result = registration._provision_signup_agent_identity(
                "free@example.com",
                "header.payload.signature",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["agent_runtime_id"], "runtime-new")
        self.assertTrue(result["private_key_validated"])
        self.assertNotIn("private-key", json.dumps(result))

    def test_provision_reuses_structurally_valid_runtime_without_registering_task(self):
        stale = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-stale",
                "agent_private_key": "private-stale",
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "email": "free@example.com",
                "plan_type": "free",
            },
        }
        with (
            patch.object(agent_identity, "build_codex_json", return_value=({"email": "free@example.com", "plan_type": "free"}, [])),
            patch.object(agent_identity, "load_agent_identity", return_value={"ok": True, "path": "stale.json", "data": stale}),
            patch.object(agent_identity, "register_agent_identity_task") as register_task,
            patch.object(agent_identity, "create_agent_identity") as created,
        ):
            result = agent_identity.provision_agent_identity({"access_token": "header.payload.signature"})

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused"])
        self.assertEqual(result["data"]["agent_identity"]["agent_runtime_id"], "runtime-stale")
        register_task.assert_not_called()
        created.assert_not_called()

    def test_register_agent_task_decrypts_encrypted_task_id(self):
        private_key = agent_identity.Ed25519PrivateKey.generate()
        private_der = private_key.private_bytes(
            agent_identity.serialization.Encoding.DER,
            agent_identity.serialization.PrivateFormat.PKCS8,
            agent_identity.serialization.NoEncryption(),
        )
        seed = private_key.private_bytes(
            agent_identity.serialization.Encoding.Raw,
            agent_identity.serialization.PrivateFormat.Raw,
            agent_identity.serialization.NoEncryption(),
        )
        encrypted = SealedBox(SigningKey(seed).to_curve25519_private_key().public_key).encrypt(b"task-encrypted")
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-encrypted",
                "agent_private_key": base64.b64encode(private_der).decode("ascii"),
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
            },
        }

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"encrypted_task_id": base64.b64encode(encrypted).decode("ascii")}

        with patch.object(agent_identity.curl_requests, "post", return_value=FakeResponse()):
            result = agent_identity.register_agent_identity_task(auth_json, disposable_canary=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], "task-encrypted")

    def test_agent_assertion_signs_runtime_task_and_timestamp(self):
        private_key = agent_identity.Ed25519PrivateKey.generate()
        private_der = private_key.private_bytes(
            agent_identity.serialization.Encoding.DER,
            agent_identity.serialization.PrivateFormat.PKCS8,
            agent_identity.serialization.NoEncryption(),
        )
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-assertion",
                "agent_private_key": base64.b64encode(private_der).decode("ascii"),
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "task_id": "task-assertion",
            },
        }

        result = agent_identity.build_agent_identity_authorization(
            auth_json,
            timestamp="2026-07-23T00:00:00Z",
        )

        self.assertTrue(result["ok"])
        encoded = result["authorization"].split(" ", 1)[1]
        envelope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        signature = base64.b64decode(envelope["signature"])
        private_key.public_key().verify(
            signature,
            b"runtime-assertion:task-assertion:2026-07-23T00:00:00Z",
        )

    def test_agent_identity_task_and_probe_require_disposable_canary(self):
        self.assertEqual(
            agent_identity.register_agent_identity_task({})["error"],
            "agent_task_registration_requires_disposable_canary",
        )
        self.assertEqual(
            agent_identity.probe_agent_identity({})["error"],
            "agent_identity_probe_requires_disposable_canary",
        )

    def test_disposable_canary_probe_registers_ephemeral_task(self):
        private_key = agent_identity.Ed25519PrivateKey.generate()
        private_der = private_key.private_bytes(
            agent_identity.serialization.Encoding.DER,
            agent_identity.serialization.PrivateFormat.PKCS8,
            agent_identity.serialization.NoEncryption(),
        )
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-probe",
                "agent_private_key": base64.b64encode(private_der).decode("ascii"),
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
            },
        }

        class FakeResponse:
            status_code = 200
            text = 'data: {"type":"response.created"}\n\ndata: {"type":"response.completed"}\n\n'

        with (
            patch.object(agent_identity, "register_agent_identity_task", return_value={"ok": True, "task_id": "task-probe"}) as register_task,
            patch.object(agent_identity.curl_requests, "post", return_value=FakeResponse()) as post,
        ):
            result = agent_identity.probe_agent_identity(auth_json, disposable_canary=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["event"], "response.completed")
        self.assertTrue(result["consumed"])
        register_task.assert_called_once()
        self.assertTrue(register_task.call_args.kwargs["disposable_canary"])
        self.assertEqual(post.call_args.kwargs["json"]["reasoning"]["summary"], "auto")

    def test_agent_identity_probe_surfaces_nested_api_error(self):
        private_key = agent_identity.Ed25519PrivateKey.generate()
        private_der = private_key.private_bytes(
            agent_identity.serialization.Encoding.DER,
            agent_identity.serialization.PrivateFormat.PKCS8,
            agent_identity.serialization.NoEncryption(),
        )
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-probe",
                "agent_private_key": base64.b64encode(private_der).decode("ascii"),
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "task_id": "task-probe",
            },
        }

        class FakeResponse:
            status_code = 400
            text = '{"error":{"message":"invalid reasoning summary","type":"invalid_request_error"}}'

            @staticmethod
            def json():
                return {"error": {"message": "invalid reasoning summary", "type": "invalid_request_error"}}

        with (
            patch.object(agent_identity, "register_agent_identity_task", return_value={"ok": True, "task_id": "task-probe"}),
            patch.object(agent_identity.curl_requests, "post", return_value=FakeResponse()),
        ):
            result = agent_identity.probe_agent_identity(auth_json, disposable_canary=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "agent_identity_probe_http_400")
        self.assertEqual(result["message"], "invalid reasoning summary")

    def test_rebuild_uses_codex_rt_then_chatgpt_session_before_stored_at(self):
        source = {"email": "free@example.com", "access_token": "stored.at.token"}
        codex_source = {"email": "free@example.com", "access_token": "codex.rt.token"}
        session_source = {"email": "free@example.com", "access_token": "chatgpt.session.token"}
        identity = {
            "auth_mode": "agent_identity",
            "agent_identity": {"email": "free@example.com", "agent_runtime_id": "runtime-new"},
        }

        def create(candidate, **kwargs):
            token = candidate["access_token"]
            if token == "codex.rt.token":
                return {"ok": False, "error": "agent_registration_http_401"}
            if token == "chatgpt.session.token":
                return {"ok": True, "data": identity}
            self.fail("stored AT must not be attempted after session succeeds")

        with (
            patch.object(agent_identity, "_refresh_agent_identity_codex_rt", return_value=(codex_source, "")),
            patch.object(agent_identity, "_refresh_agent_identity_chatgpt_session", return_value=(session_source, "session.json", "")),
            patch.object(agent_identity, "create_agent_identity", side_effect=create) as created,
            patch.object(agent_identity, "write_agent_identity", return_value={"ok": True, "path": "agent.json", "data": identity}),
        ):
            result = agent_identity.rebuild_agent_identity(source_data=source)

        self.assertTrue(result["ok"])
        self.assertEqual(result["token_source"], "chatgpt_session")
        self.assertEqual([item["source"] for item in result["attempts"]], ["codex_rt", "chatgpt_session"])
        self.assertEqual(created.call_count, 2)

    def test_auto_mode_prefers_agent_identity_for_free_account(self):
        auth_json = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-free",
                "agent_private_key": "private-key",
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "email": "free@example.com",
                "plan_type": "free",
            },
        }
        source = {
            "access_token": "header.payload.signature",
            "email": "free@example.com",
            "account_id": "acct-free",
            "plan_type": "free",
        }
        with (
            patch.object(sub2api_import, "load_agent_identity", return_value={"ok": False}),
            patch.object(sub2api_import, "_load_cpa_source", return_value={"ok": True, "data": source, "path": "session.json", "mode": "session_json"}),
            patch.object(sub2api_import, "build_codex_json", return_value=({"access_token": source["access_token"], "email": source["email"], "account_id": source["account_id"], "plan_type": "free"}, [])),
            patch.object(sub2api_import, "create_agent_identity", return_value={"ok": True, "data": auth_json, "warnings": []}) as created,
            patch.object(sub2api_import, "write_agent_identity", return_value={"ok": True, "path": "agent.json", "data": auth_json}),
        ):
            result = sub2api_import._prepare_sub2api_import_data("free@example.com", auth_mode="auto")

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_mode"], "agent_identity")
        self.assertEqual(result["data"], auth_json)
        created.assert_called_once()

    def test_agent_identity_upload_only_verifies_remote_config(self):
        def fake_request(origin, path, token="", method="GET", body=None, timeout=30):
            if path == "/api/v1/admin/groups/all":
                return {"ok": True, "data": [{"id": 7, "name": "codex", "platform": "openai"}]}
            if path == "/api/v1/admin/accounts/import/codex-session":
                return {
                    "ok": True,
                    "status_code": 200,
                    "data": {
                        "created": 1,
                        "updated": 0,
                        "failed": 0,
                        "items": [{"action": "created", "account_id": 42}],
                    },
                }
            if path == "/api/v1/admin/accounts/42":
                return {"ok": True, "data": {"id": 42, "group_ids": [7], "status": "active"}}
            return {"ok": False, "error": "unexpected"}

        with (
            patch.object(sub2api_import, "_request_json", side_effect=fake_request),
            patch.object(sub2api_import, "_request_sub2api_test", return_value={"ok": True, "event": "test_complete"}) as tested,
        ):
            result = sub2api_import.upload_to_sub2api(
                {"auth_mode": "agent_identity", "agent_identity": {"email": "free@example.com"}},
                origin="https://sub.example",
                api_token="jwt-token",
                group_name="codex",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verification"]["ok"])
        self.assertTrue(result["verification"]["structural_only"])
        self.assertFalse(result["verification"]["execution_tested"])
        tested.assert_not_called()

    def test_agent_identity_payload_is_preserved_and_named_from_nested_email(self):
        token_data = {
            "auth_mode": "agentIdentity",
            "agent_identity": {
                "agent_runtime_id": "runtime-free",
                "agent_private_key": "private-key",
                "account_id": "acct-free",
                "chatgpt_user_id": "user-free",
                "email": "free@example.com",
            },
        }

        payload = sub2api_import._build_sub2api_payload(token_data, group_ids=[7])

        self.assertEqual(payload["name"], "free@example.com")
        self.assertEqual(json.loads(payload["content"]), token_data)

    def test_sub2api_sse_test_requires_successful_final_event(self):
        class FakeResponse:
            status_code = 200
            text = 'data: {"type":"message","message":"ok"}\n\ndata: {"type":"test_complete","success":true}\n\n'

        with patch.object(sub2api_import.curl_requests, "request", return_value=FakeResponse()):
            result = sub2api_import._request_sub2api_test("https://sub.example", "jwt-token", 42)

        self.assertTrue(result["ok"])
        self.assertEqual(result["event"], "test_complete")


if __name__ == "__main__":
    unittest.main()

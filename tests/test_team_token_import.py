import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool.team_token_import import (
    _extract_kyl_fingerprint_from_har,
    _prepare_kyl_cookie_path,
    cleanup_team_runtime_files,
    default_team_script_path,
    default_kyl_runner_dir,
    discover_team_token_files,
    import_team_tokens,
    normalize_team_token,
    run_kyl_protocol_and_import,
    run_team_script_and_import,
)


class TeamTokenImportTests(unittest.TestCase):
    def test_normalize_team_token_maps_chatgpt_team_fields(self):
        data = {
            "type": "codex",
            "email": "team@example.com",
            "refresh_token": "rt_team",
            "access_token": "at_team",
            "id_token": "id_team",
            "token_source": "ChatGPT_team",
        }

        normalized = normalize_team_token(data)

        self.assertEqual(normalized["email"], "team@example.com")
        self.assertEqual(normalized["access_token"], "at_team")
        self.assertEqual(normalized["oauth_refresh_token"], "rt_team")
        self.assertEqual(normalized["refresh_token_status"], "oauth_present")
        self.assertEqual(normalized["team_token_import"]["source"], "ChatGPT_team.py")

    def test_normalize_team_token_can_mark_kyl_source(self):
        normalized = normalize_team_token(
            {
                "email": "kyl@example.com",
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": "id",
            },
            source="KYL Protocol Runner",
        )

        self.assertEqual(normalized["team_token_import"]["source"], "KYL Protocol Runner")

    def test_import_team_tokens_writes_normalized_session_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / "codex_tokens"
            token_dir.mkdir()
            token_path = token_dir / "team@example.com.json"
            token_path.write_text(
                json.dumps(
                    {
                        "email": "team@example.com",
                        "refresh_token": "rt_team",
                        "access_token": "at_team",
                        "id_token": "id_team",
                    }
                ),
                encoding="utf-8",
            )
            export_dir = Path(tmp) / "exports"
            with patch("sms_tool.team_token_import.upsert_account", return_value=True):
                with patch(
                    "sms_tool.team_token_import.import_account_session",
                    return_value={"ok": True, "email": "team@example.com"},
                ) as imported:
                    result = import_team_tokens(
                        token_dir=str(token_dir),
                        target="cliproxyapi",
                        export_dir=str(export_dir),
                    )

            self.assertTrue(result["ok"])
            imported.assert_called_once()
            self.assertEqual(imported.call_args.args[0], "cliproxyapi")
            normalized_path = Path(imported.call_args.kwargs["session_file"])
            self.assertTrue(normalized_path.exists())
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            self.assertEqual(normalized["oauth_refresh_token"], "rt_team")

    def test_discover_team_token_files_from_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "a.json"
            token_path.write_text("{}", encoding="utf-8")
            list_path = Path(tmp) / "files.txt"
            list_path.write_text(str(token_path), encoding="utf-8")
            self.assertEqual(discover_team_token_files(token_file_list=str(list_path)), [token_path])

    def test_cli_import_team_tokens_routes_to_module(self):
        args = SimpleNamespace(
            run_team_script=False,
            team_channel="chatgpt_team",
            team_script="",
            team_total=None,
            team_workers=None,
            team_script_timeout=1800,
            team_output="",
            count=1,
            proxy=None,
            team_token_dir="tokens",
            team_token_file=[],
            team_token_file_list="",
            import_target="cpa",
            codex_export_dir="",
            workers=4,
            cpa_api_url="",
            cpa_api_token="",
            sub2api_url="",
            sub2api_token="",
            sub2api_email="",
            sub2api_password="",
            sub2api_group="",
            sub2api_group_ids="",
            sub2api_proxy="",
            sub2api_proxy_id=None,
            sub2api_priority=None,
            sub2api_concurrency=None,
            kyl_state_path="",
            kyl_cookie_path="",
            kyl_har_path="",
            kyl_fingerprint="",
            kyl_start=0,
            kyl_runner_dir="",
            kyl_runtime_dir="",
            kyl_auth_dir="",
            kyl_include_existing=False,
        )
        with patch("sms_tool.team_token_import.import_team_tokens", return_value={"ok": True}) as imported:
            cli._import_team_tokens(args)

        imported.assert_called_once()
        self.assertEqual(imported.call_args.kwargs["token_dir"], "tokens")

    def test_cli_import_team_tokens_routes_kyl_channel(self):
        args = SimpleNamespace(
            run_team_script=True,
            team_channel="kyl_protocol",
            team_script="",
            team_total=5,
            team_workers=2,
            team_script_timeout=1800,
            team_output="",
            count=1,
            proxy="socks5h://127.0.0.1:7897",
            team_token_dir="",
            team_token_file=[],
            team_token_file_list="",
            import_target="cpa",
            codex_export_dir="",
            workers=4,
            cpa_api_url="",
            cpa_api_token="",
            sub2api_url="",
            sub2api_token="",
            sub2api_email="",
            sub2api_password="",
            sub2api_group="",
            sub2api_group_ids="",
            sub2api_proxy="",
            sub2api_proxy_id=None,
            sub2api_priority=None,
            sub2api_concurrency=None,
            kyl_state_path="state.json",
            kyl_cookie_path="cookies.json",
            kyl_har_path="chatgpt.com-1.har",
            kyl_fingerprint="fp",
            kyl_start=3,
            kyl_runner_dir="runner",
            kyl_runtime_dir="runtime",
            kyl_auth_dir="auths",
            kyl_include_existing=True,
        )
        with patch("sms_tool.team_token_import.run_kyl_protocol_and_import", return_value={"ok": True}) as run_kyl:
            cli._import_team_tokens(args)

        run_kyl.assert_called_once()
        self.assertEqual(run_kyl.call_args.kwargs["state_path"], "state.json")
        self.assertEqual(run_kyl.call_args.kwargs["har_path"], "chatgpt.com-1.har")
        self.assertEqual(run_kyl.call_args.kwargs["total"], 5)
        self.assertEqual(run_kyl.call_args.kwargs["protocol_workers"], 2)
        self.assertTrue(run_kyl.call_args.kwargs["include_existing"])

    def test_run_team_script_and_import_imports_generated_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "ChatGPT_team.py"
            script.write_text(
                "import argparse, json, pathlib\n"
                "p=argparse.ArgumentParser(); p.add_argument('--total', type=int); p.add_argument('--workers'); p.add_argument('--output'); args=p.parse_args()\n"
                "d=pathlib.Path('codex_tokens'); d.mkdir(exist_ok=True)\n"
                "for i in range(args.total): (d / f'user{i}@example.com.json').write_text(json.dumps({'email':f'user{i}@example.com','access_token':'at','refresh_token':'rt','id_token':'id'}))\n"
                "print('done')\n",
                encoding="utf-8",
            )
            with patch("sms_tool.team_token_import.upsert_account", return_value=True):
                with patch("sms_tool.team_token_import.import_account_session", return_value={"ok": True}) as imported:
                    result = run_team_script_and_import(
                        script_path=str(script),
                        total=2,
                        script_workers=1,
                        target="cpa",
                        timeout=60,
                    )

            self.assertTrue(result["ok"])
            self.assertEqual(len(result["generated_token_files"]), 2)
            self.assertEqual(imported.call_count, 2)
            for generated in result["generated_token_files"]:
                self.assertFalse(Path(generated).exists())
            self.assertGreaterEqual(len(result["cleanup"]["removed"]), 2)

    def test_cleanup_team_runtime_files_removes_token_and_session_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_dir = root / "codex_tokens"
            session_dir = root / "chatgpt_sessions"
            token_dir.mkdir()
            session_dir.mkdir()
            (token_dir / "a.json").write_text("{}", encoding="utf-8")
            (session_dir / "b.json").write_text("{}", encoding="utf-8")
            result = cleanup_team_runtime_files(root)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["removed"]), 2)
            self.assertFalse(token_dir.exists())
            self.assertFalse(session_dir.exists())

    def test_default_team_script_path_points_to_project_scripts(self):
        self.assertTrue(default_team_script_path().replace("\\", "/").endswith("/scripts/ChatGPT_team.py"))

    def test_default_kyl_runner_dir_points_to_project_scripts(self):
        self.assertTrue(default_kyl_runner_dir().replace("\\", "/").endswith("/scripts/kyl_protocol_runner"))

    def test_kyl_har_extracts_cookies_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            har = root / "chatgpt.har"
            har.write_text(
                json.dumps(
                    {
                        "log": {
                            "entries": [
                                {
                                    "request": {
                                        "url": "https://invite.kyl23333.xyz/api/v1/challenge/restore",
                                        "cookies": [{"name": "sid", "value": "cookie_value", "domain": "invite.kyl23333.xyz", "path": "/"}],
                                        "postData": {"text": json.dumps({"fingerprint": "kyl-fp-test-123"})},
                                    },
                                    "response": {"cookies": [], "content": {"text": ""}},
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_extract_kyl_fingerprint_from_har(har), "kyl-fp-test-123")
            info = _prepare_kyl_cookie_path(har_path=str(har), runtime_root=root / "runtime")

            self.assertEqual(info["fingerprint"], "kyl-fp-test-123")
            cookies = json.loads(Path(info["path"]).read_text(encoding="utf-8"))["cookies"]
            self.assertEqual(cookies[0]["name"], "sid")

    def test_run_kyl_protocol_and_import_imports_generated_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            state.write_text(json.dumps({"fingerprint": "fp", "accounts": [{"email": "kyl@example.com", "sub": "sub"}]}), encoding="utf-8")
            runner = root / "runner"
            runner.mkdir()
            (runner / "kyl_protocol_replay.py").write_text("# placeholder\n", encoding="utf-8")
            (runner / "run_kyl_protocol_batch.js").write_text(
                "import json, pathlib, sys\n"
                "args=sys.argv[1:]\n"
                "auth=pathlib.Path(args[args.index('--auth-dir')+1]); auth.mkdir(parents=True, exist_ok=True)\n"
                "(auth/'codex-kyl@example.com.json').write_text(json.dumps({'email':'kyl@example.com','access_token':'at','refresh_token':'rt','id_token':'id'}), encoding='utf-8')\n"
                "print('{\"event\":\"protocolBatchStop\",\"done\":1,\"failed\":0}')\n",
                encoding="utf-8",
            )

            with patch("sms_tool.team_token_import._node_executable", return_value=sys.executable):
                with patch("sms_tool.team_token_import.upsert_account", return_value=True):
                    with patch("sms_tool.team_token_import.import_account_session", return_value={"ok": True}) as imported:
                        result = run_kyl_protocol_and_import(
                            state_path=str(state),
                            runner_dir=str(runner),
                            total=1,
                            timeout=60,
                            target="cpa",
                        )

            self.assertTrue(result["ok"])
            self.assertEqual(result["channel"], "kyl_protocol")
            imported.assert_called_once()
            normalized_path = Path(imported.call_args.kwargs["session_file"])
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            self.assertEqual(normalized["team_token_import"]["source"], "KYL Protocol Runner")
            for generated in result["generated_token_files"]:
                self.assertFalse(Path(generated).exists())


if __name__ == "__main__":
    unittest.main()

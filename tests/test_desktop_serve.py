"""Offline tests for the resident desktop-read server."""

import io
import json
import unittest
from unittest.mock import patch

from sms_tool import desktop_serve


def _fake_payload(op, request):
    if op == "accounts":
        return {"ok": True, "accounts": [{"id": "1", "email": "a@example.test"}]}
    if op == "pools":
        return {"ok": True, "accounts": [{"id": "1"}], "files": [{"path": "x", "lines": []}]}
    if op == "account":
        return {"ok": True, "account": {"id": request.get("account_id")}}
    raise ValueError(f"unexpected op {op}")


class DesktopServeTests(unittest.TestCase):
    def test_handle_request_dispatches_and_sanitizes(self):
        with patch.object(desktop_serve, "_payload_for", _fake_payload):
            response = desktop_serve.handle_request({"id": 7, "op": "accounts"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["payload"]["accounts"][0]["email"], "a@example.test")

    def test_error_response_carries_request_id(self):
        response = desktop_serve.handle_request({"id": 3, "op": "nope"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["id"], 3)
        self.assertIn("unknown desktop-serve op", response["error"])

    def test_malformed_json_yields_error_without_killing_loop(self):
        self.assertFalse(desktop_serve.handle_request(None)["ok"])

    def test_serve_forever_round_trips_lines_until_eof(self):
        requests = (
            json.dumps({"id": 1, "op": "accounts"}) + "\n"
            + json.dumps({"id": 2, "op": "pools"}) + "\n"
            + "\n"
        )
        stdin = io.StringIO(requests)
        stdout = io.StringIO()
        with patch.object(desktop_serve, "_payload_for", _fake_payload):
            code = desktop_serve.serve_forever(stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in lines], [1, 2])
        self.assertTrue(lines[1]["payload"]["files"][0]["path"] == "x")

    def test_pools_payload_merges_accounts_and_mailbox(self):
        captured = {}

        def fake_pool(config, extra_files):
            captured["extra_files"] = extra_files
            return {"files": [{"path": "mailbox.txt", "lines": []}]}

        def fake_accounts(config):
            return [{"id": "9"}]

        with patch.object(desktop_serve, "read_mailbox_pool", fake_pool), \
                patch.object(desktop_serve, "read_accounts", fake_accounts), \
                patch.object(desktop_serve, "load_runtime_config", lambda: {}):
            payload = desktop_serve._payload_for("pools", {"extra_files": ["mailbox.txt"]})
        self.assertEqual(captured["extra_files"], ("mailbox.txt",))
        self.assertEqual(payload["accounts"], [{"id": "9"}])
        self.assertEqual(payload["files"][0]["path"], "mailbox.txt")


if __name__ == "__main__":
    unittest.main()

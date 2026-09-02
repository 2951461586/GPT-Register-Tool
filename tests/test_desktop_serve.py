"""Offline tests for the resident desktop-read server."""

import io
import json
import os
import threading
import time
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
            code = desktop_serve.serve_forever(stdin=stdin, stdout=stdout, watchdog_seconds=0)
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


class DesktopServeProtocolTests(unittest.TestCase):
    """Version negotiation, liveness probe and machine-readable error codes.

    These mirror ``SmsWorkbench.Contracts/DesktopReadProtocol.cs``; if the two
    drift, the client silently falls back to one-shot reads, which is exactly
    the failure this pair of files exists to make visible.
    """

    def test_hello_reports_protocol_version_without_touching_config(self):
        def explode():
            raise AssertionError("hello must not resolve runtime config")

        with patch.object(desktop_serve, "load_runtime_config", explode):
            payload = desktop_serve._payload_for("hello", {})
        self.assertEqual(payload["protocol"], desktop_serve.PROTOCOL_VERSION)
        self.assertIn("accounts", payload["ops"])
        self.assertIn("ping", payload["ops"])

    def test_hello_ops_match_the_supported_tuple(self):
        payload = desktop_serve._payload_for("hello", {})
        self.assertEqual(list(payload["ops"]), list(desktop_serve.SUPPORTED_OPS))

    def test_ping_is_cheap_and_side_effect_free(self):
        def explode():
            raise AssertionError("ping must not resolve runtime config")

        with patch.object(desktop_serve, "load_runtime_config", explode):
            self.assertEqual(desktop_serve._payload_for("ping", {}), {"ok": True, "pong": True})

    def test_unknown_op_is_classified_for_the_client(self):
        response = desktop_serve.handle_request({"id": 5, "op": "definitely-not-an-op"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "unknown_operation")
        self.assertEqual(response["id"], 5)

    def test_non_object_request_is_bad_request(self):
        response = desktop_serve.handle_request(["not", "an", "object"])
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "bad_request")

    def test_handler_exception_is_classified_as_backend_error(self):
        with patch.object(desktop_serve, "load_runtime_config", side_effect=RuntimeError("boom")):
            response = desktop_serve.handle_request({"id": 9, "op": "accounts"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "backend_error")
        self.assertIn("RuntimeError", response["error"])

    def test_protocol_version_matches_the_csharp_contract(self):
        """Guard against the two sides drifting apart silently.

        The C# constant is the single source of truth; this asserts the Python
        side still agrees with whatever is checked into the repo.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        contract = os.path.join(root, "SmsWorkbench.Contracts", "DesktopReadProtocol.cs")
        if not os.path.exists(contract):
            self.skipTest("DesktopReadProtocol.cs not present in this checkout")
        with open(contract, encoding="utf-8") as handle:
            text = handle.read()
        marker = "public const int Version = "
        start = text.index(marker) + len(marker)
        version = int(text[start:].split(";", 1)[0].strip())
        self.assertEqual(
            desktop_serve.PROTOCOL_VERSION,
            version,
            "sms_tool/desktop_serve.py PROTOCOL_VERSION must match "
            "SmsWorkbench.Contracts/DesktopReadProtocol.cs Version",
        )


class DesktopServeWatchdogTests(unittest.TestCase):
    def test_watchdog_is_off_when_seconds_is_zero(self):
        watchdog = desktop_serve._Watchdog(io.StringIO(), 0)
        try:
            self.assertFalse(watchdog.enabled)
            watchdog.arm(1)
            time.sleep(0.05)
        finally:
            watchdog.stop()

    def test_watchdog_writes_a_coded_response_then_hard_exits(self):
        """A wedged handler must not leave the process alive but useless."""
        writer = io.StringIO()
        exited = threading.Event()
        captured: dict[str, object] = {}

        def fake_exit(code):
            captured["code"] = code
            exited.set()

        with patch.object(desktop_serve, "_hard_exit", fake_exit):
            watchdog = desktop_serve._Watchdog(writer, 0.05)
            try:
                watchdog.arm(42)
                self.assertTrue(exited.wait(5), "watchdog never fired")
            finally:
                watchdog.stop()
        self.assertEqual(captured["code"], desktop_serve.WATCHDOG_EXIT_CODE)
        response = json.loads(writer.getvalue().strip().splitlines()[-1])
        self.assertEqual(response["id"], 42)
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "watchdog_timeout")

    def test_watchdog_does_not_fire_when_the_request_returns(self):
        writer = io.StringIO()
        exited = threading.Event()
        with patch.object(desktop_serve, "_hard_exit", lambda _code: exited.set()):
            watchdog = desktop_serve._Watchdog(writer, 0.2)
            try:
                watchdog.arm(1)
                watchdog.disarm()
                time.sleep(0.4)
            finally:
                watchdog.stop()
        self.assertFalse(exited.is_set(), "watchdog fired on a completed request")
        self.assertEqual(writer.getvalue(), "")

    def test_serve_forever_disarms_between_requests(self):
        """The deadline must not survive into the next request."""
        exited = threading.Event()
        requests = json.dumps({"id": 1, "op": "ping"}) + "\n"
        with patch.object(desktop_serve, "_hard_exit", lambda _code: exited.set()):
            code = desktop_serve.serve_forever(
                stdin=io.StringIO(requests), stdout=io.StringIO(), watchdog_seconds=0.2
            )
        self.assertEqual(code, 0)
        self.assertFalse(exited.is_set())

    def test_watchdog_seconds_resolve_from_environment(self):
        with patch.dict(os.environ, {"SMS_DESKTOP_SERVE_WATCHDOG": "7"}):
            self.assertEqual(desktop_serve._resolve_watchdog_seconds(None), 7.0)
        with patch.dict(os.environ, {"SMS_DESKTOP_SERVE_WATCHDOG": "not-a-number"}):
            self.assertEqual(
                desktop_serve._resolve_watchdog_seconds(None),
                desktop_serve.DEFAULT_WATCHDOG_SECONDS,
            )
        self.assertEqual(desktop_serve._resolve_watchdog_seconds(0), 0.0)


if __name__ == "__main__":
    unittest.main()

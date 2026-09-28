"""Offline tests for the vendored UPI Sentinel bridge wiring.

Covers the two pieces added for the reference-aligned approve fix:

* ``sms_tool.upi_link._vendor.upi_sentinel.mint_sentinel`` — subprocess
  contract (success / missing node / bad JSON / bridge error).
* ``sms_tool.upi_link.sentinel._upi_sentinel_headers`` — composes the
  ``openai-sentinel-token`` + ``openai-sentinel-so-token`` pair, and falls back
  to a ``chatgpt_checkout`` mint for the SO when a non-checkout flow yields none.

No network and no Node are used.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from sms_tool.upi_link import sentinel as upi_sentinel
from sms_tool.upi_link._vendor import upi_sentinel as vendor_sentinel
from sms_tool.upi_link.constants import UPI_SENTINEL_APPROVAL_FLOW, UPI_SENTINEL_CHECKOUT_FLOW

# --------------------------------------------------------------- mint_sentinel


class _Completed:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_mint_sentinel_parses_bridge_output():
    payload = {"main": '{"p":"x","t":"y","c":"z","id":"d","flow":"chatgpt_checkout"}', "so": '{"so":"s","c":"z"}', "hasT": True, "hasSo": True}
    with patch.object(vendor_sentinel.subprocess, "run", return_value=_Completed(json.dumps(payload).encode())):
        result = vendor_sentinel.mint_sentinel(flow="chatgpt_checkout", device_id="d", user_agent="ua")
    assert result["main"].startswith('{"p"')
    assert result["so"]
    assert result["has_t"] is True and result["has_so"] is True


def test_mint_sentinel_reports_node_missing():
    with patch.object(vendor_sentinel.subprocess, "run", side_effect=FileNotFoundError):
        assert vendor_sentinel.mint_sentinel(flow="f", device_id="d", user_agent="ua") == {"error": "sentinel_node_missing"}


def test_mint_sentinel_reports_timeout():
    with patch.object(
        vendor_sentinel.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="node", timeout=1),
    ):
        assert vendor_sentinel.mint_sentinel(flow="f", device_id="d", user_agent="ua") == {
            "error": "sentinel_bridge_timeout"
        }


def test_mint_sentinel_reports_bad_json_and_bridge_error():
    with patch.object(vendor_sentinel.subprocess, "run", return_value=_Completed(b"not-json")):
        assert vendor_sentinel.mint_sentinel(flow="f", device_id="d", user_agent="ua")["error"].startswith("sentinel_bridge_bad_json")
    with patch.object(
        vendor_sentinel.subprocess, "run", return_value=_Completed(json.dumps({"error": "proto2: boom"}).encode())
    ):
        assert vendor_sentinel.mint_sentinel(flow="f", device_id="d", user_agent="ua") == {"error": "proto2: boom"}


# ------------------------------------------------------- _upi_sentinel_headers


class _FakeSession:
    def __init__(self, cookie: str = ""):
        self.headers = {"User-Agent": "ua", "Cookie": cookie} if cookie else {"User-Agent": "ua"}


def _call(flow, minted, *, session=None, fingerprint=None):
    with (
        patch.object(upi_sentinel, "_upi_session_is_live", return_value=True),
        patch.object(upi_sentinel, "_upi_mint_sentinel_via_bridge", side_effect=minted),
    ):
        return upi_sentinel._upi_sentinel_headers(
            session or _FakeSession(cookie="oai-did=d; __Secure-next-auth.session-token=s"),
            "d",
            "http://proxy",
            flow=flow,
            fingerprint=fingerprint or {"user_agent": "ua", "locale": "en-IN", "timezone": "Asia/Kolkata"},
        )


def test_headers_carry_main_and_so():
    headers = _call(UPI_SENTINEL_CHECKOUT_FLOW, [{"main": "MAIN", "so": "SO", "has_t": True, "has_so": True}])
    assert headers == {"OpenAI-Sentinel-Token": "MAIN", "OpenAI-Sentinel-SO-Token": "SO"}


def test_approval_flow_reuses_checkout_so_when_missing():
    # First mint (approval) has no SO; the second mint (checkout) supplies it.
    headers = _call(
        UPI_SENTINEL_APPROVAL_FLOW,
        [{"main": "APPR", "so": ""}, {"main": "CO", "so": "CO_SO"}],
    )
    assert headers == {"OpenAI-Sentinel-Token": "APPR", "OpenAI-Sentinel-SO-Token": "CO_SO"}


def test_bridge_error_falls_back_to_registration_runner():
    issued = type("Issued", (), {"token": "LEGACY", "so_token": "LEGACY_SO"})()
    with (
        patch.object(upi_sentinel, "_upi_session_is_live", return_value=True),
        patch.object(upi_sentinel, "_upi_mint_sentinel_via_bridge", return_value={"error": "sentinel_node_missing"}),
        patch("sms_tool.sentinel.issue_sentinel_flow", return_value=issued),
    ):
        headers = upi_sentinel._upi_sentinel_headers(
            _FakeSession(), "d", "http://proxy", flow=UPI_SENTINEL_APPROVAL_FLOW
        )
    assert headers == {"OpenAI-Sentinel-Token": "LEGACY", "OpenAI-Sentinel-SO-Token": "LEGACY_SO"}

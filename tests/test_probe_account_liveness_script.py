import importlib.util
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from sms_tool.account_liveness import CODEX_USAGE_URL


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "probe_account_liveness.py"
SPEC = importlib.util.spec_from_file_location("probe_account_liveness_script", SCRIPT_PATH)
LIVENESS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LIVENESS)


def test_liveness_script_uses_canonical_wham_usage_endpoint():
    assert LIVENESS.LIVENESS_ENDPOINT == CODEX_USAGE_URL
    assert "/backend-api/me" not in SCRIPT_PATH.read_text(encoding="utf-8")


def test_liveness_script_runs_by_file_path():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=SCRIPT_PATH.parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "/backend-api/wham/usage" in result.stdout


def test_probe_account_delegates_to_canonical_quota_probe():
    account = {"email": "user@example.com", "access_token": "at_123"}
    expected = {"ok": True, "status": "active", "status_code": 200}

    with patch.object(LIVENESS, "probe_account_liveness", return_value=expected) as probe:
        result = LIVENESS.probe_account(account, proxy="http://proxy.example:8080", timeout=25)

    assert result == expected
    probe.assert_called_once_with(account, proxy="http://proxy.example:8080", timeout=25, browser_fetch=None)


def test_classify_probe_matches_desktop_liveness_semantics():
    assert LIVENESS.classify_probe({"ok": True, "status_code": 200}) == "alive"
    assert LIVENESS.classify_probe({"ok": False, "status": "token_invalid", "status_code": 401}) == "unauthorized"
    assert LIVENESS.classify_probe({"ok": False, "status": "account_deactivated"}) == "deactivated"
    assert LIVENESS.classify_probe({"ok": False, "status_code": 403}) == "forbidden"

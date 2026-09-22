import json
import os
import re
import subprocess
import sys
from pathlib import Path
import pytest
from sms_tool.config import ConfigError, current_config_data, load_runtime_config, runtime_config_scope, validate_config
from sms_tool import storage


def _base_config():
    return {"chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"}, "proxy": {"pool": []}, "registration": {"at_probe_timeout_seconds": 30}, "protocol_payments": {"enabled_methods": ["paypal"], "matrix": {"cells": []}}}


def test_config_import_performs_no_file_io():
    root = os.path.dirname(os.path.dirname(__file__))
    command = (
        "from pathlib import Path; "
        "Path.read_text=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('import-time read')); "
        "import sms_tool.config"
    )
    subprocess.run(
        [sys.executable, "-c", command],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_example_config_omits_retired_paypal_auto_section():
    root = Path(__file__).resolve().parent.parent
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert "paypal_auto" not in example


def test_explicit_config_is_immutable_and_independent_of_cwd(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_base_config()), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    config = load_runtime_config(path)
    assert config.source == path.resolve()
    with pytest.raises(TypeError):
        config.data["chatgpt"] = {}


def test_payment_schema_rejects_unknown_method_and_country(tmp_path):
    value = _base_config()
    value["protocol_payments"] = {"enabled_methods": ["not-a-method"], "matrix": {"cells": [{"registration_country": "USA"}]}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported protocol payment methods"):
        load_runtime_config(path)


def test_registration_schema_rejects_unknown_stage_timeout(tmp_path):
    value = _base_config()
    value["registration"]["stage_timeouts"] = {"mystery_stage": 10}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported registration stage timeout"):
        load_runtime_config(path)


def test_registration_schema_rejects_retired_browser_profile_pool(tmp_path):
    value = _base_config()
    value["registration"]["browser_profile_pool"] = {
        "profiles": [{"screen_width": 1440}]
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigError, match="browser hardware profiles are built in"):
        load_runtime_config(path)


def test_registration_schema_rejects_non_boolean_pulse_canary(tmp_path):
    value = _base_config()
    value["registration"]["pulse"] = {"canary_enabled": "yes"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(
        ConfigError, match=r"registration\.pulse\.canary_enabled must be a boolean"
    ):
        load_runtime_config(path)


@pytest.mark.parametrize(
    "key",
    ["cross_batch_cooldown_seconds", "otp_pending_quarantine_threshold"],
)
def test_registration_schema_rejects_non_positive_retry_policy(tmp_path, key):
    value = _base_config()
    value["registration"]["retry_policy"] = {key: 0}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(
        ConfigError,
        match=rf"registration\.retry_policy\.{key} must be a positive number",
    ):
        load_runtime_config(path)


def test_payment_matrix_validates_method_country_and_sample_size(tmp_path):
    value = _base_config()
    value["protocol_payments"]["matrix"]["cells"] = [{
        "name": "wrong",
        "payment_method": "gopay",
        "checkout_country": "PH",
        "sample_size": 0,
    }]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigError, match="sample_size must be a positive integer"):
        load_runtime_config(path)


def test_runtime_config_scope_injects_and_restores_config():
    original = current_config_data()
    injected = _base_config()
    injected["email_registration"] = {"otp_timeout": 17}
    with runtime_config_scope(injected, workflow="registration"):
        assert current_config_data()["email_registration"]["otp_timeout"] == 17
    assert current_config_data() is original


@pytest.mark.parametrize(
    "key,value,match",
    [
        ("cfworker_url", "not-a-url", "cfworker_url must be an http(s) URL"),
        ("graph_messages_url", "ftp://x", "graph_messages_url must be an http(s) URL"),
        ("oauth_token_url", "oauth", "oauth_token_url must be an http(s) URL"),
        ("use_as_username", "yes", "use_as_username must be a boolean"),
        ("sentinel_max_concurrency", -1, "sentinel_max_concurrency must be a non-negative number"),
        ("sentinel_prewarm_window", -1, "sentinel_prewarm_window must be a non-negative number"),
        ("gmail", "not-an-object", "gmail must be an object"),
    ],
)
def test_email_registration_schema_rejects_bad_shapes(key, value, match):
    """2026-09-19: these keys used to sail through validate_config untouched and
    only fail at first use deep inside a registration batch."""
    cfg = _base_config()
    cfg.setdefault("email_registration", {})[key] = value
    with pytest.raises(ConfigError, match=re.escape(match)):
        validate_config(cfg)


def test_storage_runtime_config_controls_every_database_operation(tmp_path):
    injected = _base_config()
    database = tmp_path / "injected.sqlite3"
    injected["storage"] = {"sqlite_path": str(database)}

    assert storage.upsert_account(
        {"email": "injected@example.com", "access_token": "secret", "success": True},
        runtime_config=injected,
    )
    assert storage.get_account_record("injected@example.com", runtime_config=injected)["email"] == "injected@example.com"
    assert storage.mark_quota_status(
        "injected@example.com",
        "available",
        runtime_config=injected,
    )
    assert storage.get_account_record("injected@example.com", runtime_config=injected)["quota_status"] == "available"

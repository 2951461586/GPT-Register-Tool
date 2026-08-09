import json
import os
import subprocess
import sys
import pytest
from sms_tool.config import ConfigError, current_config_data, load_runtime_config, runtime_config_scope
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

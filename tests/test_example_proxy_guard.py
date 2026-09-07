import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "example_guard", Path(__file__).resolve().parents[1] / "scripts" / "precommit_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize("host", ["8.8.8.8", "[2606:4700:4700::1111]"])
def test_example_rejects_public_ip_without_printing_address(tmp_path, host):
    path = tmp_path / "config.example.json"
    path.write_text('{"proxy": "http://' + host + ':8080"}', encoding="utf-8")
    findings = guard.scan_file(path, set())
    assert any(f[2] == "example-public-ip" for f in findings)
    assert host not in str(findings)


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.2", "192.0.2.1", "[::1]", "[2001:db8::1]", "proxy.example"])
def test_example_accepts_local_and_documentation_endpoints(tmp_path, host):
    path = tmp_path / "config.example.json"
    path.write_text('{"proxy": "http://' + host + ':8080"}', encoding="utf-8")
    assert guard.scan_file(path, set()) == []


def test_staged_content_cannot_be_hidden_by_clean_working_file(tmp_path):
    path = tmp_path / "config.example.json"
    path.write_text('{"proxy":"http://127.0.0.1:80"}', encoding="utf-8")
    findings = guard.scan_file(path, set(), text='{"proxy":"http://8.8.8.8:80"}')
    assert any(item[2] == "example-public-ip" for item in findings)

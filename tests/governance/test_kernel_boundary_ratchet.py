"""Tests for scripts/kernel_boundary_ratchet.py."""

from __future__ import annotations

from pathlib import Path

from scripts import kernel_boundary_ratchet as ratchet

ROOT = Path(__file__).resolve().parents[2]


def test_live_kernel_and_config_pass_boundary_ratchet():
    assert ratchet.violations(ROOT) == []
    assert ratchet.config_violations(ROOT) == []
    assert ratchet.main(["--root", str(ROOT)]) == 0


def test_kernel_rejects_any_application_import(tmp_path: Path):
    kernel = tmp_path / "sms_tool" / "kernel"
    kernel.mkdir(parents=True)
    (kernel / "__init__.py").write_text("", encoding="utf-8")
    (kernel / "bad.py").write_text("def f():\n    from ..config import CFG\n    return CFG\n", encoding="utf-8")
    problems = ratchet.violations(tmp_path)
    assert len(problems) == 1
    assert "sms_tool.config" in problems[0]


def test_config_rejects_module_scope_or_unapproved_delayed_app_imports(tmp_path: Path):
    kernel = tmp_path / "sms_tool" / "kernel"
    config = tmp_path / "sms_tool" / "config"
    kernel.mkdir(parents=True)
    config.mkdir(parents=True)
    (kernel / "driver_env.py").write_text("DRIVERS = {}\n", encoding="utf-8")
    (config / "__init__.py").write_text("from ..kernel.driver_env import DRIVERS as DRIVERS\n", encoding="utf-8")
    (config / "schema.py").write_text(
        "from ..registration_drivers.base import DRIVERS\n"
        "def ok():\n"
        "    from ..payment.catalog import PAYMENT_METHODS\n"
        "    return PAYMENT_METHODS\n"
        "def bad():\n"
        "    from ..registration_drivers.base import normalize_registration_driver\n"
        "    return normalize_registration_driver\n",
        encoding="utf-8",
    )
    problems = ratchet.config_violations(tmp_path)
    assert len(problems) == 2
    assert any("module-scope import sms_tool.registration_drivers.base" in p for p in problems)
    assert any("delayed import sms_tool.registration_drivers.base" in p for p in problems)

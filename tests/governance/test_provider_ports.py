"""Provider adapters receive persistence and UI through ports, never imports.

Why this exists
---------------
Rule 3 says provider adapters must not import the workflow. The 2026-10-21
architecture audit found the practical gap: two providers imported
``mailbox_pool_writer.apply_rotation`` (persistence) and one imported
``desktop_ipc.progress_dots_enabled`` (the stdout/IPC channel). Both are
decisions the routing owner makes, so the adapter was reaching up for them.

``sms_tool/providers/ports.py`` is the seam. These tests pin both halves: the
scan rejects a provider that imports the application modules again, and the port
fails loudly rather than silently skipping the write-back when nothing wired it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import architecture_scan  # noqa: E402  # type: ignore
from sms_tool.providers import ports  # noqa: E402


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


def test_a_relative_import_of_a_forbidden_module_is_reported(tmp_path):
    root = build(
        tmp_path,
        {
            "providers/__init__.py": "",
            "providers/mailbox_gmail.py": "from ..mailbox_pool_writer import apply_rotation\n",
        },
    )
    failures = architecture_scan.provider_port_violations(root)
    assert len(failures) == 1
    assert "mailbox_gmail.py" in failures[0]
    assert "mailbox_pool_writer" in failures[0]


def test_an_absolute_import_of_a_forbidden_module_is_reported(tmp_path):
    root = build(
        tmp_path,
        {
            "providers/__init__.py": "",
            "providers/mailbox_remail.py": "from sms_tool.desktop_ipc import progress_dots_enabled\n",
        },
    )
    assert architecture_scan.provider_port_violations(root)


def test_an_allowed_shared_module_is_not_reported(tmp_path):
    root = build(
        tmp_path,
        {
            "providers/__init__.py": "",
            # mailbox_poll is shared polling infrastructure a client may reuse.
            "providers/smailr_client.py": "from ..mailbox_poll import _poll_otp_with_settle\n",
        },
    )
    assert architecture_scan.provider_port_violations(root) == []


def test_the_ports_module_itself_is_the_exception(tmp_path):
    root = build(
        tmp_path,
        {
            "providers/__init__.py": "",
            "providers/ports.py": "def wire():\n    from ..desktop_ipc import progress_dots_enabled\n",
        },
    )
    assert architecture_scan.provider_port_violations(root) == []


def test_the_committed_tree_has_no_provider_port_violation():
    assert architecture_scan.provider_port_violations(PROJECT_ROOT / "sms_tool") == []


def test_an_unwired_rotation_raises_instead_of_losing_the_write_back(monkeypatch):
    # Block the lazy wiring so the unwired path itself is what runs.
    monkeypatch.setattr(ports, "_ensure_wired", lambda: None)
    monkeypatch.setattr(ports, "_apply_rotation", ports._unwired_apply_rotation)

    with pytest.raises(RuntimeError) as error:
        ports.apply_rotation(object(), previous="old", current="new")

    assert "not wired" in str(error.value)
    assert "sms_tool.mailbox.router" in str(error.value)


def test_install_makes_both_ports_callable(monkeypatch):
    monkeypatch.setattr(ports, "_ensure_wired", lambda: None)
    # ``install`` assigns module globals, so every fake it writes has to be
    # registered with monkeypatch too or it outlives this test: measured on a
    # serial full run, the fake rotation made the pool-write assertions in
    # ``tests/mailbox/test_mailbox_pool_writer.py`` fail (they pass alone, and
    # CI hid it because ``-n auto`` puts the two files in different workers).
    monkeypatch.setattr(ports, "_apply_rotation", ports._apply_rotation)
    monkeypatch.setattr(ports, "_progress_dots", ports._progress_dots)
    monkeypatch.setattr(ports, "_wired", ports._wired)
    calls: list[tuple[object, str, str]] = []

    def fake_rotation(mailbox, *, previous, current, pool_path=None):
        calls.append((mailbox, previous, current))
        return current

    ports.install(apply_rotation_impl=fake_rotation, progress_dots_impl=lambda: False)

    assert ports.progress_dots_enabled() is False
    assert ports.apply_rotation("box", previous="old", current="new") == "new"
    assert calls == [("box", "old", "new")]


def test_progress_dots_defaults_to_a_terminal_when_unwired(monkeypatch):
    """The pre-IPC behaviour: dots are allowed unless the app says otherwise."""

    monkeypatch.setattr(ports, "_ensure_wired", lambda: None)
    monkeypatch.setattr(ports, "_progress_dots", lambda: True)

    assert ports.progress_dots_enabled() is True

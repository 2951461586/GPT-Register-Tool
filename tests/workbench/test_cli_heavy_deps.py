"""Regression guard: CLI lazy dependencies must survive a patch rollback.

The 2026-10-18 scan reproduced an order-dependent failure:

    pytest -q tests/registration tests/workbench   # one_click failure
    pytest -q                                      # green

Root cause: `_heavy_deps_loaded` was a one-shot latch *and* the lazily imported
symbols were cached separately.  A test called `cli.main()` inside
``patch.object(cli, "load_mailbox_pool", ...)``; the latch was set while the
mock was in place, `patch.object` then restored the symbol to ``None``, and
every later call in the same process skipped the import for good.  The
workbench one-click command then called ``None(...)``
(``sms_tool/commands/one_click.py:94``).

The full suite hid the defect only because some earlier test had already filled
the symbols outside a patch.  These tests start from the un-resolved state, so
they fail on any future one-shot latch regardless of collection order.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import sms_tool.cli as cli


@pytest.fixture(autouse=True)
def unresolved_heavy_deps(monkeypatch):
    """Start each test from the state a fresh process has, and restore after."""
    for name in (
        "load_mailbox_pool",
        "remail_enabled",
        "_build_session_file",
        "_mailbox_snapshot",
        "run_email",
    ):
        monkeypatch.setattr(cli, name, None)


def test_heavy_deps_resolve_again_after_a_patched_symbol_is_restored():
    with patch.object(cli, "load_mailbox_pool", return_value=[]):
        cli._load_heavy_deps()
        assert cli.load_mailbox_pool is not None

    # `patch.object` restored the pre-patch value (None).  The next call must
    # re-resolve instead of trusting a latch set under the patch.
    assert cli.load_mailbox_pool is None
    cli._load_heavy_deps()
    assert callable(cli.load_mailbox_pool)


def test_one_click_context_never_carries_a_none_loader():
    with patch.object(cli, "load_mailbox_pool", return_value=[]):
        cli._load_heavy_deps()

    context = cli._one_click_command_context()

    assert callable(context.load_mailbox_pool)

"""Shared pytest fixtures (added 2026-09-02, round 6).

The suite previously had no conftest at all, so there was no shared place to
put isolation and no caller anywhere for ``reset_database_init_cache()``.

Design note -- deliberately NOT an autouse DB reset
---------------------------------------------------
``init_database()`` is memoized (97.4 ms -> 0.33 ms, a 294x win). Resetting that
memo before every test would silently hand the win back. The memo is already
keyed by resolved database path (see ``store/connection.py:_init_key``), so a
test that redirects ``database_path`` to a temp file gets a fresh entry on its
own -- cross-test leakage only becomes possible when a test reuses the *real*
path, which is exactly what ``isolated_database`` below is for.

So: opt-in fixtures for DB work, autouse only for cheap global state.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sms_tool import logging_setup
from sms_tool.store import reset_database_init_cache


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Tests that chdir must not leak it into the next test."""
    origin = Path.cwd()
    try:
        yield
    finally:
        try:
            os.chdir(origin)
        except OSError:  # pragma: no cover - cwd vanished
            pass


@pytest.fixture(autouse=True)
def _reset_logging_configured():
    """Undo the module-level idempotency flag that configure_logging() sets.

    ``cli.main()`` wires logging, so any test going through the CLI leaves
    ``_CONFIGURED = True`` behind and every later logging test becomes a no-op.
    Green alone, red in the full run -- the classic shape of global-state
    leakage between tests.
    """
    yield
    logging_setup._CONFIGURED = False


@pytest.fixture
def isolated_database(monkeypatch, tmp_path):
    """Point the account store at a throwaway SQLite file.

    Patches the public ``sms_tool.storage`` module (not ``store.connection``)
    because that is the indirection the store deliberately keeps so tests can
    redirect internal callers -- see the comment in ``store/_connect``.

    Yields the database path.
    """
    from sms_tool import storage

    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda *args, **kwargs: db_path)
    # Reset before as well as after: an earlier test may have populated the memo
    # for this same path, and the memo is keyed by path, not by test.
    reset_database_init_cache()
    try:
        yield db_path
    finally:
        reset_database_init_cache()

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


@pytest.fixture(autouse=True)
def _no_real_proxy_probe(request, monkeypatch):
    """Block the real geo/IP probe so a leaked config fails fast, not flaky.

    ``paypal_proxy._probe_proxy_network`` is the true network edge: both
    ``probe_proxy`` and the egress country gate (``payment_egress``) funnel
    into it.  Tests that mean to exercise the probe fake this function out --
    e.g. ``test_paypal_proxy.py`` patches ``_probe_proxy_network`` or
    ``paypal_proxy.requests.Session``, and their targets are deliberately
    unroutable hosts such as ``proxy.example``.

    Why this exists: a test that patches config on the *wrong* module
    (notably the ``payment_link_manager`` back-compat shell) silently keeps
    the real merged config, and therefore the real paid proxy pool, in play.
    The route planner then probes ``us.ipwo.net`` for real and the test
    passes or fails with the weather.  That is exactly the shape of the
    ``test_payment_result_contract.py`` flake -- see the note in that file.

    Opt out with ``@pytest.mark.allow_proxy_probe`` when a test genuinely
    needs the real implementation.
    """
    if "allow_proxy_probe" in request.keywords:
        yield
        return

    from sms_tool import paypal_proxy

    def _blocked(value, expected, stage, timeout):
        raise AssertionError(
            "real network probe reached in tests: paypal_proxy._probe_proxy_network"
            f"(value={value!r}, expected={expected!r}, stage={stage!r}). "
            "The test leaked the real config (or real proxy pool) into the payment "
            "route planner. Patch the config seam the production code actually "
            "reads -- sms_tool.pay_link.base.current_config_data, not the "
            "payment_link_manager shell -- or fake the probe explicitly."
        )

    monkeypatch.setattr(paypal_proxy, "_probe_proxy_network", _blocked)
    yield


@pytest.fixture(scope="session")
def runtime_sandbox(tmp_path_factory):
    """One runtime sandbox for the whole session -- see ``isolated_runtime``.

    🔴 The last path segment must be **literally** ``runtime``.  ``mktemp()``
    appends a counter (``runtime0``, ``runtime1``, ...), and
    ``test_logging_setup.py`` asserts that the default log path sits under a
    directory *named* ``runtime`` rather than directly inside the repo root --
    that assertion is the production contract, not an implementation detail, so
    the sandbox has to satisfy it instead of the assertion being relaxed.
    Hence: a numbered parent from ``mktemp`` with a real ``runtime/`` inside.
    """
    root = tmp_path_factory.mktemp("runtime_sandbox") / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def isolated_runtime(request, runtime_sandbox, monkeypatch):
    """Re-root the whole runtime tree at a temp sandbox.

    ``paths.runtime_file(cfg, name)`` resolves its directory through the module
    global ``paths.runtime_dir(cfg)``, so patching that one function re-roots
    every caller -- including the 15+ modules that bound ``runtime_file`` by
    name (``from .paths import runtime_file``), which is the same
    "patch the binding, not the source" trap that caused the
    ``current_config_data`` flake.

    Without this a full run writes into the real ``runtime/``: ``accounts.sqlite3``,
    ``payment_link_runs.jsonl``, ``registration_progress.jsonl``,
    ``paypal_proxy_state.json``, ``logs/sms_tool.log``, ``payment_batches/`` and
    ``payment_batch_locks/gates/`` -- about 36 files per run even after the
    payment-operation store was isolated.  ``accounts.sqlite3`` is the real
    account database, so this is a correctness issue, not just hygiene.

    🔴 The sandbox is **session-scoped on purpose**.  A per-test sandbox makes
    ``storage.database_path()`` resolve to a different SQLite file for every
    test, which defeats the ``init_database()`` memo in ``store/connection.py``
    (keyed on the resolved DB path, 97.4 ms -> 0.33 ms).  Measured cost of
    getting this wrong: **a full run goes from ~6 min to ~12 min**.  Sharing one
    sandbox per session is no weaker than what tests did before -- they all
    shared the real ``runtime/`` anyway.

    Opt out with ``@pytest.mark.allow_real_runtime``.

    Yields the sandbox root.
    """
    if "allow_real_runtime" in request.keywords:
        yield None
        return

    from sms_tool import paths

    monkeypatch.setattr(paths, "runtime_dir", lambda cfg=None: runtime_sandbox)
    yield runtime_sandbox


@pytest.fixture(autouse=True)
def isolated_payment_operations(tmp_path, monkeypatch):
    """Keep the payment-operation ledger out of the real runtime tree.

    Belt and braces on top of ``isolated_runtime``.  This is the one runtime
    artefact that is *production state* rather than a cache or a log: it is the
    ledger of payment operations, and ~27 call sites across 4 test files used to
    write fake records into it on every full run (measured with a recorder
    plugin).  Keeping a dedicated seam means a future test that opts out of the
    global runtime re-rooting with ``allow_real_runtime`` still cannot pollute
    the ledger.

    Tests that construct the store directly (``test_payment_operation.py``) are
    already isolated, and tests that patch ``from_config`` themselves simply
    override this one.

    Yields the isolated root.
    """
    from sms_tool.payment_operation import PaymentOperationStore

    root = tmp_path / "payment_operations"
    monkeypatch.setattr(
        PaymentOperationStore,
        "from_config",
        classmethod(lambda cls, config: cls(root)),
    )
    yield root


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

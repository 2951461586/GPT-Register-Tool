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
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Must run before any `sms_tool` import: `sms_tool.config` resolves its config
# directory at import time. Without this, the suite would read (and, through the
# legacy-root migration, move) the operator's real credential files.
if not os.environ.get("GPT_REGISTER_CONFIG_DIR"):
    _TEST_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="gpt_register_test_config_"))
    # Seed from the public example so config-dependent code paths see a valid,
    # credential-free configuration instead of the operator's real one.
    _example = Path(__file__).resolve().parent.parent / "config.example.json"
    if _example.is_file():
        (_TEST_CONFIG_DIR / "config.json").write_bytes(_example.read_bytes())
    os.environ["GPT_REGISTER_CONFIG_DIR"] = str(_TEST_CONFIG_DIR)

from sms_tool import logging_setup
from sms_tool.store import reset_database_init_cache


@pytest.fixture(autouse=True)
def _mark_test_telemetry(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_EVENT_SOURCE", "test")


@pytest.fixture(autouse=True)
def _restore_database_path_provider():
    """A test that redirects the store must not leak the redirect.

    ``set_database_path_provider`` is process-global by design (26 ``_connect()``
    call sites would otherwise each carry a parameter), so the suite snapshots and
    restores it around every test -- including the session-scoped sandbox guard
    installed by ``isolated_runtime``.
    """
    from sms_tool.store import connection as store_connection

    previous = store_connection.database_path_provider()
    yield
    store_connection.set_database_path_provider(previous)


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
def _reset_upi_sentinel_mint_cache():
    """Clear the UPI Sentinel mint cache around every test.

    ``sms_tool.upi_link.sentinel._MINT_CACHE`` is module-level and keyed by
    ``(flow, device_id, proxy, cookie, page_url, fingerprint)``.  A leaked entry
    makes a later test read a previous test's mocked token instead of its own --
    the same green-alone, red-in-full-run shape as the logging flag above.
    """
    from sms_tool.upi_link import sentinel as upi_sentinel

    upi_sentinel._MINT_CACHE.clear()
    yield
    upi_sentinel._MINT_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_registration_cancel():
    """Reset the process-global registration cancel flag around every test.

    ``sms_tool.registration_cancel._cancel_event`` is a module-level Event.
    A test that requests cancellation and does not clear it would cancel
    every later registration test in the same session -- the same
    green-alone, red-in-full-run shape as the logging flag above.
    """
    from sms_tool import registration_cancel

    registration_cancel.clear_registration_cancel()
    yield
    registration_cancel.clear_registration_cancel()


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


@pytest.fixture(autouse=True)
def _no_real_checkout_sentinel(request, monkeypatch):
    """Block real Sentinel minting on the Checkout create lane.

    ``payment_wire._checkout_post`` attaches the ``chatgpt_checkout`` token
    pair to every Checkout *create*. Minting would dial
    ``sentinel.openai.com`` and spawn Node for a test that only means to assert
    payload/header plumbing. Tests that exercise the mint opt out with
    ``@pytest.mark.allow_checkout_sentinel`` and patch the mint explicitly.
    """
    from sms_tool import sentinel as sentinel_pkg

    sentinel_pkg.client._CHECKOUT_MINT_CACHE.clear()
    if "allow_checkout_sentinel" in request.keywords:
        yield
        sentinel_pkg.client._CHECKOUT_MINT_CACHE.clear()
        return

    monkeypatch.setattr(sentinel_pkg, "checkout_sentinel_headers", lambda **kwargs: {})
    monkeypatch.setattr(
        sentinel_pkg,
        "issue_checkout_sentinel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("real checkout Sentinel mint reached in tests")),
    )
    yield
    sentinel_pkg.client._CHECKOUT_MINT_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_real_promotion_preflight(request, monkeypatch):
    """Block the promotion batch's real egress preflight.

    ``promotion_batch._preflight_egress`` sends one anonymous GET per sampled
    candidate through ``probe_openai_edge``.  Tests drive the batch by stubbing
    ``check_account_promotion``; without this guard the preflight would be the
    only real network call left -- and a test using an unroutable host would
    either sit in the probe's timeout or abort the batch before the stubbed
    probe ever ran.  Patch the *promotion_batch* binding, not
    ``proxy_edge_probe`` itself, so other probe users are untouched.

    A test that wants the real verdict just re-patches
    ``promotion_batch.probe_openai_edge`` in its own body (its monkeypatch runs
    after this fixture, so it wins).
    """
    if "allow_edge_probe" in request.keywords:
        yield
        return

    from sms_tool.accounts import promotion_batch
    from sms_tool.proxy_edge_probe import CLEAN, EdgeVerdict

    monkeypatch.setattr(
        promotion_batch,
        "probe_openai_edge",
        lambda proxy, **kwargs: EdgeVerdict(proxy="***:***", status=CLEAN),
    )
    yield


@pytest.fixture(autouse=True)
def _no_real_egress_probe(request, monkeypatch):
    """Block the payment egress gate's real proxy probe.

    ``payment_egress.assert_egress_countries`` dials every routed stage proxy and
    compares the observed exit country.  Since the in-process adapters
    (``native_paypal`` / ``wallet`` / ``gcash_custom`` / ``regional_wallet``, and
    ``native_upi`` through the pipeline) assert the same contract the subprocess
    extractors always did, any test that drives a function adapter through the
    router *with both a proxy and an expected country* would reach the network --
    and the assertion under test is about routing, not exit geography.

    Same shape as :func:`_no_real_promotion_preflight`: stub the probe the
    production code actually binds, and let a test that means to exercise the gate
    opt out with ``@pytest.mark.allow_egress_probe``.  The gate's own tests
    (``test_payment_egress_gate``, ``test_upi_egress_gate``) patch
    ``payment_egress._default_probe`` directly; their patch is applied after this
    fixture, so it wins.
    """
    if "allow_egress_probe" in request.keywords:
        yield
        return

    from sms_tool import payment_egress

    def _stub(proxy, expected, stage, timeout):
        # "the exit matches what the route plan expected" -- the only verdict
        # that lets a routing test proceed.
        return SimpleNamespace(ok=True, country_code=str(expected or "").upper(), error="")

    payment_egress.clear_cache()
    monkeypatch.setattr(payment_egress, "_default_probe", _stub)
    yield
    payment_egress.clear_cache()


@pytest.fixture(autouse=True)
def _offline_payment_methods_online(request, monkeypatch):
    """Run the subprocess-extractor plumbing tests as if the methods were online.

    The catalog marks ideal / pix / kakao / blik / twint / momo ``offline`` and the
    router refuses to execute them, because the Checkout create no longer issues
    the ``cs_*`` sessions their extractors need.  The adapter, contract and batch
    machinery those tests cover is still live code and still needs a concrete
    method to drive, so each test sees the pre-offline definition by default.

    A test that means to exercise the refusal opts in with
    ``@pytest.mark.enforce_offline_methods``.
    """
    if "enforce_offline_methods" in request.keywords:
        yield
        return

    import dataclasses

    from sms_tool import payment_batch, payment_catalog

    restored = {
        key: (
            dataclasses.replace(
                definition,
                release_tier="production",
                batch_enabled=key != "blik",
                registration_enabled=True,
            )
            if definition.release_tier == "offline"
            else definition
        )
        for key, definition in payment_catalog.PAYMENT_METHODS.items()
    }
    monkeypatch.setattr(payment_catalog, "PAYMENT_METHODS", restored)
    monkeypatch.setattr(payment_batch, "PAYMENT_METHODS", restored)
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
    name (``from .kernel.paths import runtime_file``), which is the same
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

    from sms_tool.kernel import paths
    from sms_tool.store import connection as store_connection

    monkeypatch.setattr(paths, "runtime_dir", lambda cfg=None: runtime_sandbox)

    # 🔴 Re-rooting ``runtime_dir`` is not enough: the operator's real
    # ``runtime.json`` carries ``storage.sqlite_path = runtime/accounts.sqlite3``
    # and ``database_path`` returns that *before* it ever asks ``runtime_dir``.
    # Every test that read the real config therefore wrote into the production
    # account database -- hundreds of fake ``environment_ledger`` rows a day
    # (``proxy.invalid`` / ``127.0.0.1:7897`` exits) since 2026-10-05.  Redirect
    # only the path that *is* the real database, so tests that configure their
    # own ``sqlite_path`` still get exactly what they asked for.
    real_db = Path(__file__).resolve().parents[1] / "runtime" / "accounts.sqlite3"
    sandbox_db = runtime_sandbox / "accounts.sqlite3"
    original_database_path = store_connection.default_database_path

    def _guarded_database_path(*args, **kwargs):
        path = Path(original_database_path(*args, **kwargs))
        try:
            if path.resolve() == real_db.resolve():
                sandbox_db.parent.mkdir(parents=True, exist_ok=True)
                if not sandbox_db.exists():
                    # A test may have deleted the sandbox file; the schema memo
                    # would still say "initialized" and later readers would hit
                    # "no such table".  The real file never vanished, so this
                    # case did not exist before the redirect.
                    store_connection._INITIALIZED.discard(
                        os.path.normcase(str(sandbox_db.resolve()))
                    )
                return sandbox_db
        except OSError:
            pass
        return path

    previous_provider = store_connection.database_path_provider()
    store_connection.set_database_path_provider(_guarded_database_path)
    request.addfinalizer(lambda: store_connection.set_database_path_provider(previous_provider))
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

    def _from_config(cls, config):
        return cls(root)

    monkeypatch.setattr(PaymentOperationStore, "from_config", classmethod(_from_config))
    yield root


@pytest.fixture
def isolated_database(tmp_path):
    """Point the account store at a throwaway SQLite file.

    Uses the explicit path seam (``database_path_provider``), which is what the
    production resolver consults on every call -- no module attribute is
    monkeypatched and ``store`` no longer reaches into ``sms_tool.storage``.

    Yields the database path.
    """
    from sms_tool.store import connection as store_connection

    db_path = tmp_path / "accounts.sqlite3"
    previous = store_connection.database_path_provider()
    store_connection.set_database_path_provider(lambda *args, **kwargs: db_path)
    # Reset before as well as after: an earlier test may have populated the memo
    # for this same path, and the memo is keyed by path, not by test.
    store_connection.reset_database_init_cache()
    try:
        yield db_path
    finally:
        store_connection.set_database_path_provider(previous)
        store_connection.reset_database_init_cache()

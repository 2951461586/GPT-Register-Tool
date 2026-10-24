"""Pin the bootstrap timing row against the nested-stage clobber.

``_run_bootstrap`` ticks ``0-Bootstrap`` and then runs ``_bootstrap``, which
itself calls two ``_run_stage`` calls (sentinel, identity).  Those inner stages
tick/tock their own rows and cleared the shared ``_timing_open`` flag, so the
outer row was never closed: it kept its raw ``time.time()`` value, and both the
printed ``TOTAL`` and ``timing.total_seconds`` came out epoch-sized (observed
live on 2026-10-23, three protocol registrations).

The fix closes the row *by name* (``_tock_named``), because ``_safe_tock`` only
closes the last entry and by then the last entry is the identity stage.
"""

from pathlib import Path

from sms_tool.registration import orchestrator as registration
from sms_tool.registration.handlers import RegistrationEmailWorkflow
from sms_tool.registration.protocol_helpers import tock_named
from sms_tool.registration.state import RegistrationState, RegistrationStateMachine
from sms_tool.utils import _tl

EPOCH = 1_000_000


def _workflow() -> RegistrationEmailWorkflow:
    return RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        operations=registration._email_registration_operations(),
    )


def test_bootstrap_row_is_closed_even_when_nested_stages_close_theirs():
    workflow = _workflow()
    workflow.r._tl().clear()

    def fake_bootstrap() -> None:
        # The real bodies tick exactly these two labels, in this order.
        workflow._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", lambda: None)
        workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", lambda: None)

    workflow._bootstrap = fake_bootstrap
    workflow._run_bootstrap()

    rows = list(workflow.r._tl())
    names = [name for name, _ in rows]
    assert names == ["0-Bootstrap", "0-Extract sentinel token", "1-Prepare registration identity"]
    assert rows[0][1] < EPOCH, f"bootstrap row kept its epoch value: {rows[0][1]}"
    assert sum(value for _, value in rows) < EPOCH, "TOTAL is still epoch-sized"


def test_bootstrap_row_survives_a_raising_nested_stage():
    workflow = _workflow()
    workflow.r._tl().clear()

    def failing_bootstrap() -> None:
        workflow._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", lambda: None)
        raise RuntimeError("transport died")

    workflow._bootstrap = failing_bootstrap
    try:
        workflow._run_bootstrap()
    except Exception:  # noqa: BLE001 - the abort path is the point
        pass

    rows = list(workflow.r._tl())
    assert rows[0][0] == "0-Bootstrap"
    assert rows[0][1] < EPOCH


def test_tock_named_is_inert_on_a_second_call_and_preserves_order():
    import time

    _tl().clear()
    # A real tick stores ``time.time()``; the close subtracts it from now.
    _tl().extend([("outer", time.time() - 5.0), ("inner", 1.5)])
    tock_named(_tl(), "outer")
    first = list(_tl())
    tock_named(_tl(), "outer")
    assert _tl() == first
    assert [name for name, _ in first] == ["outer", "inner"]
    assert 4.0 < first[0][1] < 30.0


def test_tock_named_ignores_an_absent_name():
    import time

    _tl().clear()
    _tl().append(("outer", time.time() - 3.0))
    tock_named(_tl(), "missing")
    assert _tl()[0][1] > EPOCH


def test_tock_named_uses_an_injected_clock():
    rows = [("outer", 1_000_000_000.0), ("inner", 2.0)]
    tock_named(rows, "outer", now=lambda: 1_000_000_042.0)
    assert rows == [("outer", 42.0), ("inner", 2.0)]


def test_protocol_lane_files_carry_the_fix():
    """Pin *where* the fix lives, because three ratchets bound that choice.

    ``utils`` may not gain another private consumer (private-import ratchet) and
    the orchestrator facade may not gain a module dependency (lane-coupling
    ratchet + the committed dependency graph), so the helper lives in
    ``protocol_helpers`` and is reached from the module that already imports it.
    """
    root = Path(__file__).resolve().parents[2] / "sms_tool"
    bootstrap = (root / "registration" / "bootstrap_stages.py").read_text(encoding="utf-8")
    handlers = (root / "registration" / "handlers.py").read_text(encoding="utf-8")
    orchestrator = (root / "registration" / "orchestrator.py").read_text(encoding="utf-8")
    # 2026-10-24: the bootstrap body moved to bootstrap_stages.py; the module
    # that *uses* the helper is where the fix must stay visible.
    assert 'tock_named(r._tl(), "0-Bootstrap")' in bootstrap
    assert "from .protocol_helpers import tock_named" in bootstrap
    assert "from ..utils import _tock_named" not in handlers
    assert "from ..utils import _tock_named" not in bootstrap
    assert "_tock_named" not in orchestrator

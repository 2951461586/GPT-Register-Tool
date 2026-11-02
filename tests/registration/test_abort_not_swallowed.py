"""A stage abort must never be turned into an ordinary failure by a broad except.

Why this exists
---------------
``RegistrationAbort`` and ``RegistrationCancelled`` both derive from ``RuntimeError``,
so a bare ``except Exception`` sitting between the stage runner and ``run()`` would
swallow them and report ``registration_internal_error`` instead of the real verdict
-- the operator would see "internal error" for a clean cancellation, and the funnel
would count it as an unclassified failure.

The 2026-11-01 P6 measurement found **no** such handler in the protocol lane (all 7
silent handlers are resource cleanup), and the ratchet now holds the lane at
``silent = 0``. This file pins the property itself so it cannot regress by a
handler that looks local:

1. the stage runner propagates both signals untouched;
2. no broad handler in the lane wraps a call to a stage runner;
3. every silent broad handler that remains is a reviewed whitelist entry.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from sms_tool.registration.stage_runner import (
    RegistrationAbort,
    RegistrationStageRunner,
    RegistrationState,
    RegistrationStateMachine,
)

ROOT = Path(__file__).resolve().parents[2]
LANE_ROOTS = (ROOT / "sms_tool" / "auth_flow", ROOT / "sms_tool" / "registration")
BASELINE = ROOT / "scripts" / "broad_except_baseline.json"

sys.path.insert(0, str(ROOT / "scripts"))
# One definition of "broad"/"silent": the gate's own predicate.
import broad_except_ratchet as gate  # noqa: E402

STAGE_CALLS = {"run_stage", "_run_stage"}


def _lane_sources():
    for root in LANE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


_ABORT_TYPES = {"RegistrationAbort", "RegistrationCancelled", "CancelledError"}


def _names_abort(handler: ast.ExceptHandler) -> bool:
    """True when the handler catches one of the workflow's own exit signals."""
    node = handler.type
    candidates = node.elts if isinstance(node, ast.Tuple) else [node]
    return any(isinstance(item, ast.Name) and item.id in _ABORT_TYPES for item in candidates)


def test_the_runner_propagates_an_abort_unchanged():
    runner = RegistrationStageRunner(object(), RegistrationStateMachine(lambda *_: None))

    def failing():
        raise RegistrationAbort("email_otp_send_stuck:arm_mismatch")

    with pytest.raises(RegistrationAbort):
        runner.run_stage(RegistrationState.EMAIL_OTP_SEND, failing)


def test_the_runner_propagates_a_cancellation_unchanged():
    from sms_tool.registration.cancel import RegistrationCancelled

    runner = RegistrationStageRunner(object(), RegistrationStateMachine(lambda *_: None))

    def cancelled():
        raise RegistrationCancelled()

    with pytest.raises(RegistrationCancelled):
        runner.run_stage(RegistrationState.EMAIL_OTP_WAIT, cancelled)


def test_no_broad_handler_in_the_lane_wraps_a_stage_runner():
    """A swallowed abort is indistinguishable from a protocol failure downstream."""
    offenders = []
    for path in _lane_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            handlers = node.handlers
            # A broad handler is only dangerous *before* the typed exit signals are
            # caught: `run()` and `_run_stage` both list RegistrationAbort /
            # RegistrationCancelled first, so their trailing `except Exception` (the
            # unknown-failure funnel) cannot swallow one.
            first_broad = next((i for i, h in enumerate(handlers) if gate._is_broad(h)), None)
            if first_broad is None:
                continue
            if any(_names_abort(h) for h in handlers[:first_broad]):
                continue
            called = {
                item.func.attr if isinstance(item.func, ast.Attribute) else getattr(item.func, "id", "")
                for item in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                if isinstance(item, ast.Call)
            }
            if called & STAGE_CALLS:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert offenders == [], f"a broad except wraps a stage runner and would swallow an abort: {offenders}"


def test_every_silent_handler_in_the_lane_is_a_reviewed_whitelist_entry():
    whitelist = json.loads(BASELINE.read_text(encoding="utf-8")).get("whitelist", {})
    silent_files = []
    for path in _lane_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not gate._is_broad(node):
                continue
            if gate._is_silent(node):
                silent_files.append(path.relative_to(ROOT).as_posix())
                break

    unreviewed = sorted(name for name in silent_files if name not in whitelist)
    assert unreviewed == [], (
        "silent broad handlers outside the reviewed whitelist: "
        f"{unreviewed}. Either narrow the except, or add the file to "
        "scripts/broad_except_baseline.json's whitelist with a reason."
    )

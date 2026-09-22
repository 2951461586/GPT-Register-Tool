"""Every field of the registration runtime state that is read must also be written.

Why this exists
---------------
``RegistrationRuntimeState`` is a flat-looking bag of 52 fields spread over six
dataclasses.  A field can be declared, defaulted, read by ``finalize`` -- and
still never be assigned by any code path.  Nothing about the declaration or the
read site looks wrong, so the defect is invisible: the reader silently gets the
dataclass default, every time, forever.

Measured 2026-09-14, this is not hypothetical.  Five instances were found by
hand around the Codex OAuth plumbing (``oauth_result``, ``oauth_refresh_token``,
the ``codex_oauth`` constructor argument, ``RegistrationState.CODEX_OAUTH``,
``_oauth_result_summary``), and a sixth -- ``phone_result`` -- was only found by
writing this scan.  ``oauth_refresh_token`` was the expensive one: 0 of 1245
accounts held a refresh token, so the recovery chain skipped its only free
strategy on every account and paid an email code instead.

The rule
--------
For each field of the state, count attribute **loads** and attribute **stores**
across ``sms_tool/``:

* ``reads > 0`` and ``writes == 0``  -> **fails**.  A reader exists, so the field
  is load-bearing, but it can only ever yield the default.
* ``reads == 0`` and ``writes == 0`` -> **fails**.  Nothing reads it, so the
  field is dead weight in a schema that is part of the checkpoint contract.

Both lists are allowed to carry known exceptions, each with a written reason.
An exception that stops being needed fails the suite too, so the allowlists
cannot rot into decoration.

Scope note: the scan reads ``sms_tool/`` only.  Test fixtures write state too,
but they are not evidence that production code does.

Why this is scoped to *one* class
---------------------------------
Measured 2026-09-14 with ``runtime/_probe_state_field_sweep.py``: applying the
same criterion to every annotated dataclass in ``sms_tool/`` flags **130 of 329**
name-unique fields -- a 40% false-positive rate.  The inference "no attribute
write anywhere means nothing can produce a value" is only valid for a flat
property bag.  Four construction shapes break it, each verified in the tree:

* **factory ``cls(...)``** -- ``account_models.py:128``, ``browser_pool.py:69``.
  The callee is ``cls``, so constructor keyword arguments are invisible.
* **declaration-only interface** -- ``registration_operations.py:104``.  Its
  attributes are satisfied by the implementing object at runtime; there is no
  dataclass write by design.  This one class accounts for 60 of the 130.
* **declared default / injection seam** -- ``geo/resolver.py:319-328``.  Relying
  on ``verify_hint: bool = False`` is correct; ``probe`` is documented as a seam
  a caller or test swaps.
* **``**mapping`` / ``from_config``** -- ``browser_pool.py:119``,
  ``mailbox_service.py:46``.  Field names arrive as dict keys, not keywords.

``RegistrationRuntimeState`` avoids all four: it is written by attribute
assignment everywhere, and ``_compat_property`` routes even ``setattr(state,
name, value)`` onto the grouped dataclass.  So "no write" really does mean "no
way to produce a value" here, and only here.  Do not generalise this guard
without first removing those four shapes from the scan.
"""

from __future__ import annotations

import ast
import sys
import textwrap
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sms_tool.registration_runtime import _FIELD_GROUPS  # noqa: E402

#: Fields that are read but never written, with the reason each one is tolerated.
#:
#: ``phone_result`` -- **a real, unfixed gap.**  ``finalize`` reads it twice to
#: fill ``response["phone_verification"]`` and ``extra["phone"]``, but no code
#: path assigns it, so both are permanently ``{}`` and ``""``.  The lane also
#: accepts a ``phone_pool`` constructor argument and stores it without ever
#: using it (``registration_handlers.py:290``/``:305``), and ``accounts`` has no
#: ``phone`` column at all -- so wiring it up is a product decision (does this
#: lane do phone verification?) rather than a mechanical fix.  Remove this entry
#: when that decision lands either way.
#: ``resume_email_verification`` used to be the second entry here.  It was
#: **deleted outright on 2026-09-16** (owner decision) instead of being wired
#: back up: the state field, both of its reads, and the ``_email_otp_send_url``
#: fallback it drove -- along with the ``auth_base`` parameter that only that
#: fallback consumed.  There is nothing left to tolerate, so nothing is
#: registered.
#:
#: 🔴 Do not "helpfully" re-add it to silence a future guard failure.  A field
#: reappearing in this whitelist means someone resurrected a producer, and that
#: argument has to be won again (see the deletion-site comment in
#: ``registration_handlers.user_register`` and the docstring of
#: ``account_creation._email_otp_send_url``).
READ_BUT_NEVER_WRITTEN = {
    "phone_result": "finalize reads it; the lane has no phone step to write it (see module docstring)",
}

#: Fields that are neither read nor written, with the reason each one survives.
#:
#: ``oauth_tokens`` -- dead.  Nothing reads it, nothing writes it, it is absent
#: from the checkpoint payload, and ``RegistrationRuntimeState`` is constructed
#: without keyword arguments in production.  Kept only because it is part of the
#: published state schema; deleting it is a one-line change once someone
#: confirms no downstream integration passes it.
NEVER_READ_NEVER_WRITTEN = {
    "oauth_tokens": "dead field; removal proposed, pending confirmation nobody writes it downstream",
}


def _attribute_counts(source: str) -> tuple[dict[str, int], dict[str, int]]:
    """``(reads, writes)`` for the state fields mentioned in ``source``."""
    reads: dict[str, int] = defaultdict(int)
    writes: dict[str, int] = defaultdict(int)
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Attribute) and node.attr in _FIELD_GROUPS:
            if isinstance(node.ctx, ast.Load):
                reads[node.attr] += 1
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                writes[node.attr] += 1
        # ``setattr(state, "field", value)`` is a write too.
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                name = str(node.args[1].value)
                if name in _FIELD_GROUPS:
                    writes[name] += 1
    return dict(reads), dict(writes)


def _scan_package() -> tuple[dict[str, int], dict[str, int]]:
    reads: dict[str, int] = defaultdict(int)
    writes: dict[str, int] = defaultdict(int)
    for path in sorted((ROOT / "sms_tool").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        file_reads, file_writes = _attribute_counts(path.read_text(encoding="utf-8"))
        for name, count in file_reads.items():
            reads[name] += count
        for name, count in file_writes.items():
            writes[name] += count
    return dict(reads), dict(writes)


def _read_but_never_written(
    reads: dict[str, int], writes: dict[str, int], allowed: dict[str, str]
) -> list[str]:
    """Fields a reader depends on that no code path can ever set."""
    return sorted(
        name
        for name in _FIELD_GROUPS
        if reads.get(name, 0) > 0 and writes.get(name, 0) == 0 and name not in allowed
    )


def _never_read_never_written(
    reads: dict[str, int], writes: dict[str, int], allowed: dict[str, str]
) -> list[str]:
    """Fields that are dead weight in the checkpoint schema."""
    return sorted(
        name
        for name in _FIELD_GROUPS
        if reads.get(name, 0) == 0 and writes.get(name, 0) == 0 and name not in allowed
    )


class TheScannerItselfTests(unittest.TestCase):
    """Guard the guard: a scanner that sees nothing makes every check pass."""

    def test_it_counts_a_plain_read_and_write(self):
        reads, writes = _attribute_counts("state.oauth_result = state.at_probe")
        self.assertEqual(reads.get("at_probe"), 1)
        self.assertEqual(writes.get("oauth_result"), 1)

    def test_it_counts_a_tuple_assignment_as_a_write(self):
        # The exact shape the first version of this scan missed with a regex:
        # ``s.success, s.error, s.registration_warning = ...`` has a comma where
        # the regex wanted ``=``, so ``success`` looked never-written.
        reads, writes = _attribute_counts("s.success, s.error, s.registration_warning = verdict()")
        self.assertEqual(writes.get("success"), 1)
        self.assertEqual(writes.get("error"), 1)
        self.assertEqual(writes.get("registration_warning"), 1)

    def test_a_read_is_not_mistaken_for_a_write(self):
        _reads, writes = _attribute_counts("print(s.phone_result)")
        self.assertEqual(writes.get("phone_result", 0), 0)

    def test_augmented_assignment_counts_as_a_write(self):
        _reads, writes = _attribute_counts("s.auth_flow_started += 1")
        self.assertEqual(writes.get("auth_flow_started"), 1)


class TheVerdictsThemselvesTests(unittest.TestCase):
    """Each verdict gets a case where *only* it can fire.

    The real tree today has exactly one read-but-never-written field and exactly
    one dead field, both allowlisted.  So a verdict function that silently
    stopped flagging anything would still make the real-tree tests pass -- the
    allowlists would cover for it.  These synthetic inputs remove that cover.
    """

    def test_a_read_without_a_write_is_flagged(self):
        self.assertEqual(
            _read_but_never_written({"at_probe": 3}, {}, {}),
            ["at_probe"],
        )

    def test_a_read_with_a_write_is_not_flagged(self):
        self.assertEqual(_read_but_never_written({"at_probe": 3}, {"at_probe": 1}, {}), [])

    def test_an_allowlisted_field_is_not_flagged(self):
        allowed = {"at_probe": "known"}
        self.assertEqual(_read_but_never_written({"at_probe": 3}, {}, allowed), [])

    def test_a_dead_field_is_flagged(self):
        self.assertEqual(_never_read_never_written({}, {}, {}), sorted(_FIELD_GROUPS))

    def test_a_field_that_is_only_written_is_not_dead(self):
        # The other 51 fields are dead in this synthetic input, so assert on the
        # one field the case is about rather than on the whole list.
        self.assertNotIn("at_probe", _never_read_never_written({}, {"at_probe": 1}, {}))


class TheRealTreeTests(unittest.TestCase):
    """The scan over ``sms_tool/`` -- this is the part that catches regressions."""

    def test_the_scan_reached_every_field(self):
        reads, writes = _scan_package()
        # Premise first: if the scanner silently stopped resolving fields, the
        # two checks below would pass while checking nothing.
        self.assertGreater(len(_FIELD_GROUPS), 40)
        covered = {name for name in _FIELD_GROUPS if reads.get(name) or writes.get(name)}
        self.assertGreater(
            len(covered),
            len(_FIELD_GROUPS) - 5,
            f"scan only saw {len(covered)} of {len(_FIELD_GROUPS)} state fields",
        )

    def test_every_field_that_is_read_is_also_written(self):
        reads, writes = _scan_package()
        unexpected = _read_but_never_written(reads, writes, READ_BUT_NEVER_WRITTEN)
        self.assertEqual(
            unexpected,
            [],
            "these fields are read by sms_tool/ but never assigned anywhere, so "
            "every reader silently gets the dataclass default: "
            + ", ".join(unexpected),
        )

    def test_no_field_is_both_unread_and_unwritten(self):
        reads, writes = _scan_package()
        unexpected = _never_read_never_written(reads, writes, NEVER_READ_NEVER_WRITTEN)
        self.assertEqual(
            unexpected,
            [],
            "dead state fields (neither read nor written): " + ", ".join(unexpected),
        )

    def test_the_allowlists_carry_no_stale_entries(self):
        # An exception that is no longer needed must be deleted, or the lists
        # become a place where real gaps hide.
        reads, writes = _scan_package()
        for name, reason in READ_BUT_NEVER_WRITTEN.items():
            self.assertIn(name, _FIELD_GROUPS, f"{name}: not a state field")
            self.assertGreater(
                reads.get(name, 0),
                0,
                f"{name} is no longer read -- drop it from READ_BUT_NEVER_WRITTEN ({reason})",
            )
            self.assertEqual(
                writes.get(name, 0),
                0,
                f"{name} is now written -- drop it from READ_BUT_NEVER_WRITTEN ({reason})",
            )
        for name, reason in NEVER_READ_NEVER_WRITTEN.items():
            self.assertIn(name, _FIELD_GROUPS, f"{name}: not a state field")
            self.assertEqual(
                (reads.get(name, 0), writes.get(name, 0)),
                (0, 0),
                f"{name} is now used -- drop it from NEVER_READ_NEVER_WRITTEN ({reason})",
            )


if __name__ == "__main__":
    unittest.main()

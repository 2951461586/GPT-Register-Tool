"""Pins the type-only surface of ``UpiOperations`` against its slot tuple.

``UpiOperations`` builds ``__slots__`` from ``UPI_OPERATION_NAMES`` at
class-creation time.  That is a runtime-correct design and a static-analyser
blind spot: before 2026-10-22 the class declared no annotations, so every
``ops.<name>`` read in ``stages.py`` was a ``reportAttributeAccessIssue``
finding -- 97 of them, the largest single-file contribution to the repo's
pyright baseline (415 at the time).

The fix is a block of bare annotations (``name: Any`` with no ``=``).  What
makes that safe is not obvious and fails silently rather than loudly:

* **A bare annotation adds no class attribute.**  It only populates
  ``__annotations__``.  Writing ``name: Any = None`` instead would install a
  class attribute that *shadows the slot descriptor*, so ``ops.name`` would
  read the class default for every instance until the first assignment.  The
  class-level object for a correct slot is a ``member_descriptor``; anything
  else means the annotation grew a default.  (``hasattr(UpiOperations, name)``
  cannot tell these apart -- a slot descriptor is itself a class attribute --
  which is exactly why this test compares the descriptor type.)
* **The tuple and the annotation block are two lists a human edits
  independently.**  Drift in either direction is silent: a missing annotation
  reintroduces the findings, an extra one documents a name that is not a slot.
"""

from __future__ import annotations

import types
import unittest
from typing import Any

from sms_tool.upi_link.operations import (
    UPI_OPERATION_NAMES,
    UpiOperations,
    build_upi_operations,
)


def _fake_namespace() -> dict[str, Any]:
    """One distinct sentinel per operation, so a name mix-up is detectable."""
    return {name: object() for name in UPI_OPERATION_NAMES}


class AnnotationSurfaceTests(unittest.TestCase):
    def test_annotations_match_the_slot_tuple_exactly(self) -> None:
        self.assertEqual(
            set(UpiOperations.__annotations__),
            set(UPI_OPERATION_NAMES),
            "UpiOperations.__annotations__ and UPI_OPERATION_NAMES have drifted; "
            "a missing annotation reintroduces reportAttributeAccessIssue in stages.py, "
            "an extra one documents a slot that does not exist",
        )

    def test_every_annotation_is_bare(self) -> None:
        """Each name must resolve to the slot descriptor, never to a default."""
        for name in UPI_OPERATION_NAMES:
            with self.subTest(name=name):
                descriptor = getattr(UpiOperations, name, None)
                self.assertIsInstance(
                    descriptor,
                    types.MemberDescriptorType,
                    f"{name!r} is not a slot descriptor, so the annotation carries a default and shadows __slots__",
                )

    def test_slots_are_the_tuple_itself(self) -> None:
        self.assertIs(UpiOperations.__slots__, UPI_OPERATION_NAMES)

    def test_no_instance_dict(self) -> None:
        ops = build_upi_operations(_fake_namespace())
        self.assertFalse(hasattr(ops, "__dict__"), "the bundle must stay slot-only")


class AssemblyTests(unittest.TestCase):
    def test_every_name_reads_back_the_value_it_was_built_with(self) -> None:
        namespace = _fake_namespace()
        ops = build_upi_operations(namespace)
        for name in UPI_OPERATION_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(ops, name), namespace[name])

    def test_direct_construction_reports_every_missing_name(self) -> None:
        """``__init__`` is the layer that knows the full expected set."""
        namespace = _fake_namespace()
        dropped = (UPI_OPERATION_NAMES[0], UPI_OPERATION_NAMES[-1])
        for name in dropped:
            del namespace[name]
        with self.assertRaises(TypeError) as caught:
            UpiOperations(**namespace)
        message = str(caught.exception)
        for name in dropped:
            self.assertIn(name, message)

    def test_build_from_namespace_fails_on_a_missing_global(self) -> None:
        """``build_upi_operations`` reads the namespace eagerly, so the gap is a KeyError.

        Pinned deliberately: a caller that catches ``TypeError`` around the build
        would not catch this, and the two layers raise different exceptions for
        the same defect.
        """
        namespace = _fake_namespace()
        dropped = UPI_OPERATION_NAMES[-1]
        del namespace[dropped]
        with self.assertRaises(KeyError) as caught:
            build_upi_operations(namespace)
        self.assertIn(dropped, str(caught.exception))

    def test_slots_stay_writable_after_construction(self) -> None:
        """Stage bodies rebind operations; the bundle is a snapshot, not a freeze."""
        ops = build_upi_operations(_fake_namespace())
        replacement = object()
        ops._emit = replacement
        self.assertIs(ops._emit, replacement)

    def test_assigning_an_undeclared_name_is_refused(self) -> None:
        ops = build_upi_operations(_fake_namespace())
        # Deliberate: __slots__ is what turns a typo into AttributeError instead of
        # a silently-accepted attribute, so the violation is the assertion.
        with self.assertRaises(AttributeError):
            ops._upi_not_a_real_operation = object()  # type: ignore[reportAttributeAccessIssue]


if __name__ == "__main__":
    unittest.main()

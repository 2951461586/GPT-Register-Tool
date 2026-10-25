"""The lazy registration facade must not drift from the module that owns the names.

``sms_tool/registration/__init__.py`` resolves its exports through PEP 562, so
three hand-maintained surfaces have to agree -- and nothing used to check them:

* the ``TYPE_CHECKING`` import list (what pyright sees),
* ``_LAZY`` (what ``__getattr__`` will actually resolve), and
* ``__all__`` (the public contract; ``__dir__`` is derived from it).

Adding a name to the orchestrator without touching all three either hides it from
type checking, hides it from ``from sms_tool.registration import X``, or advertises
something ``__getattr__`` cannot resolve -- all silently, because the facade is
only exercised through whichever names a test happens to import. These tests parse
nothing that the runtime does not already hold: they import the two modules and
compare the real objects, so a failure names the exact drifted entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sms_tool.registration as facade
import sms_tool.registration.orchestrator as orchestrator


def test_public_contract_matches_the_orchestrator():
    """``registration.__all__`` is the orchestrator's public contract, no more."""
    assert set(facade.__all__) == set(orchestrator.__all__)
    assert len(facade.__all__) == len(set(facade.__all__)), "duplicate entries in __all__"


def test_lazy_map_and_type_checking_list_are_the_same_set():
    """``_LAZY`` is what ``__getattr__`` resolves; the TYPE_CHECKING import list
    is what the type checker sees. A name in one and not the other is either
    invisible to pyright or unimportable at runtime."""
    assert isinstance(facade._LAZY, dict), "_LAZY must stay a name -> module mapping"
    documented = _type_checking_names()
    assert set(facade._LAZY) == documented, (
        "_LAZY and the TYPE_CHECKING import list disagree: "
        f"lazy-only={sorted(set(facade._LAZY) - documented)}, "
        f"type-checking-only={sorted(documented - set(facade._LAZY))}"
    )


def test_every_lazy_name_resolves_on_the_orchestrator():
    """A stub name in ``_LAZY`` would raise first-use AttributeError in production."""
    missing = [name for name in sorted(facade._LAZY) if not hasattr(orchestrator, name)]
    assert missing == [], f"_LAZY names missing from the orchestrator: {missing}"


def test_every_lazy_name_points_at_the_orchestrator():
    assert set(facade._LAZY.values()) == {"orchestrator"}


def test_the_lazy_map_is_a_superset_of_the_public_contract():
    """Underscore-prefixed re-exports stay available but out of ``__all__``."""
    assert set(facade.__all__) <= set(facade._LAZY)
    assert {name for name in facade.__all__ if name.startswith("_")} == set()


def test_dir_matches_the_public_contract():
    """``__dir__`` is derived from ``__all__``; a mismatch breaks tab completion
    and the documentation generators that enumerate the facade."""
    assert set(dir(facade)) == set(facade.__all__)


def _type_checking_names() -> set[str]:
    """Names imported from the orchestrator under ``if TYPE_CHECKING``.

    Read from the source rather than the runtime because the block never executes.
    Only the import list is parsed (no evaluation), so a syntax change inside the
    block fails loudly instead of silently reducing this test to a no-op.
    """
    source = Path(facade.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            names: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module == "orchestrator":
                    names.update(alias.name for alias in child.names)
            assert names, "the TYPE_CHECKING block imports no orchestrator names"
            return names
    raise AssertionError("registration/__init__.py lost its TYPE_CHECKING import block")


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False

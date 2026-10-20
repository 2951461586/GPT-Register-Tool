"""The two lazy package facades stay lazy, and stay visible to the type checker.

Why this exists
---------------
``sms_tool.auth_flow`` and ``sms_tool.upi_link`` re-export their submodules' names
from ``__init__``. Importing them eagerly closed a package facade cycle: the
package imported its submodules and every submodule imported the package
(``from . import deps``), which is why both appeared as strongly connected
components in ``docs/current/dependency-graph.md``.

PEP 562 ``__getattr__`` breaks it at import time. The trade-off is that a static
checker cannot see the names, so each ``__init__`` also carries an
``if TYPE_CHECKING:`` block -- which the dependency graph classifies as type-only
and therefore does not count as a runtime edge.

Three properties are pinned here: the package imports no submodule eagerly, every
exported name resolves, and every exported name is also declared in the
TYPE_CHECKING block (otherwise pyright silently degrades to ``Any`` in consumers).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

FACADES = ("sms_tool.auth_flow", "sms_tool.upi_link")


def _module(qualname: str):
    __import__(qualname)
    return sys.modules[qualname]


@pytest.mark.parametrize("qualname", FACADES)
def test_importing_the_package_does_not_import_its_submodules(qualname):
    """The cycle-breaking property, measured in a fresh interpreter."""
    code = f"import {qualname}, sys;print([m for m in sys.modules if m.startswith({qualname!r} + '.')])"
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "[]", f"{qualname} eagerly imported {output.strip()}"


@pytest.mark.parametrize("qualname", FACADES)
def test_every_exported_name_resolves(qualname):
    module = _module(qualname)
    assert module.__all__, "a facade with no exports is a silent removal"
    for name in module.__all__:
        value = getattr(module, name)
        assert value is not None


@pytest.mark.parametrize("qualname", FACADES)
def test_an_unknown_name_raises_attribute_error(qualname):
    module = _module(qualname)
    with pytest.raises(AttributeError):
        getattr(module, "definitely_not_exported")


@pytest.mark.parametrize("qualname", FACADES)
def test_the_type_checking_block_declares_every_export(qualname):
    """Without this, pyright reports "unknown import symbol" in every consumer."""
    path = PROJECT_ROOT / Path(*qualname.split(".")) / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom):
                    declared.update(alias.asname or alias.name for alias in child.names)
    exported = set(_module(qualname).__all__)
    assert exported - declared == set()

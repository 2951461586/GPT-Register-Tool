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

FACADES = (
    "sms_tool.auth_flow",
    "sms_tool.upi_link",
    # 2026-10-23: the registration and payment-link facades joined the same
    # pattern. ``registration`` used ``from .orchestrator import *`` (691 ms /
    # 431 modules for a leaf submodule import, 378 ms of it curl_cffi) and
    # ``pay_link`` imported all seven submodules eagerly (411 ms / 333 modules).
    "sms_tool.registration",
    "sms_tool.pay_link",
)


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


def test_link_manager_shell_does_not_import_pay_link_eagerly():
    """``payment.link_manager`` is the compatibility shell for ``pay_link``.

    It used ``from ..pay_link import *``, so ``import
    sms_tool.payment.link_manager`` loaded the whole payment lane (461 ms / 329
    modules) even for a caller that only wanted ``__all__``. PEP 562 resolves
    each exported name on first access; this pins that the shell itself imports
    no ``pay_link`` module.
    """
    code = (
        "import sms_tool.payment.link_manager, sys;print([m for m in sys.modules if m.startswith('sms_tool.pay_link')])"
    )
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "[]", f"payment.link_manager eagerly imported {output.strip()}"


# --------------------------------------------------------------------------
# module-level facades (a module, not a package)
# --------------------------------------------------------------------------

#: ``sms_tool.registration.orchestrator`` keeps code of its own (the entry points),
#: so it cannot be a pure package facade: the names its own functions read stay
#: eager, and only the compatibility re-exports nothing reads are lazy.
MODULE_FACADES = ("sms_tool.registration.orchestrator",)


@pytest.mark.parametrize("qualname", MODULE_FACADES)
def test_a_module_facade_declares_a_lazy_map(qualname):
    """A module facade with no map has silently gone back to eager imports."""
    module = _module(qualname)
    assert getattr(module, "_MODULE_EXPORTS", None), f"{qualname} declares no lazy re-exports"


@pytest.mark.parametrize("qualname", MODULE_FACADES)
def test_every_public_name_of_a_module_facade_resolves(qualname):
    module = _module(qualname)
    for name in module.__all__:
        assert getattr(module, name) is not None, f"{qualname}.{name} does not resolve"


@pytest.mark.parametrize("qualname", MODULE_FACADES)
def test_a_module_facade_dir_is_its_all(qualname):
    module = _module(qualname)
    assert dir(module) == sorted(module.__all__)


@pytest.mark.parametrize("qualname", MODULE_FACADES)
def test_a_module_facade_keeps_its_lazy_names_out_of_the_module_dict(qualname):
    """The lazy half must not be bound at import time -- that is the whole point.

    Asserted in a *fresh interpreter*: importing the facade in-process would find
    several of these names already bound by whatever test ran before.
    """
    code = f"import {qualname} as m;print(sorted(set(m._MODULE_EXPORTS) & set(vars(m))))"
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "[]", f"{qualname} bound lazy re-exports eagerly: {output.strip()}"


@pytest.mark.parametrize("qualname", MODULE_FACADES)
def test_a_module_facade_caches_a_lazy_name_after_first_access(qualname):
    module = _module(qualname)
    name = next(iter(module._MODULE_EXPORTS))
    getattr(module, name)
    assert name in vars(module), "the resolved value must be cached in globals() so patches are seen"


def test_the_auth_flow_facade_resolves_a_submodule_on_demand():
    """``patch.object(auth_flow.deps, ...)`` must not depend on some earlier import.

    Before this fallback the attribute existed only if another module had already
    run ``from . import deps``: moving one unrelated import line flipped three tests
    in ``test_existing_login_rate_limit.py`` (measured 2026-11-01). Asserted in a
    fresh interpreter so an earlier test cannot bind it for us.
    """
    code = "import sms_tool.auth_flow as f;print(f.deps.__name__, f.sentinel_flow.__name__)"
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "sms_tool.auth_flow.deps sms_tool.auth_flow.sentinel_flow", output


def test_the_auth_flow_facade_still_imports_no_submodule_eagerly():
    """The fallback must stay lazy: loading the package may not pull a submodule."""
    code = "import sms_tool.auth_flow, sys;print([m for m in sys.modules if m.startswith('sms_tool.auth_flow.')])"
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "[]", f"the facade eagerly imported {output.strip()}"


def test_patching_a_facade_submodule_attribute_works_without_prior_imports():
    """The exact shape the rate-limit tests use, in a fresh interpreter."""
    code = (
        "from unittest.mock import patch;"
        "import sms_tool.auth_flow as f;"
        "p = patch.object(f.deps, 'request_with_retry', lambda *a, **k: 'patched');"
        "p.start();"
        "print(f.deps.request_with_retry())"
    )
    output = subprocess.check_output([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), text=True)
    assert output.strip() == "patched", output

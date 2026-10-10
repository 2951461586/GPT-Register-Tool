"""A package's ``__all__`` must not re-export a stdlib or ``typing`` name.

The mechanical splits that produced ``paypal_link`` / ``gen_pp_link`` /
``paypal_reconciliation`` generated ``__all__`` from the *pre-split module's
global namespace*, which included that module's own imports.  The result was a
payment package whose public surface contained ``json``, ``re``, ``os``, ``sys``,
``html``, ``hashlib``, ``Any``, ``Optional``, ``Protocol``, ``Sequence``,
``Mapping``, ``Enum``, ``HTMLParser``, ``Path``, ``dataclass``, ``annotations``,
``parse_qs``, ``unquote``, ``urljoin`` and ``urlsplit`` -- 20 names per shell, on
three shells.  ``from sms_tool.paypal_link import *`` therefore shadowed the
caller's own ``json``/``re``/``os``.

Nothing consumed them (checked by AST import scan, attribute access, and string
literals), so they were removed on 2026-10-10.  This gate keeps them out.

What it deliberately does **not** flag:

* an ordinary constant whose *type* happens to be stdlib -- ``ENV_PATH`` is a
  ``pathlib.Path``, ``EMAIL_RE`` a compiled pattern, ``_LOGGER`` a ``Logger``,
  ``_STATE_LOCK`` a lock.  The type of a constant is not a leak; only the
  stdlib *name itself* is.
* a sibling ``sms_tool`` submodule in ``__all__`` (``providers.mailbox_gmail``
  and friends), which is a re-export decision rather than an accident.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STDLIB = set(sys.stdlib_module_names)
_LANGUAGE_CONSTRUCT_OWNERS = {"typing", "__future__"}


def _leaked_names(module) -> list[tuple[str, str]]:
    """``(name, owner)`` for every ``__all__`` entry that is a stdlib name."""
    leaked: list[tuple[str, str]] = []
    for name in getattr(module, "__all__", None) or []:
        try:
            obj = getattr(module, name)
        except AttributeError:
            continue
        if isinstance(obj, types.ModuleType):
            if not obj.__name__.startswith("sms_tool"):
                leaked.append((name, f"module {obj.__name__}"))
            continue
        if inspect.isclass(obj) or inspect.isroutine(obj):
            owner = getattr(obj, "__module__", None) or ""
            if owner.split(".")[0] in STDLIB:
                leaked.append((name, owner))
            continue
        # ``Optional`` is a ``typing._SpecialForm`` and ``annotations`` a
        # ``__future__._Feature``: language constructs are *instances*, so the
        # giveaway is ``type(obj).__module__``.  That is also why an ordinary
        # stdlib-typed constant stays unflagged -- its type is ``pathlib`` /
        # ``re`` / ``logging``, none of which are language constructs.
        owner = getattr(type(obj), "__module__", None) or ""
        if owner.split(".")[0] in _LANGUAGE_CONSTRUCT_OWNERS:
            leaked.append((name, owner))
    return leaked


def _module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _iter_package_modules():
    for path in sorted((ROOT / "sms_tool").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        name = _module_name(path.relative_to(ROOT))
        try:
            yield name, importlib.import_module(name)
        except Exception:  # noqa: BLE001 - an unimportable module is another gate's problem
            continue


# --------------------------------------------------------------------------
# The predicate, both directions
# --------------------------------------------------------------------------


class _Fake:
    """A namespace shaped like a module, for predicate fixtures."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)
        self.__all__ = list(attrs)


def test_predicate_flags_a_stdlib_module():
    import json as json_module

    assert _leaked_names(_Fake(json=json_module)) == [("json", "module json")]


def test_predicate_flags_a_stdlib_class_and_function():
    import dataclasses
    from pathlib import Path as PathClass

    assert ("Path", "pathlib") in _leaked_names(_Fake(Path=PathClass))
    assert ("dataclass", "dataclasses") in _leaked_names(_Fake(dataclass=dataclasses.dataclass))


def test_predicate_flags_language_constructs():
    import __future__
    from typing import Optional

    assert ("Optional", "typing") in _leaked_names(_Fake(Optional=Optional))
    assert ("annotations", "__future__") in _leaked_names(_Fake(annotations=__future__.annotations))


def test_predicate_ignores_ordinary_constants_typed_by_stdlib():
    """A ``Path``/pattern/logger *instance* is a constant, not a leaked name."""
    import logging
    import re
    import threading
    from pathlib import Path as PathClass

    fake = _Fake(
        ENV_PATH=PathClass("/tmp/x"),
        EMAIL_RE=re.compile("x"),
        _LOGGER=logging.getLogger("t"),
        _STATE_LOCK=threading.Lock(),
    )
    assert _leaked_names(fake) == []


def test_predicate_ignores_sibling_sms_tool_submodules():
    from sms_tool.providers import mailbox_gmail

    assert _leaked_names(_Fake(mailbox_gmail=mailbox_gmail)) == []


# --------------------------------------------------------------------------
# The real tree
# --------------------------------------------------------------------------


def test_no_module_exports_a_stdlib_name():
    offenders = {name: found for name, module in _iter_package_modules() if (found := _leaked_names(module))}
    assert offenders == {}, (
        "module(s) re-export a stdlib/typing name in __all__; that name is "
        "exported only because the module imported it, and it shadows the "
        f"caller's own binding under ``import *``: {offenders}"
    )


def test_the_paypal_link_shells_still_export_their_real_surface():
    """Guard against the fix overshooting and emptying the surface.

    Removing the 20 leaked names must not have removed a real one.
    """
    import sms_tool.gen_pp_link as gen_pp_link
    import sms_tool.paypal_link as paypal_link

    for module in (paypal_link, gen_pp_link):
        for name in ("generate_pp_link", "PPLinkExtractor", "CURRENCY_MAP", "DEFAULT_STRIPE_PK"):
            assert name in module.__all__, f"{module.__name__} lost {name}"
            assert hasattr(module, name)

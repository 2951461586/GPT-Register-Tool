"""One owner for the "code/contract defect, not network" exception tuple.

Why this exists
---------------
``kernel.error_classification`` answers *which failure class a message belongs
to*. It does not answer *which exception types are code defects* -- that decision
is made by the stage executors, which label an escaping exception by its type:

* a type in the tuple -> ``{stage}_internal:<Type>:<msg>`` (terminal, never
  retried);
* anything else -> ``{stage}_transport:<msg>``, handed to the marker vocabulary.

Three call sites made that decision independently (``registration/handlers.py``
``_run_stage``, ``registration/bootstrap_stages.py`` ``run_bootstrap`` and
``payment/batch.py`` ``_worker_exception_row``), each carrying a comment telling
the next reader to keep the list in sync with the other two.  That is the exact
shape that produced the 2026-09-12 defect: ``IndexError`` fell into the transport
arm, so a code defect was retried as a network failure and burned an exit.

Since 2026-10-25 the tuple has one owner, ``INTERNAL_EXCEPTION_TYPES`` in
``kernel/error_classification.py``, and this file keeps it that way:

* the constant is the documented 9 types, in one place;
* every internal/transport call site imports it from the owner (no local
  redefinition, no copy-pasted literal);
* **no** module in ``sms_tool/`` re-inlines the tuple anywhere -- a fourth copy
  is what this guard exists to prevent, and it would be written by someone who
  never saw the three-way comment;
* the marker vocabulary names every exception type, so the label the executor
  produces is the label the classifier matches.

``payment/batch.py`` imports the constant inside ``_worker_exception_row`` (the
sanctioned remedy for a single call site: the module-level edge would grow
``import_layer_ratchet``'s frozen count).  The guard therefore checks *where* the
import lives instead of insisting on module scope.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sms_tool.kernel.error_classification import (  # noqa: E402
    INTERNAL_ERROR_MARKERS,
    INTERNAL_EXCEPTION_TYPES,
)

PACKAGE = ROOT / "sms_tool"
OWNER = PACKAGE / "kernel" / "error_classification.py"
CONSTANT = "INTERNAL_EXCEPTION_TYPES"

#: importer (repo-relative) -> how it must reach the constant.
#:
#: ``module``    -- a module-scope ``from ... import INTERNAL_EXCEPTION_TYPES``
#: ``function``  -- a function-local import (allowed, but it must exist)
CALL_SITES = {
    "sms_tool/registration/handlers.py": "module",
    "sms_tool/registration/bootstrap_stages.py": "module",
    "sms_tool/payment/batch.py": "function",
}

#: Runtime identity, written out so a silent reordering/extension is visible in
#: the diff of *this* file rather than only in a live classification.
EXPECTED_TYPES = (
    NameError,
    AttributeError,
    ImportError,
    KeyError,
    TypeError,
    IndexError,
    UnboundLocalError,
    NotImplementedError,
    RecursionError,
)

#: Names that mark a tuple-shaped copy of the internal types.
_TYPE_NAMES = {item.__name__ for item in EXPECTED_TYPES}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_constant(node: ast.AST, constant: str = CONSTANT) -> bool:
    """True when ``node`` is a ``from <...>kernel.error_classification import <constant>``."""
    if not isinstance(node, ast.ImportFrom):
        return False
    module = node.module or ""
    if not module.endswith("kernel.error_classification"):
        return False
    return any(alias.name == constant for alias in node.names)


def _module_scope_constants(tree: ast.Module, constant: str = CONSTANT) -> bool:
    return any(_imports_constant(node, constant) for node in tree.body)


def _function_scope_constants(tree: ast.Module, constant: str = CONSTANT) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_imports_constant(child, constant) for child in ast.walk(node)):
                return True
    return False


def _tuple_copies(tree: ast.Module) -> list[int]:
    """Line numbers of tuple literals that look like a copy of the internal types."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        names = [element.id for element in node.elts if isinstance(element, ast.Name)]
        if len(names) != len(node.elts):
            continue
        if len(names) >= 3 and set(names) <= _TYPE_NAMES:
            hits.append(getattr(node, "lineno", 0))
    return hits


def test_the_constant_is_the_documented_tuple():
    assert isinstance(INTERNAL_EXCEPTION_TYPES, tuple)
    assert INTERNAL_EXCEPTION_TYPES == EXPECTED_TYPES
    for item in INTERNAL_EXCEPTION_TYPES:
        assert isinstance(item, type), item
        assert issubclass(item, BaseException), item
    # The label the executors build is ``..._internal:<Type>:``; the classifier
    # matches those names, so a type without a marker is classified as transport.
    for item in INTERNAL_EXCEPTION_TYPES:
        assert item.__name__.lower() in INTERNAL_ERROR_MARKERS, item.__name__


def test_the_owner_defines_it_and_does_not_import_it_from_elsewhere():
    tree = _parse(OWNER)
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        for target in [node.target]
    } | {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert CONSTANT in assigned, f"{CONSTANT} must be defined in {OWNER.relative_to(ROOT)}"
    assert not _module_scope_constants(tree), "kernel.error_classification must own the constant, not import it"


def test_every_call_site_reaches_the_constant_through_the_owner():
    problems: list[str] = []
    for relative, scope in CALL_SITES.items():
        path = ROOT / relative
        tree = _parse(path)
        if scope == "module":
            if not _module_scope_constants(tree):
                problems.append(
                    f"{relative}: expected a module-scope `from ...kernel.error_classification import {CONSTANT}`"
                )
        else:
            if not _function_scope_constants(tree):
                problems.append(f"{relative}: expected a function-local import of {CONSTANT}")
        if _tuple_copies(tree):
            problems.append(f"{relative}: re-inlines the tuple at line(s) {_tuple_copies(tree)}")
    assert not problems, "\n".join(problems)


def test_no_module_re_inlines_the_internal_exception_tuple():
    """A fourth copy is the failure mode, so the scan is package-wide."""
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if any(part in {"__pycache__", "_vendor"} for part in path.parts):
            continue
        if path == OWNER:  # the one place the literal is allowed to live
            continue
        for line in _tuple_copies(_parse(path)):
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "these modules hand-roll the internal-exception tuple instead of importing "
        f"{CONSTANT} from kernel.error_classification:\n" + "\n".join(offenders)
    )


def test_the_stage_executors_label_through_the_constant():
    """The ``except`` clause itself must name the constant, not a subset."""
    problems: list[str] = []
    for relative in ("sms_tool/registration/handlers.py", "sms_tool/registration/bootstrap_stages.py"):
        tree = _parse(ROOT / relative)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == CONSTANT
        ]
        if not handlers:
            problems.append(f"{relative}: no `except {CONSTANT}` handler found")
    assert not problems, "\n".join(problems)

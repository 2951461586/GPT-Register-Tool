"""Guard against names that are read but never bound anywhere in the module.

Why this exists
---------------
Three shipped bugs of exactly this shape were found on 2026-09-02, all in code
paths that handle real money:

1. ``sms_tool/paypal/form_steps.py`` called ``random.uniform`` / ``re.sub``
   without importing either. Five fill steps raised ``NameError`` mid-payment;
   callers wrap steps in a bare ``except``, so a payment was silently abandoned
   halfway - no crash, no log line.
2. ``sms_tool/paypal/config_picker.py`` had the same missing ``random`` / ``re``
   imports. ``_generate_alias_email()`` - which the PayPal orchestrator calls
   before it ever attempts a charge - raised ``NameError`` outright.
3. ``sms_tool/paypal/orchestrator.py:180`` read ``use_headless``, which was never
   bound in that function. The *preferred* payment strategy was 100% dead.

The common trait: none of them can be caught by a test that does not happen to
execute that exact line, and this project's paid paths have thin coverage. A
static pass is the cheap durable check.

A fourth bug of the same family is checked below:

4. ``sms_tool/paypal/orchestrator.py`` imported ``from .nodriver_paypal`` with a
   single dot, but that module lives in ``sms_tool/``, not ``sms_tool/paypal/``.
   It was a lazy in-function import, so the module imported fine and only blew
   up when the line ran - i.e. only after the reverse protocol had already
   failed. That escaped ``auto_pay``'s fallback chain and killed the Camoufox /
   CloakBrowser attempt that was supposed to come next. See
   ``test_relative_imports_resolve``.

Design notes (why this will not become noise)
---------------------------------------------
The check is deliberately approximate in the *miss* direction. A full
re-implementation of pyflakes is not worth it here, and a noisy guard gets
``--no-verify``'d into irrelevance, which is worse than a quiet one.

  * every name bound anywhere in the module counts as in scope, so closures and
    runtime-injected globals do not fire;
  * annotations are skipped when the module uses
    ``from __future__ import annotations`` (they are never evaluated, so a
    missing ``Callable`` import there is harmless - that was the only false
    positive in the initial run);
  * builtins and dunders are excluded.

Scope: shipped source only (``sms_tool``, ``services``). Test fixtures are not
scanned.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("sms_tool", "services")
SKIP_PARTS = {"__pycache__", ".venv", "dist", "runtime", "tests"}

BUILTINS = frozenset(dir(builtins))
DUNDER_OK = frozenset({
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__all__", "__path__", "__debug__", "__class__", "__module__",
    "__qualname__", "__dict__", "__annotations__", "__await__", "__aiter__",
    "__anext__", "__aenter__", "__aexit__", "__enter__", "__exit__",
})

# Known findings that need a product decision rather than a mechanical fix.
# Each entry must carry a reason; an unexplained entry is a bug in this guard.
KNOWN: dict[tuple[str, str], str] = {}


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if not SKIP_PARTS & set(p.parts))
    return sorted(files)


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _annotation_names(tree: ast.Module) -> set[ast.Name]:
    """Every Name node sitting inside an annotation expression."""
    out: set[ast.Name] = set()
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                nodes.append(node.returns)
            for arg in list(node.args.posonlyargs) + list(node.args.args) + \
                    list(node.args.kwonlyargs):
                if arg.annotation is not None:
                    nodes.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            nodes.append(node.annotation)
    for sub in nodes:
        for inner in ast.walk(sub):
            if isinstance(inner, ast.Name):
                out.add(inner)
    return out


def _module_bindings(tree: ast.Module) -> set[str]:
    """Names bound anywhere at module scope.

    Intentionally over-broad: every local in every function counts. That misses
    shadowing bugs but keeps dynamic globals (``CFG``, context vars, injected
    helpers) from firing.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
    return out


def _unbound_reads(path: Path) -> list[tuple[str, int, str]]:
    """(name, lineno, enclosing callable) for reads with no binding in scope."""
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
        return []  # unparseable source must not break the guard

    bound = _module_bindings(tree)
    skip_nodes = _annotation_names(tree) if _has_future_annotations(tree) else set()

    findings: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            if node in skip_nodes:
                continue
            if node.id in BUILTINS or node.id in DUNDER_OK or node.id in bound:
                continue
            findings.append((node.id, node.lineno, func.name))
    return findings


def _unresolved_relative_imports(path: Path) -> list[tuple[int, str, str]]:
    """(lineno, dotted-target, resolved-path) for `from .x import` that misses.

    Only checked inside subpackages: a wrong level there resolves to a module
    that does not exist, and a lazy in-function import hides it until the line
    actually runs.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
        return []

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        parts = list(path.parent.parts)
        if node.level > 1:
            parts = parts[: -(node.level - 1)]
        base = Path(*parts) if parts else ROOT
        module = node.module or ""
        candidate_file = base / (module.replace(".", "/") + ".py") if module \
            else base / "__init__.py"
        candidate_pkg = (base / module / "__init__.py") if module else None
        if candidate_file.exists():
            continue
        if candidate_pkg is not None and candidate_pkg.exists():
            continue
        findings.append((
            node.lineno,
            "." * node.level + module,
            candidate_file.relative_to(ROOT).as_posix(),
        ))
    return findings


def _collect() -> list[tuple[str, int, str, str]]:
    out: list[tuple[str, int, str, str]] = []
    for path in _iter_source_files():
        rel = path.relative_to(ROOT).as_posix()
        for name, lineno, func in _unbound_reads(path):
            reason = KNOWN.get((rel, name))
            if reason is None:
                out.append((rel, lineno, name, func))
    return out


def _collect_imports() -> list[tuple[str, int, str, str]]:
    out: list[tuple[str, int, str, str]] = []
    for path in _iter_source_files():
        # Only subpackages have a parent to get wrong.
        if path.parent == ROOT / "sms_tool" or path.parent == ROOT / "services":
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, target, resolved in _unresolved_relative_imports(path):
            out.append((rel, lineno, target, resolved))
    return out


FINDINGS = _collect()
IMPORT_FINDINGS = _collect_imports()


def test_guard_scanned_a_real_amount_of_source():
    """Meta-check: a broken scanner would make every assertion below vacuous."""
    assert len(_iter_source_files()) > 100, (
        "the scanner found almost no files - path filters are wrong"
    )


@pytest.mark.parametrize(
    "rel,lineno,target,resolved",
    IMPORT_FINDINGS,
    ids=[f"{r}:{n} {t}" for r, n, t, _ in IMPORT_FINDINGS],
)
def test_relative_imports_resolve(rel: str, lineno: int, target: str, resolved: str):
    assert False, (
        f"{rel}:{lineno} does `from {target} import ...`, which resolves to "
        f"{resolved} - that file does not exist. A lazy in-function import like "
        f"this still imports the module fine and only fails when the line runs, "
        f"so it stays invisible until that specific branch is taken. This "
        f"shipped once: paypal/orchestrator.py used `from .nodriver_paypal` for a "
        f"module that lives in `sms_tool/`, which broke the whole nodriver "
        f"fallback after a failed reverse-protocol payment."
    )


@pytest.mark.parametrize(
    "rel,lineno,name,func",
    FINDINGS,
    ids=[f"{r}:{n} {nm}" for r, n, nm, _ in FINDINGS],
)
def test_no_unbound_name_is_read(rel: str, lineno: int, name: str, func: str):
    assert False, (
        f"{rel}:{lineno} reads {name!r} inside {func}() but nothing in the module "
        f"binds it. Three shipped bugs looked exactly like this (missing "
        f"`import random`/`re` in paypal/form_steps.py and paypal/config_picker.py, "
        f"and an undefined `use_headless` in paypal/orchestrator.py) - all in paid "
        f"paths, all invisible until that line ran. Add the missing import or bind "
        f"the name; if the finding is intentional, add it to KNOWN with a reason."
    )

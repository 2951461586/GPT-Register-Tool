"""No module may carry a dead ``try: <import> / except ImportError: <mirror copy>``.

Why this exists
---------------
About 40 import blocks across 16 payment/UPI/PayPal modules were written as:

```python
try:
    from ..payment.wire import _new_session          # package path
except ImportError:                                   # pragma: no cover
    from sms_tool.payment.wire import _new_session    # "direct script execution"
```

The comment claims the fallback covers ``python sms_tool/paypal/extract.py``.
It never could: running a file as a script puts **that file's directory** on
``sys.path[0]``, not the repository root, so the second import fails exactly
like the first one.  ``python sms_tool/upi_link/verify.py`` cannot resolve
``sms_tool.payment.wire`` for the same reason it cannot resolve
``payment.wire``.  Measured 2026-10-23: the fallback branch is unreachable in
all three execution modes this repository actually uses (``-m``, package
import, ``pytest``).

Two of them had already rotted further: the shadow copy imported modules that
do not exist anywhere in the tree (``paths``, ``timeouts``, ``sanitizer``,
``constants``, ``operations``, ``verify``, ``env``).  A dead branch that
references a missing module is invisible -- it only fails if it ever runs,
which it never does.

The cost was not only 40 dead blocks: the ``except ImportError`` also swallowed
the *real* failure modes of that import (a renamed symbol, a new cycle), and the
``# type: ignore`` on the shadow copy told pyright to stop checking the whole
statement.

What is asserted
----------------
1. the detector flags the exact pattern on synthetic samples;
2. the detector does **not** flag the shapes that are legitimate in this tree
   (an ``except`` that assigns ``None`` for an optional third-party extra, or a
   fallback that imports a *different* distribution);
3. no module under ``sms_tool/`` or ``services/`` still carries the mirror copy.

If a genuinely script-executable module ever needs a fallback, it will be a
``python -m`` invocation or a ``sys.path`` bootstrap inside the file -- neither
of which looks like a shadow import, so this guard stays valid.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("sms_tool", "services")
SKIP_PARTS = frozenset({".venv", "__pycache__", ".git", "node_modules"})


def _imported_symbols(statements: list[ast.stmt]) -> set[str] | None:
    """Symbols bound by an all-``ImportFrom`` block, or ``None`` if not one."""
    symbols: set[str] = set()
    for statement in statements:
        if not isinstance(statement, ast.ImportFrom):
            return None
        for alias in statement.names:
            symbols.add(alias.asname or alias.name)
    return symbols


def mirror_fallbacks(tree: ast.AST) -> list[tuple[int, set[str]]]:
    """``(lineno, symbols)`` for every try/except that re-imports the same names."""
    found: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if len(node.handlers) != 1 or node.orelse or node.finalbody:
            continue
        handler = node.handlers[0]
        handled = handler.type
        names = handled.elts if isinstance(handled, ast.Tuple) else [handled]
        if not any(isinstance(item, ast.Name) and item.id == "ImportError" for item in names):
            continue
        guarded = _imported_symbols(node.body)
        if not guarded:
            continue
        fallback = _imported_symbols(handler.body)
        if fallback is None:
            continue
        if fallback and fallback <= guarded:
            found.append((node.lineno, fallback))
    return found


def _scan(path: Path) -> list[tuple[int, set[str]]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return []
    return mirror_fallbacks(tree)


def _iter_sources():
    for top in SOURCE_DIRS:
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def test_detector_flags_the_mirror_copy():
    sample = """
try:
    from ..payment.wire import _new_session
except ImportError:  # pragma: no cover - direct script execution
    from sms_tool.payment.wire import _new_session  # type: ignore
"""
    found = mirror_fallbacks(ast.parse(sample))
    assert found == [(2, {"_new_session"})], found


def test_detector_flags_a_partial_mirror_copy():
    sample = """
try:
    from ..pp_link_helpers import DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:
    from pp_link_helpers import DEFAULT_TIMEOUT
"""
    found = mirror_fallbacks(ast.parse(sample))
    assert [symbols for _, symbols in found] == [{"DEFAULT_TIMEOUT"}], found


def test_detector_ignores_an_optional_dependency_seam():
    """This tree's real ``curl_cffi`` / ``psutil`` shape: the handler assigns."""
    sample = """
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None
"""
    assert mirror_fallbacks(ast.parse(sample)) == []


def test_detector_ignores_a_different_distribution():
    """A fallback that imports different names is a genuine portability seam."""
    sample = """
try:
    from ujson import loads
except ImportError:
    from json import loads as json_loads
"""
    assert mirror_fallbacks(ast.parse(sample)) == []


def test_detector_ignores_non_import_statements_in_the_guarded_block():
    sample = """
try:
    from ..payment.routing import method_payment_config
    paypal_cfg = method_payment_config(cfg, "paypal")
except ImportError:
    paypal_cfg = cfg.get("paypal") or {}
"""
    assert mirror_fallbacks(ast.parse(sample)) == []


def test_no_source_file_carries_a_dead_import_fallback():
    offenders = []
    for path in _iter_sources():
        for lineno, symbols in _scan(path):
            rel = path.relative_to(ROOT).as_posix()
            offenders.append(f"  {rel}:{lineno} -> {sorted(symbols)}")
    assert not offenders, (
        "dead `except ImportError` mirror copies found. Running a module as a script "
        "puts that module's directory on sys.path[0], so the fallback can never "
        "resolve -- use a plain package import (or `python -m`) and delete the "
        "handler:\n" + "\n".join(offenders)
    )

"""Source-hygiene guards for defect classes no other gate can see.

**Why the docstring guard exists.**  A bare string is only a docstring when it is
the *first* statement of a function/class body.  During the 2026-09 extraction
splits a ``from x import y`` line was inserted *ahead of* three docstrings,
turning them into discarded expressions: ``__doc__`` became ``None`` and the
explanation was silently lost (one of them took five class docstrings with it).

Nothing else notices.  The code still runs, the ruff gate
(``select = ["E9", "F63", "F7", "F82"]``) does not cover it, and
``inspect.getdoc`` only reveals it at runtime.

**Scope is deliberately narrow.**  Only *bare* string expressions sitting
directly in a function/class body are flagged.  A long string that is assigned
or passed as an argument is an ordinary value and is never reported -- that is
what keeps this guard free of false positives on logging messages and format
strings.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("sms_tool", "services", "scripts", "tests")
MIN_LENGTH = 25
SKIP_PARTS = frozenset({".venv", "__pycache__", "node_modules", ".git"})


def _iter_sources():
    for top in SOURCE_DIRS:
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def _misplaced_docstrings(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Bare strings that read as docstrings but are not in first position."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if ast.get_docstring(node) is not None:
            continue
        for index, statement in enumerate(node.body):
            if index == 0 or not isinstance(statement, ast.Expr):
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if len(value.value) >= MIN_LENGTH:
                    first_line = value.value.strip().splitlines()[0][:60]
                    found.append((statement.lineno, node.name, first_line))
    return found


def test_no_misplaced_docstrings():
    offenders: list[str] = []
    for path in _iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        try:
            # utf-8-sig, not utf-8: a stray BOM would otherwise be read as
            # U+FEFF and make this guard silently *skip* the file it was
            # supposed to check. Round 5 recorded exactly that failure mode.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError as exc:  # pragma: no cover - defensive
            offenders.append(f"{rel}:{exc.lineno}: syntax error")
            continue
        for lineno, owner, text in _misplaced_docstrings(tree):
            offenders.append(f"{rel}:{lineno}: {owner}() -> {text!r}")
    assert not offenders, (
        "these bare strings look like docstrings but are not the first statement of "
        "their body, so __doc__ is None and the text is lost. Move them to the top "
        "of the body (or delete them):\n  " + "\n  ".join(offenders)
    )


def test_no_utf8_bom_in_python_sources():
    """A BOM is legal for CPython but blinds every plain-UTF-8 AST tool.

    PEP 263 tolerates a UTF-8 BOM in ``.py`` files, so imports still work and
    no test goes red -- but ``ast.parse`` on text decoded with ``encoding="utf-8"``
    raises ``SyntaxError: invalid non-printable character U+FEFF``. Round 5
    (2026-09-02, P2-4) recorded five such files; two were cleaned, three were
    not. This guard closes the class instead of the instance.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _iter_sources()
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    assert not offenders, (
        "these Python files start with a UTF-8 BOM, which makes ast.parse() fail "
        "for any tool reading them as plain UTF-8. Strip the first three bytes:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_detects_a_planted_defect():
    """Mutation check: a guard that cannot fail is not a guard."""
    planted = ast.parse(
        "def f():\n"
        "    from x import y\n"
        '    """A long explanatory docstring that landed in the wrong place."""\n'
    )
    assert _misplaced_docstrings(planted) == [
        (3, "f", "A long explanatory docstring that landed in the wrong place.")
    ]


def test_guard_accepts_a_real_docstring_and_ignores_values():
    benign = ast.parse(
        "def documented():\n"
        '    """A long docstring sitting exactly where it belongs."""\n'
        "    return 1\n"
        "\n"
        "def values_only():\n"
        "    do_something()\n"
        '    message = "A long explanatory string used as a value, not a docstring."\n'
        '    log("Another long string that is an argument, not a docstring.")\n'
        '    """too short"""\n'
    )
    assert _misplaced_docstrings(benign) == []


def _self_named_free_functions(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "self":
                found.append((node.lineno, node.name))
    return found


def test_no_module_level_function_takes_a_self_parameter():
    """A free function whose first parameter is ``self`` is a de-self leftover.

    The registration stage modules were extracted out of
    ``RegistrationEmailWorkflow`` as module-level functions but kept the method's
    ``self`` parameter. That reads as a method of an object the module does not
    own, and it hides the instance behind ``Any`` in a way the type checker
    cannot verify. The convention (2026-10-18) is a first parameter named
    ``workflow``, annotated with the workflow class under ``TYPE_CHECKING``.
    """
    offenders: list[str] = []
    for path in _iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError as exc:  # pragma: no cover - defensive
            offenders.append(f"{rel}:{exc.lineno}: syntax error")
            continue
        for lineno, name in _self_named_free_functions(tree):
            offenders.append(f"{rel}:{lineno}: {name}()")
    assert not offenders, (
        "module-level functions must not take a `self` parameter: name the "
        "workflow instance `workflow` and annotate it under TYPE_CHECKING:\n  "
        + "\n  ".join(offenders)
    )


def test_self_parameter_guard_detects_a_planted_defect():
    """Mutation check: the guard must fail on the shape it forbids."""
    planted = ast.parse(
        "class Workflow:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "def stage(self: object) -> None:\n"
        "    return None\n"
    )
    assert _self_named_free_functions(planted) == [(5, "stage")]

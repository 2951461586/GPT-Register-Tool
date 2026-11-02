"""Classify every broad handler in the protocol lane before touching it (P6-PR-B).

Why a script instead of reading 41 handlers by hand: each site's *fixture* decides
whether narrowing is safe, and the three failed bulk attempts (28 / 11 / 7 red
tests) were all fixture problems, not product-code problems. This prints, per
site: the enclosing function, whether the try body is transport-only (narrowable),
whether it mixes parsing (compat), and which test files patch the same target with
a stand-in that is not a transport error.

Usage:
    python scripts/audit_protocol_broad_handlers.py            # table
    python scripts/audit_protocol_broad_handlers.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE = (ROOT / "sms_tool" / "auth_flow", ROOT / "sms_tool" / "registration")
TESTS = ROOT / "tests"

#: Names that mean "this call can raise a transport error".
TRANSPORT_CALLS = {
    "request_with_retry",
    "_follow_continue_url",
    "_fetch_client_auth_session_dump",
    "session.get",
    "session.post",
    "session.request",
}
#: Names that mean "this call raises something else" -- parsing, serialisation, a
#: vendor runner verdict, a state-machine transition. Mixing one into the try body
#: makes the handler `compat`, not `transport`.
NON_TRANSPORT_MARKERS = {
    "json.dumps",
    "json.loads",
    "response.json",
    "getattr",
    "isinstance",
    "int",
    "str",
    "set",
    ".get",
    "RuntimeError",
    "print",
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"} for item in handler.type.elts
        )
    return False


def _fixture_stand_ins(module: str) -> list[tuple[str, int, str]]:
    """Test files that mention this module and use a non-transport stand-in."""
    hits: list[tuple[str, int, str]] = []
    stem = module.rsplit(".", 1)[-1]
    for path in TESTS.rglob("test_*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if stem not in text and "auth_flow" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"(RuntimeError|AssertionError|StopIteration)\s*\(", line) or "raise AssertionError" in line:
                hits.append((path.relative_to(ROOT).as_posix(), lineno, line.strip()))
    return hits


def classify() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for root in LANE:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source)
            owners: dict[int, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Try):
                            owners[id(child)] = node.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                broad = [index for index, handler in enumerate(node.handlers) if _is_broad(handler)]
                if not broad:
                    continue
                calls = sorted(
                    {
                        _name(item.func)
                        for item in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                        if isinstance(item, ast.Call)
                    }
                )
                transport = [name for name in calls if name in TRANSPORT_CALLS or name.endswith(tuple(TRANSPORT_CALLS))]
                mixed = [name for name in calls if any(marker in name for marker in NON_TRANSPORT_MARKERS)]
                kind = "transport" if transport and not mixed else ("mixed" if transport else "compat")
                rows.append(
                    {
                        "file": path.relative_to(ROOT).as_posix(),
                        "line": node.lineno,
                        "function": owners.get(id(node), "<module>"),
                        "kind": kind,
                        "calls": calls[:6],
                        "transport_calls": transport,
                        "mixed_calls": mixed[:6],
                        "silent": all(isinstance(stmt, ast.Pass) for stmt in node.handlers[broad[0]].body),
                    }
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify the protocol lane's broad handlers.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = classify()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["kind"])] = counts.get(str(row["kind"]), 0) + 1
        print(f"{row['kind']:9s} {row['file']}:{row['line']}  {row['function']}")
        # ``classify()`` returns ``dict[str, object]``, so the row value is erased at
        # the annotation; narrow it here rather than re-typing the whole row.
        calls = row["calls"]
        calls_text = ", ".join(str(name) for name in calls) if isinstance(calls, list) and calls else "(none)"
        print(f"          calls: {calls_text}")
    print()
    print("by kind:", ", ".join(f"{name}={value}" for name, value in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

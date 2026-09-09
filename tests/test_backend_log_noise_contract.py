"""``SmsWorkbench/BackendLogPresenter.cs`` noise prefixes must stay real.

``NoiseLinePrefixes`` is a hand-maintained list of whole-line prefixes the
WPF log panel never shows. It is a **duplicate of knowledge that lives in
Python**: every entry exists because some ``print()`` in ``sms_tool/`` emits
that line. When the Python side stops emitting one, the C# entry silently
becomes dead weight -- and nobody notices, because a filter that never
matches looks exactly like a filter that works.

This module pins the two sides together. It is deliberately **one-directional**
(C# prefixes must have a Python emitter); the reverse is not true, because a
print that is *not* in the blacklist is simply a line the operator wants to
see.

Why this instead of just converting the prints to ``logger`` calls
-------------------------------------------------------------------
``cli.py:418-424`` configures logging with ``to_console=False`` on purpose --
stdout is the WPF host's IPC channel. Routing these prints through the logger
would remove them from the CLI's progress output entirely, which is a user
visible change, not a refactor. Until that trade-off is decided, the honest
thing is to keep the two lists from drifting apart.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSHARP = ROOT / "SmsWorkbench" / "BackendLogPresenter.cs"
PYTHON_ROOT = ROOT / "sms_tool"

# The array literal in BackendLogPresenter.cs.
_NOISE_BLOCK = re.compile(
    r"NoiseLinePrefixes\s*=\s*\{(?P<body>.*?)\}",
    re.DOTALL,
)
_STRING_ENTRY = re.compile(r'"((?:[^"\\]|\\.)*)"')

# print(...) calls -- the first argument may be a literal **or a variable**
# that was assigned a literal a few lines up. ``auth_flow.py`` builds
# ``line = f"  Protocol diagnostic[...]: ..."`` and then calls ``print(line)``;
# an earlier revision of this extractor only looked at literals and reported
# the prefix as orphaned, which was a false positive.
_LITERAL = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"
_PRINT_CALL = re.compile(
    rf"\bprint\s*\(\s*(?:f)?(?P<arg>{_LITERAL}|[A-Za-z_][A-Za-z0-9_]*)"
)
_ASSIGN_LITERAL = re.compile(
    rf"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:f)?(?P<lit>{_LITERAL})",
    re.MULTILINE,
)
_FSTRING_TEXT = re.compile(r"[A-Za-z\[\*]")  # literal head of an f-string


def _literal_head(raw: str) -> str:
    """Leading literal run of a format string, before the first interpolation."""
    return re.split(r"\{|\}", raw)[0].strip()


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def csharp_noise_prefixes() -> list[str]:
    """Prefixes the WPF panel filters out, in declaration order."""
    text = _strip_comments(CSHARP.read_text(encoding="utf-8"))
    match = _NOISE_BLOCK.search(text)
    if not match:
        return []
    return [
        s.replace("\\[", "[").replace("\\]", "]")
        for s in _STRING_ENTRY.findall(match.group("body"))
    ]


def python_emitted_lines() -> list[str]:
    """Literal line heads that ``sms_tool/`` can print.

    Only the leading literal run is kept: for ``print(f"  Existing account OTP
    send: {x}")`` that is ``Existing account OTP send:`` -- exactly the shape
    the C# prefix list records.
    """
    heads: list[str] = []
    for path in PYTHON_ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # name -> literal head, for `line = f"..."` followed by `print(line)`.
        assigned: dict[str, str] = {}
        for match in _ASSIGN_LITERAL.finditer(text):
            head = _literal_head(match.group("lit")[1:-1])
            if head:
                assigned[match.group("name")] = head

        for match in _PRINT_CALL.finditer(text):
            arg = match.group("arg")
            if arg[:1] in ("\"", "'"):
                head = _literal_head(arg[1:-1])
            else:
                head = assigned.get(arg, "")
            if head and _FSTRING_TEXT.match(head):
                heads.append(head)
    return heads


def _matches(prefix: str, heads: list[str]) -> bool:
    return any(h.startswith(prefix) or prefix.startswith(h) for h in heads)


class NoisePrefixContract(unittest.TestCase):
    def test_extractors_are_not_empty(self):
        """An empty extraction turns every assertion below into a tautology."""
        self.assertGreater(len(csharp_noise_prefixes()), 8)
        self.assertGreater(len(python_emitted_lines()), 100)

    def test_every_filtered_prefix_has_a_python_emitter(self):
        heads = python_emitted_lines()
        orphaned = [p for p in csharp_noise_prefixes() if not _matches(p, heads)]
        self.assertEqual(
            [],
            orphaned,
            "C# filters prefixes that no print() in sms_tool/ can emit; "
            "they are dead entries and should be removed from NoiseLinePrefixes",
        )

    def test_prefixes_are_unique(self):
        prefixes = csharp_noise_prefixes()
        dupes = {p for p in prefixes if prefixes.count(p) > 1}
        self.assertEqual(set(), dupes, f"duplicate noise prefixes: {sorted(dupes)}")


if __name__ == "__main__":
    unittest.main()

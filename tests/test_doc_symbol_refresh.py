"""Tests for ``scripts/refresh_doc_symbol_lines.py`` -- the *fix* half of the doc gate.

``docs_consistency_scan`` detects drift but only inside table rows, and it can never
repair anything. The generator rewrites both tiers from the symbol each pointer
names. Its danger is the mirror image of the gate's: a rewriter that guesses wrong
silently *writes* a wrong number. These tests pin the three rails that prevent it.
"""
from __future__ import annotations

from pathlib import Path

import scripts.docs_consistency_scan as scan
import scripts.refresh_doc_symbol_lines as refresh


def _rewrite(tmp_path, monkeypatch, body: str, sources: dict[str, str]) -> str:
    """Run the generator over one synthetic doc; return the rewritten text."""
    src = tmp_path / "sms_tool"
    src.mkdir(exist_ok=True)
    for name, text in sources.items():
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    monkeypatch.setattr(scan, "SRC", src)
    monkeypatch.setattr(scan, "DOCS", ("architecture.md",))
    (tmp_path / "architecture.md").write_text(body, encoding="utf-8")
    return refresh._rewrite(body, {})[0]


def _source(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def test_prose_pointer_is_rewritten_to_the_real_line(tmp_path, monkeypatch):
    """The case the gate cannot see: `` `Widget()`（`widget.py:1`） `` in a sentence."""
    out = _rewrite(
        tmp_path,
        monkeypatch,
        "见 `Widget()`（`widget.py:1`）定义。\n",
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    assert out == "见 `Widget()`（`widget.py:2`）定义。\n"


def test_table_row_pointer_is_rewritten(tmp_path, monkeypatch):
    out = _rewrite(
        tmp_path,
        monkeypatch,
        "| `Widget` | `widget.py:1` | a widget |\n",
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    assert out == "| `Widget` | `widget.py:2` | a widget |\n"


def test_bare_line_shorthand_in_a_table_row_is_rewritten(tmp_path, monkeypatch):
    """``/ `:5` `` repeats the previous file -- the number span is group 3, not 2."""
    out = _rewrite(
        tmp_path,
        monkeypatch,
        "| `A` / `B` | `w.py:1` / `:1` | x |\n",
        {"w.py": _source("# pad", "A = 1", "", "", "B = 2")},
    )
    assert out == "| `A` / `B` | `w.py:2` / `:5` | x |\n"


def test_each_pointer_pairs_with_its_own_nearest_name(tmp_path, monkeypatch):
    """Two pointers on one line must not both bind to the first name."""
    out = _rewrite(
        tmp_path,
        monkeypatch,
        "`_get_cached()`（`w.py:1`） / `_save()`（`w.py:1`）\n",
        {"w.py": _source("# header", "def _get_cached():", "    pass", "", "def _save():", "    pass")},
    )
    assert out == "`_get_cached()`（`w.py:2`） / `_save()`（`w.py:5`）\n"


def test_unknown_symbol_is_left_alone(tmp_path, monkeypatch):
    """Rail #1: a name that is not defined in that file cannot fabricate a number."""
    body = "See `Ghost`（`widget.py:1`）.\n"
    out = _rewrite(
        tmp_path,
        monkeypatch,
        body,
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    assert out == body


def test_json_pointers_are_never_rewritten(tmp_path, monkeypatch):
    """Rail #3: config.json is gitignored and user-edited, even when a symbol precedes it."""
    body = "`Widget` 的配置在 `config.json:1`。\n"
    out = _rewrite(
        tmp_path,
        monkeypatch,
        body,
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    assert out == body


def test_resolver_refuses_non_python_files_even_when_parseable(tmp_path):
    """Rail #3, pinned at the guard itself.

    The end-to-end case above never reaches the ``ext`` check -- the synthetic
    tree has no ``config.json``, so ``_resolve_source`` returns ``None`` first.
    This one hands the resolver a real, parseable file and asserts the extension
    rail still refuses it, *before* anything is parsed: config.json is
    user-owned, so its line numbers are not ours to renumber.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text("Widget = 1\n", encoding="utf-8")  # valid Python, still not ours
    cache: dict[Path, dict[str, int]] = {}
    assert refresh._resolve(cfg, "json", "Widget", cache) is None
    assert cfg not in cache  # short-circuited -- never handed to ast.parse


def test_pointer_without_a_preceding_name_is_left_alone(tmp_path, monkeypatch):
    """A bare pointer is prose-anchored into a function body; nothing to bind to."""
    body = "See `widget.py:1` for details.\n"
    out = _rewrite(
        tmp_path,
        monkeypatch,
        body,
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    assert out == body


def test_a_pointer_directly_after_another_pointer_is_left_alone(tmp_path, monkeypatch):
    """Rail #2: with no name in between, the shorthand cannot be attributed."""
    body = "见 `AAA`（`w.py:9`）与 `:1`。\n"
    out = _rewrite(
        tmp_path,
        monkeypatch,
        body,
        {"w.py": _source("", "", "", "", "", "", "", "", "AAA = 1")},
    )
    assert out == body


def test_rewritten_doc_passes_the_consistency_gate(tmp_path, monkeypatch):
    """The two halves must agree: what the generator writes, the gate accepts."""
    body = (
        "| `Widget` | `widget.py:1` | a widget |\n"
        "见 `Widget()`（`widget.py:1`）定义。\n"
    )
    out = _rewrite(
        tmp_path,
        monkeypatch,
        body,
        {"widget.py": _source("# header", "class Widget:", "    pass")},
    )
    (tmp_path / "architecture.md").write_text(out, encoding="utf-8")
    failures: list[str] = []
    scan.check_line_refs(failures)
    scan.check_symbol_tables(failures)
    assert failures == []


def test_check_mode_reports_drift_without_writing(tmp_path, monkeypatch):
    doc = tmp_path / "architecture.md"
    doc.write_text("见 `Widget()`（`widget.py:1`）定义。\n", encoding="utf-8")
    src = tmp_path / "sms_tool"
    src.mkdir(exist_ok=True)
    (src / "widget.py").write_text(_source("# header", "class Widget:", "    pass"), encoding="utf-8")
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    monkeypatch.setattr(scan, "SRC", src)
    monkeypatch.setattr(scan, "DOCS", ("architecture.md",))

    assert refresh._scan(apply=False) == 1
    assert doc.read_text(encoding="utf-8") == "见 `Widget()`（`widget.py:1`）定义。\n"


def test_apply_mode_rewrites_the_file_and_is_idempotent(tmp_path, monkeypatch):
    doc = tmp_path / "architecture.md"
    doc.write_text("见 `Widget()`（`widget.py:1`）定义。\n", encoding="utf-8")
    src = tmp_path / "sms_tool"
    src.mkdir(exist_ok=True)
    (src / "widget.py").write_text(_source("# header", "class Widget:", "    pass"), encoding="utf-8")
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    monkeypatch.setattr(scan, "SRC", src)
    monkeypatch.setattr(scan, "DOCS", ("architecture.md",))

    assert refresh._scan(apply=True) == 0
    assert doc.read_text(encoding="utf-8") == "见 `Widget()`（`widget.py:2`）定义。\n"
    assert refresh._scan(apply=True) == 0  # second pass finds nothing left to do


def test_live_docs_have_no_drift_left():
    """The generator is part of CI: any future refactor that moves a symbol trips this."""
    assert refresh._scan(apply=False) == 0

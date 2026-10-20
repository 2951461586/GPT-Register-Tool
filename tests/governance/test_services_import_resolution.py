"""Pin the resolver for ``services/protocol-payment``'s ``common.*`` import root.

Why this exists
---------------
``services/protocol-payment`` is not a package (the hyphen makes it
unimportable), so extractors put the directory on ``sys.path`` and import
``common.*`` as a top-level package. Static analysers that do not know that
extra root report every ``common/*`` module as dead code -- the 2026-09-22 and
2026-10-04 scans both had to refute that by hand.

``scripts/services_import_resolution.py`` resolves those imports with the
runtime's own rule. These tests pin the resolver's edge cases so the gate
cannot pass by resolving nothing: an unresolved import, an unreachable module,
and a same-package relative import each get their own case.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import services_import_resolution as resolver  # noqa: E402  # type: ignore

COMMON = "common"


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write a throwaway ``services/protocol-payment`` tree and return its root."""
    for rel, text in files.items():
        path = tmp_path / "protocol-payment" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path / "protocol-payment"


def analyze(root: Path):
    return resolver.analyze(root=root, common_dir=root / COMMON)


def test_the_committed_tree_resolves_and_has_no_orphans():
    assert resolver.main([]) == 0


def test_a_resolvable_import_makes_the_module_reachable(tmp_path):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/helpers.py": "VALUE = 1\n",
            "blik/extract.py": "from common.helpers import VALUE\n",
        },
    )
    unresolved, entry_map, edges, orphans = analyze(root)
    assert unresolved == []
    assert orphans == []
    assert entry_map["blik/extract.py"] == {"common.helpers"}
    assert edges == {}


def test_an_unresolved_import_is_reported_with_its_line(tmp_path):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/helpers.py": "VALUE = 1\n",
            "blik/extract.py": "from common.helpers import VALUE\nfrom common.missing import NOPE\n",
        },
    )
    unresolved, _entry, _edges, _orphans = analyze(root)
    assert [(path.name, line, spec) for path, line, spec in unresolved] == [("extract.py", 2, "common.missing")]


def test_a_module_no_extractor_reaches_is_an_orphan(tmp_path):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/helpers.py": "VALUE = 1\n",
            "common/forgotten.py": "VALUE = 2\n",
            "blik/extract.py": "from common.helpers import VALUE\n",
        },
    )
    _unresolved, _entry, _edges, orphans = analyze(root)
    assert orphans == ["common.forgotten"]


def test_a_relative_import_inside_common_keeps_the_target_reachable(tmp_path):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/base.py": "VALUE = 1\n",
            "common/derived.py": "from .base import VALUE\n",
            "blik/extract.py": "from common.derived import VALUE\n",
        },
    )
    _unresolved, _entry, edges, orphans = analyze(root)
    assert orphans == []
    assert edges == {"common.derived": {"common.base"}}


def test_stdlib_and_third_party_imports_are_not_edges(tmp_path):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/helpers.py": "import json\nimport requests\nVALUE = json.dumps({})\n",
            "blik/extract.py": "import os\nfrom common.helpers import VALUE\n",
        },
    )
    _unresolved, _entry, edges, orphans = analyze(root)
    assert edges == {}
    assert orphans == []


def test_main_fails_and_names_the_unresolved_spec(tmp_path, monkeypatch, capsys):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "blik/extract.py": "from common.missing import NOPE\n",
        },
    )
    monkeypatch.setattr(resolver, "PROTOCOL_ROOT", root)
    monkeypatch.setattr(resolver, "COMMON_DIR", root / COMMON)
    assert resolver.main([]) == 1
    captured = capsys.readouterr()
    assert "common.missing" in captured.err
    assert "blik/extract.py:1" in captured.err


def test_main_fails_and_names_the_orphan(tmp_path, monkeypatch, capsys):
    root = build(
        tmp_path,
        {
            "common/__init__.py": "",
            "common/helpers.py": "VALUE = 1\n",
            "common/forgotten.py": "VALUE = 2\n",
            "blik/extract.py": "from common.helpers import VALUE\n",
        },
    )
    monkeypatch.setattr(resolver, "PROTOCOL_ROOT", root)
    monkeypatch.setattr(resolver, "COMMON_DIR", root / COMMON)
    assert resolver.main([]) == 1
    assert "common.forgotten" in capsys.readouterr().err

"""Pin the generated dependency graph and its agreement with the ratchet.

Why this exists
---------------
``docs/current/dependency-graph.md`` is generated, so the interesting failures
are (a) a stale committed copy and (b) the generator drifting away from
``import_layer_ratchet``'s resolver -- two answers to "what imports what" is
exactly the defect the generated file was meant to remove.

The two scripts count different units on purpose: the ratchet counts import
*statements* per directory pair (it ratchets statements), while the graph keeps
one edge per (source module, target module) pair (it needs module granularity
for cycles). What must agree is the **set** of ordered directory pairs and the
**set** of mutually dependent pairs; those are asserted against the ratchet
directly, so a resolver change on either side fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import dependency_graph as graph  # noqa: E402  # type: ignore
import import_layer_ratchet as ratchet  # noqa: E402  # type: ignore


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write a throwaway repo tree and return its root."""
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


def test_the_committed_document_is_current():
    assert graph.main([]) == 0


def test_directory_pairs_agree_with_the_ratchet():
    built = graph.build()
    mine = {
        (graph.directory_of(source), graph.directory_of(target))
        for source, targets in built.edges.items()
        for target in targets
        if graph.directory_of(source) != graph.directory_of(target)
    }
    theirs = {(edge.source_dir, edge.target_dir) for edge in ratchet.analyze().module_edges}
    assert mine == theirs


def test_mutual_pairs_agree_with_the_ratchet():
    assert graph.mutual_directory_pairs(graph.build()) == ratchet.analyze().mutual_pairs()


def test_relative_import_in_a_module_resolves_to_the_parent_package(tmp_path):
    root = build(
        tmp_path,
        {
            "sms_tool/__init__.py": "",
            "sms_tool/geo/__init__.py": "",
            "sms_tool/geo/helper.py": "VALUE = 1\n",
            "sms_tool/alpha.py": "from .geo import helper\n",
        },
    )
    built = graph.build(root)
    # Both are real: the statement imports the package and binds its submodule.
    assert built.edges["sms_tool.alpha"] == {"sms_tool.geo", "sms_tool.geo.helper"}


def test_relative_import_in_a_package_resolves_to_itself(tmp_path):
    root = build(
        tmp_path,
        {
            "sms_tool/__init__.py": "",
            "sms_tool/geo/__init__.py": "from . import helper\n",
            "sms_tool/geo/helper.py": "VALUE = 1\n",
        },
    )
    built = graph.build(root)
    assert built.edges["sms_tool.geo"] == {"sms_tool.geo.helper"}


def test_a_function_local_import_is_delayed_not_a_cycle_edge(tmp_path):
    root = build(
        tmp_path,
        {
            "sms_tool/__init__.py": "",
            "sms_tool/alpha.py": "def f():\n    from . import beta\n    return beta\n",
            "sms_tool/beta.py": "from . import alpha\n",
        },
    )
    built = graph.build(root)
    assert built.edges == {"sms_tool.beta": {"sms_tool", "sms_tool.alpha"}}
    # ``from . import beta`` binds the package too, so the delayed set has both.
    assert built.delayed == {"sms_tool.alpha": {"sms_tool", "sms_tool.beta"}}
    # beta -> alpha alone is not a cycle; the delayed half must not create one.
    assert graph.strongly_connected(built) == []


def test_a_star_import_names_no_module(tmp_path):
    root = build(
        tmp_path,
        {
            "sms_tool/__init__.py": "",
            "sms_tool/alpha.py": "from . import *\n",
        },
    )
    assert graph.build(root).edges == {}


def test_cycle_detection_is_symmetric_and_complete(tmp_path):
    root = build(
        tmp_path,
        {
            "sms_tool/__init__.py": "",
            "sms_tool/a.py": "from . import b\n",
            "sms_tool/b.py": "from . import c\n",
            "sms_tool/c.py": "from . import a\n",
            "sms_tool/d.py": "from . import a\n",
        },
    )
    built = graph.build(root)
    assert graph.strongly_connected(built) == [["sms_tool.a", "sms_tool.b", "sms_tool.c"]]


def test_render_is_stable_across_calls():
    assert graph.render(graph.build()) == graph.render(graph.build())

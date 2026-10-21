"""Tests for ``scripts/spec_validate.py``.

Layers, in increasing order of "does it protect anything":

1. **The real tree is clean** -- every spec resolves, no cycle. A validator that
   reports nothing on a broken graph protects nothing, so the rest of this file
   drives the failure paths on synthetic trees.
2. **Each check can go red** -- a dangling rule, a dangling ADR, a dependency on
   a spec that does not exist, a `depends-on` cycle, an id that disagrees with
   its filename, a missing owner path, a missing frontmatter block, a duplicate
   id. Each produces its own message.
3. **The shape is pinned** -- the required keys, the allowed `type` and
   `status`, and the frontmatter subset the parser accepts.

Why the synthetic tree rather than editing `docs/specs/`: the gate has to be
tested in the state a future commit will create, not only in the state it
happens to be in today.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import spec_validate as sv  # noqa: E402  # type: ignore

FRONTMATTER = """---
id: spec-{stem}
type: architecture-design
status: accepted
owner: sms_tool/owner.py
implements:
  - {rule}
depends-on:
{depends}---

body
"""


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the validator at a synthetic repo and hand back a spec writer."""
    for relative in ("docs/specs", "docs/rules", "docs/adr", "sms_tool"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sms_tool" / "owner.py").write_text("", encoding="utf-8")
    (tmp_path / "docs" / "rules" / "rule-01-transport.md").write_text("", encoding="utf-8")
    (tmp_path / "docs" / "adr" / "0009-registration-hardening.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    monkeypatch.setattr(sv, "SPECS", tmp_path / "docs" / "specs")
    monkeypatch.setattr(sv, "RULES", tmp_path / "docs" / "rules")
    monkeypatch.setattr(sv, "ADRS", tmp_path / "docs" / "adr")

    def write(stem: str, *, rule: str = "rule-01", depends: tuple[str, ...] = (), body: str | None = None) -> Path:
        depends_block = "".join(f"  - {item}\n" for item in depends)
        text = body if body is not None else FRONTMATTER.format(stem=stem, rule=rule, depends=depends_block)
        path = tmp_path / "docs" / "specs" / f"{stem}.md"
        path.write_text(text, encoding="utf-8")
        return path

    return write


# --------------------------------------------------------------------------
# The real tree
# --------------------------------------------------------------------------


def test_the_real_spec_graph_is_clean():
    assert sv.validate() == []


def test_every_real_spec_declares_the_required_keys():
    by_id, problems = sv.collect()
    assert problems == []
    assert len(by_id) >= 10, "the plan calls for at least ten boundaries"
    for spec_id, data in by_id.items():
        assert spec_id.startswith("spec-")
        assert data["type"] == "architecture-design"
        assert data["status"] in sv.STATUSES
        assert (ROOT / str(data["owner"])).exists(), spec_id
        assert sv._refs(data, "implements"), f"{spec_id} implements no rule"


# --------------------------------------------------------------------------
# Each check can go red
# --------------------------------------------------------------------------


def test_a_clean_synthetic_tree_passes(tree):
    tree("01-transport")
    assert sv.validate() == []


def test_dangling_rule_reference_is_reported(tree):
    tree("01-transport", rule="rule-99")
    problems = sv.validate()
    assert any("rule-99" in p and "no docs/rules" in p for p in problems), problems


def test_dangling_adr_reference_is_reported(tree):
    tree("01-transport", depends=("adr-0099",))
    problems = sv.validate()
    assert any("adr-0099" in p for p in problems), problems


def test_dependency_on_a_missing_spec_is_reported(tree):
    tree("01-transport", depends=("spec-02-missing",))
    problems = sv.validate()
    assert any("spec-02-missing" in p and "names no spec" in p for p in problems), problems


def test_depends_on_cycle_is_reported(tree):
    tree("01-a", depends=("spec-02-b",))
    tree("02-b", depends=("spec-01-a",))
    problems = sv.validate()
    assert any("depends-on cycle" in p for p in problems), problems


def test_id_must_match_the_filename(tree):
    tree("01-transport")
    path = sv.SPECS / "01-transport.md"
    path.write_text(path.read_text(encoding="utf-8").replace("spec-01-transport", "spec-07-other"), encoding="utf-8")
    problems = sv.validate()
    assert any("does not match the filename" in p for p in problems), problems


def test_missing_owner_path_is_reported(tree):
    tree("01-transport")
    path = sv.SPECS / "01-transport.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("sms_tool/owner.py", "sms_tool/absent.py"), encoding="utf-8"
    )
    problems = sv.validate()
    assert any("owner 'sms_tool/absent.py' does not exist" in p for p in problems), problems


def test_missing_frontmatter_is_reported(tree):
    tree("01-transport", body="# no frontmatter here\n")
    problems = sv.validate()
    assert any("missing frontmatter block" in p for p in problems), problems


def test_unterminated_frontmatter_is_reported(tree):
    tree("01-transport", body="---\nid: spec-01-transport\n")
    problems = sv.validate()
    assert any("unterminated frontmatter" in p for p in problems), problems


def test_duplicate_id_is_reported(tree):
    tree("01-transport")
    duplicate = sv.SPECS / "02-other.md"
    duplicate.write_text((sv.SPECS / "01-transport.md").read_text(encoding="utf-8"), encoding="utf-8")
    problems = sv.validate()
    assert any("duplicate id" in p for p in problems), problems


def test_unknown_type_and_status_are_reported(tree):
    tree("01-transport")
    path = sv.SPECS / "01-transport.md"
    text = path.read_text(encoding="utf-8").replace("type: architecture-design", "type: blog-post")
    text = text.replace("status: accepted", "status: maybe")
    path.write_text(text, encoding="utf-8")
    problems = sv.validate()
    assert any("type 'blog-post'" in p for p in problems), problems
    assert any("status 'maybe'" in p for p in problems), problems


# --------------------------------------------------------------------------
# The frontmatter subset
# --------------------------------------------------------------------------


def test_parser_accepts_scalars_and_lists():
    data, error = sv.parse_frontmatter(FRONTMATTER.format(stem="01-x", rule="rule-01", depends="  - adr-0009\n"))
    assert error == ""
    assert data["owner"] == "sms_tool/owner.py"
    assert data["implements"] == ["rule-01"]
    assert data["depends-on"] == ["adr-0009"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("no frontmatter\n", "missing frontmatter"),
        ("---\nid: x\n", "unterminated"),
        ("---\njust-a-word\n---\n", "expected 'key: value'"),
        ("---\n- orphan\n---\n", "list item outside a key"),
        ("---\nid: a\nid: b\n---\n", "duplicate key"),
    ],
)
def test_parser_rejects_malformed_frontmatter(body: str, expected: str):
    _data, error = sv.parse_frontmatter(body)
    assert expected in error, error

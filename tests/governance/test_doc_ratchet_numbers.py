"""Guard the generated ratchet-number blocks in the architecture docs.

Why this exists
---------------
``docs/architecture.md`` quoted "287 module-level edges over 31 directory pairs"
long after ``scripts/import_layer_ratchet.py`` measured 267 / 28. Nothing
recomputed the quote, so it drifted silently -- and a document that calls itself
the architecture authority cannot carry numbers nobody checks.

``scripts/refresh_doc_ratchet_numbers.py`` regenerates the numbers inside
marker-delimited blocks. This module pins both directions:

* the committed docs are up to date (a stale block fails);
* a hand edit *inside* a block is caught, and ``--apply`` repairs it;
* the numbers really come from the ratchet scripts -- patching
  ``import_layer_ratchet.analyze`` changes the rendered block, so a generator
  that printed constants could not pass;
* a missing or duplicated marker pair is an error rather than a silent skip.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import refresh_doc_ratchet_numbers as refresh  # noqa: E402  # type: ignore


def sandbox(tmp_path: Path, monkeypatch) -> Path:
    """Copy the configured docs into a scratch tree; point the writer there.

    ``refresh.ROOT`` stays the real repository on purpose: the ratchets must keep
    measuring production code while the documents are rewritten in the sandbox.
    """
    for doc in refresh.BLOCKS:
        target = tmp_path / doc
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / doc, target)
    monkeypatch.setattr(refresh, "DOCS_ROOT", tmp_path)
    return tmp_path


def doc_block(root: Path, doc: str) -> tuple[Path, str, str]:
    path = root / doc
    text = path.read_bytes().decode("utf-8")
    block_id = refresh.BLOCKS[doc]
    block = refresh.extract_block(text, block_id)
    assert block is not None, "sandbox copy lost its markers"
    return path, text, block


def test_the_committed_docs_are_up_to_date():
    assert refresh.main([]) == 0


def test_a_hand_edited_number_is_reported_and_repaired(tmp_path, monkeypatch):
    root = sandbox(tmp_path, monkeypatch)
    path, text, block = doc_block(root, "docs/architecture.md")
    # Value-agnostic on purpose: pinning one literal here turned every ratchet
    # change into a second, unrelated edit in this test.
    edited = re.sub(r"\| \d+ \|", "| 999 |", block, count=1)
    assert edited != block, "the block has no numeric cell to tamper with"
    path.write_bytes(text.replace(block, edited, 1).encode("utf-8"))

    assert refresh.main([]) == 1, "a hand-edited block must fail the check"
    assert refresh.main(["--apply"]) == 0
    assert refresh.main([]) == 0
    assert "| 999 |" not in path.read_bytes().decode("utf-8")


def test_the_numbers_come_from_the_ratchet_scripts(tmp_path, monkeypatch):
    """A generator that printed constants would pass every other test here."""

    class FakeAnalysis:
        def totals(self):
            return {
                "total_module_edges": 12345,
                "ordered_pairs": 7,
                "mutual_pairs": 1,
                "minority_edges": 2,
                "delayed_cross_dir_edges": 3,
            }

    real_tool = refresh._tool

    def fake_tool(name: str):
        module = real_tool(name)
        if name == "import_layer_ratchet":
            # ``setattr`` rather than ``module.analyze = ...``: pyright rejects
            # assignment to an attribute it does not see declared on ``ModuleType``.
            setattr(module, "analyze", lambda root: FakeAnalysis())
        return module

    monkeypatch.setattr(refresh, "_tool", fake_tool)
    root = sandbox(tmp_path, monkeypatch)

    assert refresh.main([]) == 1
    assert refresh.main(["--apply"]) == 0
    rendered = (root / "docs/architecture.md").read_bytes().decode("utf-8")
    assert "| Module-level cross-directory edges | 12345 |" in rendered
    committed = (PROJECT_ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert "12345" not in committed, "the fake measurement must not leak into the real doc"


def test_missing_markers_are_an_error(tmp_path, monkeypatch):
    root = sandbox(tmp_path, monkeypatch)
    path, text, _block = doc_block(root, "docs/architecture.md")
    block_id = refresh.BLOCKS["docs/architecture.md"]
    path.write_bytes(text.replace(refresh.begin_marker(block_id), "<!-- gone -->", 1).encode("utf-8"))
    assert refresh.main([]) == 1


def test_duplicated_markers_are_an_error(tmp_path, monkeypatch):
    root = sandbox(tmp_path, monkeypatch)
    path, text, block = doc_block(root, "docs/architecture.md")
    path.write_bytes((text + "\n" + block + "\n").encode("utf-8"))
    assert refresh.main([]) == 1

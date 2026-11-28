"""Single-source-of-truth guard for Rust PyO3 crate counts and kernel native loader.

Why this exists
---------------
``AGENTS.md`` states "Facts live in one place": every time a ``rust-anything``
wave adds or expands a crate under ``rust/*-rs/``, the crate count, accelerated
function count, total recorded cases, and gate-check totals appear across six
docs (``README.md``, ``README_EN.md``, ``docs/README.md``,
``docs/current/README.md``, ``docs/directory-map.md``,
``docs/current/rust-acceleration.md``).

This test derives the authoritative facts straight from ``rust/*-rs/``
(``rust-anything.json``, ``cases.jsonl``, ``src/*.rs``) and compares them
against the docs so drift cannot slip past CI.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

from sms_tool.kernel import native_loader

ROOT = Path(__file__).resolve().parents[2]
RUST_DIR = ROOT / "rust"

GATES_PER_CRATE = 12
_UNSAFE_RE = re.compile(r"\bunsafe\b")
_TABLE_ROW_RE = re.compile(
    r"^\|\s*`rust/([a-z0-9_-]+-rs)/`\s*\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*\*\*[^*]+\*\*\s*\|\s*([\d,]+)\s*/\s*([\d,]+)\s*\|\s*12/12 PASS\s*\|$"
)


def _strip_rust_comments(source: str) -> str:
    """Strip ``//`` line comments and ``/* ... */`` block comments."""
    no_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def _collect_rust_facts(rust_dir: Path = RUST_DIR) -> dict[str, dict[str, object]]:
    """Measure every ``rust/*-rs/`` crate directly from its manifest and cases."""
    facts: dict[str, dict[str, object]] = {}
    for crate in sorted(rust_dir.glob("*-rs")):
        manifest_path = crate / "rust-anything.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Split strictly on '\n' (not .splitlines()) because JSON string literals
        # inside cases.jsonl can contain Unicode line separators (\u0085/\u2028).
        cases_path = crate / "cases.jsonl"
        case_lines = [line for line in cases_path.read_text(encoding="utf-8").split("\n") if line.strip()]
        raw_exports = manifest.get("exports") or manifest.get("functions") or []
        functions = [item["name"] if isinstance(item, dict) else str(item) for item in raw_exports]
        unit = str(manifest.get("unit") or "")
        unsafe_files = [
            rs.relative_to(crate).as_posix()
            for rs in sorted((crate / "src").glob("*.rs"))
            if _UNSAFE_RE.search(_strip_rust_comments(rs.read_text(encoding="utf-8")))
        ]
        facts[crate.name] = {
            "unit": unit,
            "functions": functions,
            "cases": len(case_lines),
            "unsafe_files": unsafe_files,
        }
    return facts


def _parse_rust_acceleration_table(text: str) -> dict[str, tuple[str, int]]:
    """Return ``{crate_dir: (unit_path, case_count)}`` from the Markdown table."""
    rows: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        crate_name, unit_path, _fns, lhs, rhs = match.groups()
        assert lhs == rhs, f"{crate_name}: table recorded-case mismatch {lhs} != {rhs}"
        rows[crate_name] = (unit_path, int(lhs.replace(",", "")))
    return rows


def _check_doc_totals(doc_texts: dict[str, str], facts: dict[str, dict[str, object]]) -> list[str]:
    """Return human-readable drift messages when docs disagree with ``facts``."""
    crate_count = len(facts)
    fn_count = sum(len(info["functions"]) for info in facts.values())  # type: ignore[arg-type]
    case_count = sum(int(info["cases"]) for info in facts.values())  # type: ignore[arg-type]
    gate_count = crate_count * GATES_PER_CRATE
    cases_fmt = f"{case_count:,}"

    expected_snippets: dict[str, tuple[str, ...]] = {
        "docs/current/rust-acceleration.md": (
            f"({crate_count} Crates · {fn_count} Functions)",
            f"Total across all {crate_count} crates: **{cases_fmt} / {cases_fmt} recorded cases matched",
            f"**{gate_count} / {gate_count} `rust-anything gate` checks passing**",
        ),
        "README.md": (
            f"[{crate_count} 个 PyO3 热点加速扩展]",
            f"{crate_count} 个 `rust-anything` PyO3 扩展加速 {fn_count} 个热点函数",
            f"（{crate_count} 个 PyO3 热点扩展 · {fn_count} 函数）",
            f"{crate_count} 个由 `rust-anything` 管理的 PyO3 扩展加速 {fn_count} 个热点函数（{cases_fmt} 条录制用例",
        ),
        "README_EN.md": (
            f"[{crate_count} PyO3 hot-path extensions]",
            f"{crate_count} `rust-anything` PyO3 extensions accelerate {fn_count} hot CPU functions",
            f"({crate_count} PyO3 Hot-Path Crates · {fn_count} Functions)",
            f"{crate_count} `rust-anything`-managed PyO3 crates under `rust/` accelerate {fn_count} hot functions ({cases_fmt} recorded cases",
        ),
        "docs/README.md": (f"{crate_count} 个 `rust-anything` PyO3 热点扩展",),
        "docs/current/README.md": (f"{crate_count} 个 `rust-anything` PyO3 热点扩展",),
        "docs/directory-map.md": tuple(f"`{name}`" for name in sorted(facts)),
    }

    problems: list[str] = []
    for rel, snippets in expected_snippets.items():
        body = doc_texts.get(rel, "")
        for snippet in snippets:
            if snippet not in body:
                problems.append(f"{rel}: missing expected snippet {snippet!r}")
    return problems


# --------------------------------------------------------------------------
# Real-tree single-source-of-truth assertions
# --------------------------------------------------------------------------


def test_every_rust_crate_has_zero_unsafe_and_valid_manifest():
    facts = _collect_rust_facts()
    assert facts, "expected rust/*-rs crates under rust/"
    for name, info in facts.items():
        assert info["unit"], f"{name}: missing unit in rust-anything.json"
        assert info["functions"], f"{name}: no functions listed in rust-anything.json"
        assert int(info["cases"]) > 0, f"{name}: empty cases.jsonl"  # type: ignore[arg-type]
        assert info["unsafe_files"] == [], f"{name}: unexpected unsafe block in {info['unsafe_files']}"


def test_rust_acceleration_table_matches_crates_on_disk():
    facts = _collect_rust_facts()
    doc_text = (ROOT / "docs/current/rust-acceleration.md").read_text(encoding="utf-8")
    table = _parse_rust_acceleration_table(doc_text)

    assert set(table) == set(facts), (
        f"crate inventory drift: only in disk={sorted(set(facts) - set(table))}, "
        f"only in doc={sorted(set(table) - set(facts))}"
    )
    for name, info in facts.items():
        doc_unit, doc_cases = table[name]
        assert doc_unit == info["unit"], f"{name}: doc unit {doc_unit!r} != manifest {info['unit']!r}"
        assert doc_cases == info["cases"], f"{name}: doc cases {doc_cases} != cases.jsonl {info['cases']}"


def test_all_six_docs_match_live_rust_totals():
    facts = _collect_rust_facts()
    docs = (
        "README.md",
        "README_EN.md",
        "docs/README.md",
        "docs/current/README.md",
        "docs/directory-map.md",
        "docs/current/rust-acceleration.md",
    )
    doc_texts = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in docs}
    problems = _check_doc_totals(doc_texts, facts)
    assert problems == [], "Rust doc counts drifted from rust/*-rs/:\n  " + "\n  ".join(problems)


def test_drift_detection_catches_stale_doc_count():
    """Mutation test: tampering with a single doc count must fail the check."""
    facts = _collect_rust_facts()
    doc_texts = {
        rel: (ROOT / rel).read_text(encoding="utf-8")
        for rel in (
            "README.md",
            "README_EN.md",
            "docs/README.md",
            "docs/current/README.md",
            "docs/directory-map.md",
            "docs/current/rust-acceleration.md",
        )
    }
    sample_crate = sorted(facts)[0]
    doc_texts["docs/directory-map.md"] = doc_texts["docs/directory-map.md"].replace(
        f"`{sample_crate}`", "`removed-crate-rs`"
    )
    problems = _check_doc_totals(doc_texts, facts)
    assert any("docs/directory-map.md" in item for item in problems)


# --------------------------------------------------------------------------
# Shared kernel native_loader unit tests
# --------------------------------------------------------------------------


def test_native_loader_honors_old_mode_and_cleans_sys_path(monkeypatch):
    monkeypatch.setenv("RUST_ANYTHING_MODE", "old")
    assert (
        native_loader.load_native_module("sanitize-text-rs", "sanitize_text_rs", "RUST_ANYTHING_UNIT_SANITIZE_TEXT")
        is None
    )

    monkeypatch.delenv("RUST_ANYTHING_MODE", raising=False)
    monkeypatch.setenv("RUST_ANYTHING_UNIT_SAMPLE", "original")
    assert native_loader.load_native_module("sanitize-text-rs", "sanitize_text_rs", "RUST_ANYTHING_UNIT_SAMPLE") is None


def test_native_loader_falls_back_or_raises_in_strict_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(native_loader, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("RUST_ANYTHING_MODE", raising=False)
    monkeypatch.delenv("RUST_ANYTHING_UNIT_MISSING", raising=False)
    assert native_loader.load_native_module("missing-rs", "missing_rs", "RUST_ANYTHING_UNIT_MISSING") is None

    monkeypatch.setenv("RUST_ANYTHING_UNIT_MISSING", "new")
    with pytest.raises(ImportError):
        native_loader.load_native_module("missing-rs", "missing_rs", "RUST_ANYTHING_UNIT_MISSING")


def test_native_loader_imports_module_and_restores_sys_path(monkeypatch, tmp_path):
    pkg_dir = tmp_path / "rust" / "demo-rs" / "python"
    pkg_dir.mkdir(parents=True)
    fake_mod = types.ModuleType("demo_rs")
    seen_path: list[bool] = []

    def fake_import(name: str):
        assert name == "demo_rs"
        seen_path.append(str(pkg_dir) in sys.path)
        return fake_mod

    monkeypatch.setattr(native_loader, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(native_loader.importlib, "import_module", fake_import)
    monkeypatch.delenv("RUST_ANYTHING_MODE", raising=False)
    monkeypatch.delenv("RUST_ANYTHING_UNIT_DEMO", raising=False)

    loaded = native_loader.load_native_module("demo-rs", "demo_rs", "RUST_ANYTHING_UNIT_DEMO")
    assert loaded is fake_mod
    assert seen_path == [True]
    assert str(pkg_dir) not in sys.path


def test_no_production_module_duplicates_load_native_helper():
    """Every Rust-accelerated module must delegate to kernel.native_loader."""
    offenders: list[str] = []
    for path in sorted((ROOT / "sms_tool").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "native_loader.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^def _load_native\w*\(", text, re.M):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == [], f"duplicated _load_native_* helper(s): {offenders}"


# --------------------------------------------------------------------------
# scripts/rust_crates_guard.py unit tests
# --------------------------------------------------------------------------


def _load_rust_crates_guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rust_crates_guard", ROOT / "scripts" / "rust_crates_guard.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rust_crates_guard_passes_on_live_repo():
    guard = _load_rust_crates_guard()
    crates, problems = guard.validate_crates()
    assert crates, "expected rust/*-rs crates"
    assert problems == [], f"rust_crates_guard reported problems: {problems}"


def test_rust_crates_guard_catches_unsafe_and_missing_export(tmp_path):
    guard = _load_rust_crates_guard()
    unit_file = tmp_path / "sms_tool" / "demo.py"
    unit_file.parent.mkdir(parents=True)
    unit_file.write_text("def real_fn(x):\n    return x\n\n_alias_fn = real_fn\n", encoding="utf-8")

    crate = tmp_path / "rust" / "demo-rs"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "demo-rs"\nversion = "0.1.0"\n', encoding="utf-8")
    (crate / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (crate / "cases.jsonl").write_text('{"fn":"real_fn","args":[1],"result":1}\n', encoding="utf-8")
    (crate / "rust-anything.json").write_text(
        json.dumps({"unit": "sms_tool/demo.py", "functions": ["real_fn", "_alias_fn", "missing_fn"]}),
        encoding="utf-8",
    )
    (crate / "src" / "lib.rs").write_text(
        "// unsafe in comment is fine\npub fn boom() { unsafe {} }\n",
        encoding="utf-8",
    )

    crates, problems = guard.validate_crates(rust_dir=tmp_path / "rust", repo_root=tmp_path)
    assert len(crates) == 1
    assert any("missing_fn" in p for p in problems), problems
    assert any("forbidden unsafe keyword" in p for p in problems), problems
    assert not any("_alias_fn" in p for p in problems), problems


def test_rust_crates_guard_accepts_shared_workspace_cargo_lock(tmp_path):
    guard = _load_rust_crates_guard()
    unit_file = tmp_path / "sms_tool" / "demo.py"
    unit_file.parent.mkdir(parents=True)
    unit_file.write_text("def real_fn(x):\n    return x\n", encoding="utf-8")

    rust_root = tmp_path / "rust"
    crate = rust_root / "demo-rs"
    (crate / "src").mkdir(parents=True)
    (rust_root / "Cargo.toml").write_text('[workspace]\nresolver = "2"\nmembers = ["demo-rs"]\n', encoding="utf-8")
    (rust_root / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text('[package]\nname = "demo-rs"\nversion = "0.1.0"\n', encoding="utf-8")
    (crate / "cases.jsonl").write_text('{"fn":"real_fn","args":[1],"result":1}\n', encoding="utf-8")
    (crate / "rust-anything.json").write_text(
        json.dumps({"unit": "sms_tool/demo.py", "functions": ["real_fn"]}),
        encoding="utf-8",
    )
    (crate / "src" / "lib.rs").write_text("pub fn real_fn(x: i64) -> i64 { x }\n", encoding="utf-8")

    crates, problems = guard.validate_crates(rust_dir=rust_root, repo_root=tmp_path)
    assert len(crates) == 1
    assert problems == []

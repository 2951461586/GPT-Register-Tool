import scripts.docs_consistency_scan as scan
from scripts.docs_consistency_scan import main


def _scan_doc(tmp_path, monkeypatch, body: str, sources: dict[str, str]) -> list[str]:
    """Run both doc-pointer tiers against a synthetic doc tree."""
    src = tmp_path / "sms_tool"
    src.mkdir(exist_ok=True)
    for name, text in sources.items():
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (tmp_path / "architecture.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    monkeypatch.setattr(scan, "SRC", src)
    monkeypatch.setattr(scan, "DOCS", ("architecture.md",))
    failures: list[str] = []
    scan.check_line_refs(failures)
    scan.check_symbol_tables(failures)
    return failures


def test_live_documentation_pointers_are_current():
    assert main() == 0


def test_symbol_table_pointer_matching_the_source_passes(tmp_path, monkeypatch):
    failures = _scan_doc(
        tmp_path,
        monkeypatch,
        "| `Widget` | `widget.py:1` | a widget |\n",
        {"widget.py": "class Widget:\n    pass\n"},
    )
    assert failures == []


def test_symbol_table_pointer_that_drifted_is_reported(tmp_path, monkeypatch):
    """The regression this gate exists for: the file grew, the pointer did not."""
    failures = _scan_doc(
        tmp_path,
        monkeypatch,
        "| `Widget` | `widget.py:1` | a widget |\n",
        {"widget.py": "# a new header comment\nclass Widget:\n    pass\n"},
    )
    assert len(failures) == 1
    assert "Widget" in failures[0]
    assert "widget.py:2" in failures[0]


def test_pointer_past_end_of_file_is_reported(tmp_path, monkeypatch):
    """Catches a module that was split, e.g. playwright.py:1518 on a 27-line shell."""
    failures = _scan_doc(
        tmp_path,
        monkeypatch,
        "See `widget.py:9999` for details.\n",
        {"widget.py": "class Widget:\n    pass\n"},
    )
    assert len(failures) == 1
    assert "past EOF" in failures[0]


def test_unresolvable_module_is_reported(tmp_path, monkeypatch):
    failures = _scan_doc(
        tmp_path,
        monkeypatch,
        "See `ghost.py:12` for details.\n",
        {"widget.py": "class Widget:\n    pass\n"},
    )
    assert len(failures) == 1
    assert "unresolvable" in failures[0]


def test_json_pointers_are_tracked_but_not_policed(tmp_path, monkeypatch):
    """config.json is gitignored and user-edited; its line numbers are not ours to enforce."""
    failures = _scan_doc(
        tmp_path,
        monkeypatch,
        "Keys live at `config.json:216`, `:227` and `:999`.\n",
        {"widget.py": "class Widget:\n    pass\n"},
    )
    assert failures == []


def test_provider_compatibility_facades_alias_the_implementation_modules():
    import importlib

    for name in ("cfworker", "gmail", "graph", "icloud_url", "remail", "smailr"):
        facade = importlib.import_module(f"sms_tool.mailbox_{name}")
        implementation = importlib.import_module(f"sms_tool.providers.mailbox_{name}")
        assert facade is implementation
    assert importlib.import_module("sms_tool.outlook_imap") is importlib.import_module("sms_tool.providers.outlook_imap_client")

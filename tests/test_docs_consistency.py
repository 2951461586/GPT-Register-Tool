import itertools

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


def _mailbox_contract() -> dict[str, str]:
    path = scan.ROOT / "docs" / "current" / "mailbox.md"
    body = path.read_text(encoding="utf-8")
    table = body.split("<!-- mailbox-contract:start -->", 1)[1].split(
        "<!-- mailbox-contract:end -->", 1
    )[0]
    contract: dict[str, str] = {}
    for line in table.splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] not in {"Key", "---"}:
            contract[cells[0]] = cells[1]
    return contract


def test_mailbox_document_is_live_and_matches_runtime_semantics():
    from types import SimpleNamespace

    from sms_tool.mailbox import _mailbox_proxy_candidates
    from sms_tool.mailbox_errors import (
        MailboxEndpointUnavailableError,
        MailboxErrorDisposition,
        mailbox_error_disposition,
    )
    from sms_tool.mailbox_strategies import (
        FunctionMailboxProviderAdapter,
        MailboxProviderRegistry,
    )

    assert "docs/current/mailbox.md" in scan.DOCS

    registry = MailboxProviderRegistry()
    always = lambda _mailbox, _config: True
    fetch = lambda _mailbox, **_kwargs: []
    registry.register(
        FunctionMailboxProviderAdapter(
            "fallback", always, message_fetcher=fetch, fallback=True
        )
    )
    registry.register(
        FunctionMailboxProviderAdapter("specific", always, message_fetcher=fetch)
    )
    resolved = registry.resolve_fetcher(SimpleNamespace(), {})

    auth_error = RuntimeError("bad credentials")
    auth_error.code = "mailbox_auth_invalid"
    routes = _mailbox_proxy_candidates(
        "http://operation.example:8003",
        {
            "chatgpt": {},
            "mailbox_proxy": "http://mailbox.example:8001",
            "mailbox_proxy_pool": ["http://pool.example:8002"],
            "email_registration": {},
        },
    )
    observed = {
        "provider_resolution": (
            "specific_then_fallback"
            if resolved is not None and resolved.name == "specific"
            else "fallback_first"
        ),
        "auth_invalid": (
            "terminal_quarantine"
            if mailbox_error_disposition(auth_error)
            is MailboxErrorDisposition.AUTH_INVALID
            else "retry_until_deadline"
        ),
        "endpoint_unavailable": (
            "terminal_cooldown"
            if mailbox_error_disposition(MailboxEndpointUnavailableError())
            is MailboxErrorDisposition.ENDPOINT_UNAVAILABLE
            else "retry_until_deadline"
        ),
        "other_errors": (
            "retry_until_deadline"
            if mailbox_error_disposition(RuntimeError("temporary"))
            is MailboxErrorDisposition.RETRY
            else "terminal"
        ),
        "operation_proxy_fallback_default": (
            "true" if routes[-1] == "http://operation.example:8003" else "false"
        ),
        "proxy_order": ",".join(
            {
                "http://mailbox.example:8001": "mailbox_proxy",
                "http://pool.example:8002": "mailbox_proxy_pool",
                "http://operation.example:8003": "operation_proxy",
            }[route]
            for route in routes
        ),
    }

    assert _mailbox_contract() == observed


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


# --------------------------------------------------------------------------- #
# The C# existence tier (`check_csharp_file_refs`).
#
# Why it needs its own tests: it is the only tier that can see a *desktop*
# source file, and the two stronger tiers are structurally blind to C# --
# `check_line_refs` resolves only `.py`/`.json` pointers and
# `check_symbol_tables` only reads Python symbol tables. So a renamed or deleted
# `.cs` left prose that **no gate could see** until this tier existed. Measured
# 2026-09-23: it immediately caught `AccountScanResultInterpreter.cs` still
# listed as a live row three weeks after the file was deleted as zero-reference
# dead code.
#
# It is a deliberately WEAK tier -- existence only, never content -- so the
# tests below pin its three silences as much as its one assertion. A guard that
# reddens on something it cannot judge is worse than no guard: it gets
# `--no-verify`'d into irrelevance.
# --------------------------------------------------------------------------- #


_csharp_tree = itertools.count()


def _scan_csharp_doc(base, monkeypatch, body: str, sources: dict[str, str]) -> list[str]:
    """Run only the C# tier against a synthetic doc tree with its own projects.

    Every call gets its **own** subdirectory. A test that asserts "a missing file
    is reported" right after asserting "a present file is accepted" would
    otherwise still see the first call's file and pass for the wrong reason --
    which is exactly the failure mode the first version of these tests had.
    """
    root = base / f"tree{next(_csharp_tree)}"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (root / "architecture.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(scan, "ROOT", root)
    monkeypatch.setattr(scan, "DOCS", ("architecture.md",))
    monkeypatch.setattr(scan, "CSHARP_ROOTS", ("SmsWorkbench", "SmsWorkbench.Contracts"))
    failures: list[str] = []
    scan.check_csharp_file_refs(failures)
    return failures


def test_a_csharp_file_named_in_a_doc_must_exist(tmp_path, monkeypatch):
    """The regression this tier exists for: the file was renamed, the prose was not."""
    assert (
        _scan_csharp_doc(
            tmp_path,
            monkeypatch,
            "The launcher lives in `SmsWorkbench/WidgetWindow.cs`.\n",
            {"SmsWorkbench/WidgetWindow.cs": "class WidgetWindow {}\n"},
        )
        == []
    )

    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "The launcher lives in `SmsWorkbench/WidgetWindow.cs`.\n",
        {"SmsWorkbench/WidgetWindowRenamed.cs": "class WidgetWindowRenamed {}\n"},
    )
    assert len(failures) == 1
    assert "does not exist" in failures[0]
    assert "WidgetWindow.cs" in failures[0]


def test_a_bare_csharp_name_passes_when_any_project_owns_it(tmp_path, monkeypatch):
    """A filename is not a path claim -- the tier must not infer which project."""
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "Read the grid through `AccountGridPresentation.cs`.\n",
        {"SmsWorkbench.Contracts/AccountGridPresentation.cs": "class AccountGridPresentation {}\n"},
    )
    assert failures == []


def test_a_path_shaped_csharp_ref_does_not_fall_back_to_the_bare_name(tmp_path, monkeypatch):
    """`SmsWorkbench/X.cs` is a path claim; a same-named file elsewhere must not satisfy it."""
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "The dialog is `SmsWorkbench/WidgetDialog.cs`.\n",
        {"SmsWorkbench.Contracts/WidgetDialog.cs": "class WidgetDialog {}\n"},
    )
    assert len(failures) == 1
    assert "SmsWorkbench/WidgetDialog.cs" in failures[0]


def test_a_type_named_without_the_extension_is_not_a_file_claim(tmp_path, monkeypatch):
    """The documented way to write about a deleted file without turning prose into a false pointer."""
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "The retired `AccountScanResultInterpreter` used to own this; do not reintroduce it.\n",
        {"SmsWorkbench/WidgetWindow.cs": "class WidgetWindow {}\n"},
    )
    assert failures == []


def test_generated_copies_do_not_keep_a_deleted_name_alive(tmp_path, monkeypatch):
    """`bin`/`obj` are excluded from the index, or one build would resurrect every deleted name."""
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "The retired `GoneWindow.cs` must not come back.\n",
        {
            "SmsWorkbench/obj/Release/GoneWindow.cs": "class GoneWindow {}\n",
            "SmsWorkbench/bin/Release/GoneWindow.cs": "class GoneWindow {}\n",
        },
    )
    assert len(failures) == 1
    assert "GoneWindow.cs" in failures[0]


def test_glob_shaped_csharp_refs_are_never_policed(tmp_path, monkeypatch):
    """Glob expansion is tool-dependent, so guessing would make the gate lie either way.

    🔴 Note how this silence is actually delivered: `CSHARP_REF`'s character
    classes exclude `*` and `?`, so a glob-shaped ref is never matched in the
    first place and the `if "*" in ref or "?" in ref: continue` guard inside
    `check_csharp_file_refs` is **unreachable today**. That guard is not dead
    weight though -- it becomes load-bearing the moment someone widens the
    regex, and this test is what keeps the *property* (globs are not policed)
    true across such a change.
    """
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "Every dialog lives in `SmsWorkbench/*.cs`, or in `*.cs` at the root.\n",
        {"SmsWorkbench/WidgetWindow.cs": "class WidgetWindow {}\n"},
    )
    assert failures == []


def test_a_bare_name_outside_the_declared_roots_is_not_indexed(tmp_path, monkeypatch):
    """Bare names are resolved against the desktop projects only.

    A path-shaped ref is checked against the real filesystem, so `tools/X.cs`
    passes when the file is there. A *bare* `X.cs` has nothing to resolve
    against except the declared roots -- which is the whole point of naming the
    roots instead of indexing the repository.
    """
    failures = _scan_csharp_doc(
        tmp_path,
        monkeypatch,
        "The generator is `WidgetGen.cs`.\n",
        {"tools/WidgetGen.cs": "class WidgetGen {}\n"},
    )
    assert len(failures) == 1
    assert "WidgetGen.cs" in failures[0]

"""Tests for scripts/delayed_import_ratchet.py.

Like test_precommit_guard.py, the last test scans the real package. CI already
runs pytest, so the ratchet is enforced on every push without touching
.github/workflows/ci.yml.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

from scripts import delayed_import_ratchet

ROOT = Path(__file__).resolve().parents[2]


def _count(source: str) -> int:
    return len(delayed_import_ratchet._delayed_imports(ast.parse(textwrap.dedent(source))))


def test_top_level_imports_are_not_counted():
    assert _count("import os\nimport sys\n") == 0


def test_import_inside_function_is_counted():
    assert (
        _count("""
        def f():
            import os
    """)
        == 1
    )


def test_class_body_import_is_not_delayed():
    # A class body executes at import time, so for cycle and startup purposes
    # it behaves like a top-level import.
    assert (
        _count("""
        class C:
            import os
    """)
        == 0
    )


def test_nested_scopes_inside_a_function_still_count():
    assert (
        _count("""
        def f():
            if True:
                try:
                    from . import x
                except ImportError:
                    import y
    """)
        == 2
    )


def test_baseline_file_is_present_and_populated():
    baseline = delayed_import_ratchet.load_baseline()
    assert int(baseline["total"]) > 0
    assert isinstance(baseline["per_file"], dict)


def test_ratchet_holds_for_the_current_tree():
    total, _per_file = delayed_import_ratchet.count_delayed_imports()
    allowed = int(delayed_import_ratchet.load_baseline()["total"])
    assert total <= allowed, (
        f"delayed imports grew: {total} > baseline {allowed}. "
        "Delete one, or justify a baseline bump via "
        "'python scripts/delayed_import_ratchet.py --update-baseline'."
    )


# --------------------------------------------------------------------------
# Reasons: every edge carries one, and a new "deferred" edge needs a decision
# --------------------------------------------------------------------------


def test_reason_is_derived_for_documented_cycles():
    classify = delayed_import_ratchet.classify
    assert classify("sms_tool/payment/catalog.py", "sms_tool.checkout_contract") == "cycle"
    assert classify("sms_tool/sentinel/client.py", "sms_tool.sentinel_tokens") == "cycle"
    # The five-module cycle's lazy edge.
    assert classify("sms_tool/proxy/routing.py", "sms_tool.accounts.account_identity") == "cycle"


def test_reason_is_startup_for_the_cli_seam():
    classify = delayed_import_ratchet.classify
    assert classify("sms_tool/cli.py", "sms_tool.mailbox.router") == "startup"
    assert classify("sms_tool/commands/payment.py", "sms_tool.payment.batch") == "startup"
    assert classify("sms_tool/cli_parsers/payment.py", "sms_tool.gen_pp_link") == "startup"


def test_reason_is_startup_for_a_heavy_target():
    classify = delayed_import_ratchet.classify
    assert classify("sms_tool/somewhere.py", "playwright") == "startup"
    assert classify("sms_tool/somewhere.py", "curl_cffi.requests") == "startup"


def test_reason_is_provider_for_provider_subsystems():
    classify = delayed_import_ratchet.classify
    assert classify("sms_tool/accounts/account_email_change.py", "sms_tool.providers.mailbox_remail") == "provider"
    assert classify("sms_tool/accounts/account_liveness.py", "sms_tool.registration_drivers.base") == "provider"
    assert classify("sms_tool/somewhere.py", "sms_tool.sentinel") == "provider"
    assert classify("sms_tool/somewhere.py", "sms_tool.sentinel.client") == "provider"


def test_reason_is_test_seam_for_the_store_shell_edge():
    assert delayed_import_ratchet.classify("sms_tool/store/connection.py", "sms_tool.storage") == "test-seam"


def test_reason_defaults_to_deferred_and_that_needs_a_why():
    classify = delayed_import_ratchet.classify
    assert classify("sms_tool/accounts/account_2fa.py", "sms_tool.auth_headers") == "deferred"
    assert classify("sms_tool/mailbox/router.py", "sms_tool.mailbox.parsers") == "deferred"


def _write_reasons(tmp_path: Path, edges: dict) -> Path:
    target = tmp_path / "delayed_import_reasons.json"
    target.write_text(json.dumps({"edges": edges}), encoding="utf-8")
    return target


def test_reasons_file_rejects_an_unknown_reason(tmp_path):
    target = _write_reasons(
        tmp_path,
        {"sms_tool/a.py -> sms_tool.b": {"reason": "because", "why": "this is not a known category"}},
    )
    with pytest.raises(ValueError, match="unknown reason"):
        delayed_import_ratchet.load_reasons(target)


def test_reasons_file_requires_a_written_why(tmp_path):
    target = _write_reasons(
        tmp_path,
        {"sms_tool/a.py -> sms_tool.b": {"reason": "deferred", "why": "short"}},
    )
    with pytest.raises(ValueError, match="why"):
        delayed_import_ratchet.load_reasons(target)


def test_an_override_wins_over_the_derived_reason(tmp_path):
    package = tmp_path / "sms_tool"
    package.mkdir()
    (package / "a.py").write_text("def f():\n    from . import b\n", encoding="utf-8")
    (package / "b.py").write_text("", encoding="utf-8")
    reasons = {"sms_tool/a.py -> sms_tool.b": {"reason": "cycle", "why": "closes a real cycle at import time"}}
    edges = delayed_import_ratchet.collect_delayed_edges(package, reasons)
    assert [(edge.importer, edge.target, edge.reason) for edge in edges] == [("sms_tool/a.py", "sms_tool.b", "cycle")]


def _fake_tree(monkeypatch, edges, total=None):
    monkeypatch.setattr(delayed_import_ratchet, "collect_delayed_edges", lambda root=None, reasons=None: edges)
    monkeypatch.setattr(
        delayed_import_ratchet,
        "count_delayed_imports",
        lambda root=None: (total if total is not None else len(edges) + 1, {"sms_tool/fake.py": len(edges) + 1}),
    )


def test_a_new_edge_fails_the_check(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"total": 2, "per_file": {}, "reasons": {}, "edges": {"sms_tool/a.py -> sms_tool.b": "deferred"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(delayed_import_ratchet, "BASELINE", baseline)
    edge = delayed_import_ratchet.DelayedEdge("sms_tool/c.py", "sms_tool.d", "startup")
    _fake_tree(monkeypatch, [edge], total=2)
    assert delayed_import_ratchet.main([]) == 1


def test_a_new_deferred_edge_cannot_be_adopted_without_a_reason(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "total": 1,
                "per_file": {},
                "reasons": {},
                # A non-empty edge map keeps this out of the one-time adoption path.
                "edges": {"sms_tool/existing.py -> sms_tool.other": "startup"},
            }
        ),
        encoding="utf-8",
    )
    reasons = _write_reasons(tmp_path, {})
    monkeypatch.setattr(delayed_import_ratchet, "BASELINE", baseline)
    monkeypatch.setattr(delayed_import_ratchet, "REASONS_FILE", reasons)

    deferred = delayed_import_ratchet.DelayedEdge("sms_tool/a.py", "sms_tool.b", "deferred")
    _fake_tree(monkeypatch, [deferred])
    assert delayed_import_ratchet.main(["--update-baseline"]) == 1
    assert "sms_tool/a.py -> sms_tool.b" in capsys.readouterr().err

    reasons.write_text(
        json.dumps(
            {
                "edges": {
                    "sms_tool/a.py -> sms_tool.b": {
                        "reason": "deferred",
                        "why": "sibling module deferred to keep the module graph acyclic",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert delayed_import_ratchet.main(["--update-baseline"]) == 0


def test_a_rule_derived_new_edge_can_be_adopted(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 1, "per_file": {}, "reasons": {}, "edges": {}}), encoding="utf-8")
    monkeypatch.setattr(delayed_import_ratchet, "BASELINE", baseline)
    edge = delayed_import_ratchet.DelayedEdge("sms_tool/cli.py", "sms_tool.mailbox.router", "startup")
    _fake_tree(monkeypatch, [edge])
    assert delayed_import_ratchet.main(["--update-baseline"]) == 0
    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert written["edges"] == {"sms_tool/cli.py -> sms_tool.mailbox.router": "startup"}


def test_a_reason_change_fails_the_check(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "total": 1,
                "per_file": {},
                "reasons": {},
                "edges": {"sms_tool/a.py -> sms_tool.b": "deferred"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(delayed_import_ratchet, "BASELINE", baseline)
    edge = delayed_import_ratchet.DelayedEdge("sms_tool/a.py", "sms_tool.b", "cycle")
    _fake_tree(monkeypatch, [edge])
    assert delayed_import_ratchet.main([]) == 1


def test_the_committed_baseline_labels_every_edge():
    baseline = delayed_import_ratchet.load_baseline()
    edges = baseline.get("edges") or {}
    assert edges, "the baseline must freeze per-edge reasons"
    assert set(edges.values()) <= set(delayed_import_ratchet.REASONS)
    assert baseline["reasons"] == delayed_import_ratchet.reason_counts(delayed_import_ratchet.collect_delayed_edges())

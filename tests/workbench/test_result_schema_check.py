"""Tests for scripts/result_schema_check.py -- the result-payload contract.

The gate exists because C# reads payload keys defensively: a renamed key does
not throw, it becomes ``0``/``""``. So the checks are tested in both directions:
each extraction helper is exercised on a synthetic source (an extractor that
silently returns an empty set would make every comparison vacuously pass), then
the real tree is checked, then a sandbox tree is broken on purpose in each of the
three ways the contract can drift.

The last direction -- "the declaration matches what the builder really returns at
runtime" -- is the only one the static checker cannot see, which is why it lives
here and calls the real builder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import result_schema_check as schema  # noqa: E402

MANIFEST_PATH = ROOT / "result_schema.json"
FAMILY = "registration.batch"

FAKE_CS = """public static string Summary(JsonElement root)
{
    if (!root.TryGetProperty("total", out JsonElement total)) return "";
    return total.ToString();
}
"""


# --------------------------------------------------------------------------
# extraction helpers -- each of these can silently go empty and vacuously pass
# --------------------------------------------------------------------------


def test_builder_keys_reads_a_dict_literal():
    source = 'def build(*, total):\n    return {"total": total, "ok": True}\n'
    assert schema.builder_keys(source, "build") == {"total", "ok"}


def test_builder_keys_follows_a_wrapper_once():
    """A thin delegate must report the real payload, not an empty set."""
    source = "def build(**kwargs):\n    return _payload(**kwargs)\n\n\ndef _payload():\n    return {'a': 1}\n"
    assert schema.builder_keys(source, "build") == {"a"}


def test_builder_keys_refuses_a_function_without_a_dict():
    with pytest.raises(LookupError):
        schema.builder_keys("def build():\n    return 1\n", "build")


def test_csharp_body_stops_at_the_functions_own_brace():
    source = (
        "public static string F(JsonElement root)\n"
        "{\n"
        '    if (root.ValueKind != JsonValueKind.Object) { return ""; }\n'
        '    string label = Loc.F("完成 {0}/{1}", 1, 2);\n'
        "    return label;\n"
        "}\n"
        "\n"
        'public static string G(JsonElement root) { return ""; }\n'
    )
    extracted = schema.csharp_function_body(source, "F")
    assert extracted is not None
    body, first_line = extracted
    assert first_line == 2
    assert body.startswith("{")


def test_csharp_body_does_not_read_past_its_own_closing_brace():
    source = (
        "public static string F(JsonElement root)\n"
        "{\n"
        "    return label(root);\n"
        "}\n"
        "\n"
        'public static string G(JsonElement root) { return ""; }\n'
    )
    extracted = schema.csharp_function_body(source, "F")
    assert extracted is not None
    assert "G(" not in extracted[0]


def test_csharp_reads_covers_every_read_shape():
    body = (
        '        if (!root.TryGetProperty("total", out _)) return;\n'
        '        string kind = Text(root, "kind");\n'
        '        int count = Number(root, "count");\n'
        '        string name = root.GetProperty("name").GetString();\n'
    )
    assert set(schema.csharp_reads(body, 10)) == {"total", "kind", "count", "name"}


def test_csharp_reads_reports_the_absolute_line():
    body = 'first;\nsecond;\nif (!root.TryGetProperty("total", out _)) return;\n'
    assert schema.csharp_reads(body, 100)["total"] == 102


# --------------------------------------------------------------------------
# the real tree
# --------------------------------------------------------------------------


def test_the_real_tree_satisfies_the_contract():
    assert schema.check() == []


def test_the_manifest_names_the_builder_the_contract_test_pins():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["families"][FAMILY]["python"] == {
        "module": "sms_tool.commands.registration",
        "builder": "build_registration_batch_summary",
    }


def test_the_declaration_matches_the_runtime_payload():
    """The one thing the static checker cannot see: the real return value."""
    from sms_tool.commands.registration import build_registration_batch_summary

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    declared = set(manifest["families"][FAMILY]["keys"])
    payload = build_registration_batch_summary(
        effective_count=1,
        success_count=1,
        batch_id="b",
        saved_count=1,
        db_saved_count=1,
        quality=None,
        promotion_report=None,
        health_job_ids=[],
        results=[{"success": True}],
    )
    assert set(payload) == declared


def test_every_declared_key_states_a_known_type():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for family_id, family in manifest["families"].items():
        for key, spec in family["keys"].items():
            assert spec["type"] in schema.KNOWN_TYPES, f"{family_id}.{key}"


def test_every_declared_reader_exists_and_still_reads_what_it_claims():
    """Belt and braces over the real tree, with the failure spelled out."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for family in manifest["families"].values():
        for reader in family["cs_readers"]:
            path = ROOT / reader["file"]
            assert path.is_file(), f"{reader['file']} is declared but missing"
            text = path.read_text(encoding="utf-8-sig")
            if "type" in reader:
                extracted = schema.csharp_type_body(text, reader["type"])
                assert extracted is not None, f"{reader['type']} is declared but missing from {reader['file']}"
                known = schema.csharp_json_properties(*extracted)
            else:
                extracted = schema.csharp_function_body(text, reader["function"])
                assert extracted is not None, f"{reader['function']} is declared but missing from {reader['file']}"
                known = schema.csharp_reads(*extracted)
            for key in reader["reads"]:
                assert key in known, f"{reader['file']} no longer handles {key!r}"


def test_the_registration_type_models_every_declared_key():
    """The typed record is the wire contract: its properties are the declared keys."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    family = manifest["families"][FAMILY]
    declared = set(family["keys"])
    reader = next(item for item in family["cs_readers"] if "type" in item)
    text = (ROOT / reader["file"]).read_text(encoding="utf-8-sig")
    extracted = schema.csharp_type_body(text, reader["type"])
    assert extracted is not None
    assert set(schema.csharp_json_properties(*extracted)) == declared


# --------------------------------------------------------------------------
# a sandbox tree, broken on purpose in each direction
# --------------------------------------------------------------------------


def _sandbox(tmp_path: Path) -> Path:
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake" / "builder.py").write_text(
        'def build(*, total):\n    return {"total": total, "ok": True}\n',
        encoding="utf-8",
    )
    (tmp_path / "Fake.cs").write_text(FAKE_CS, encoding="utf-8")
    manifest = {
        "version": 1,
        "families": {
            "fake.family": {
                "python": {"module": "fake.builder", "builder": "build"},
                "cs_readers": [{"file": "Fake.cs", "function": "Summary", "reads": ["total"]}],
                "keys": {
                    "total": {"type": "int", "required": True},
                    "ok": {"type": "bool", "required": True},
                },
            }
        },
    }
    (tmp_path / "result_schema.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _rewrite_manifest(root: Path, mutate) -> None:
    path = root / "result_schema.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest["families"]["fake.family"])
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_a_python_key_rename_is_reported_without_the_manifest_update(tmp_path):
    root = _sandbox(tmp_path)
    builder = root / "fake" / "builder.py"
    builder.write_text('def build(*, total):\n    return {"total_count": total, "ok": True}\n', encoding="utf-8")

    failures = schema.check(root)

    assert any("produced but not declared: ['total_count']" in item for item in failures), failures
    assert any("declared but not produced: ['total']" in item for item in failures), failures


def test_a_manifest_rename_that_csharp_was_not_told_about_is_reported(tmp_path):
    root = _sandbox(tmp_path)

    def rename(family):
        family["keys"]["total_count"] = family["keys"].pop("total")

    _rewrite_manifest(root, rename)

    failures = schema.check(root)

    assert any("declares reading 'total', which is not a declared key" in item for item in failures), failures
    assert any("produced but not declared: ['total']" in item for item in failures), failures


def test_csharp_that_stops_reading_a_key_cannot_keep_the_claim(tmp_path):
    root = _sandbox(tmp_path)
    (root / "Fake.cs").write_text(
        "public static string Summary(JsonElement root)\n{\n    return root.ToString();\n}\n",
        encoding="utf-8",
    )

    failures = schema.check(root)

    assert any("no longer reads 'total'" in item for item in failures), failures


def test_a_missing_builder_or_reader_function_is_reported(tmp_path):
    root = _sandbox(tmp_path)
    (root / "Fake.cs").write_text(
        "public static string Renamed(JsonElement root)\n{\n    return root.ToString();\n}\n",
        encoding="utf-8",
    )

    failures = schema.check(root)

    assert any("function Summary not found in Fake.cs" in item for item in failures), failures


def test_a_missing_manifest_is_reported_instead_of_passing(tmp_path):
    assert schema.check(tmp_path) == ["result_schema.json: missing"]


def test_a_malformed_family_is_reported_instead_of_crashing(tmp_path):
    root = _sandbox(tmp_path)

    def break_shape(family):
        family["keys"]["total"]["type"] = "integer"

    _rewrite_manifest(root, break_shape)

    failures = schema.check(root)

    assert any("unknown type 'integer'" in item for item in failures), failures


def test_main_reports_the_count_and_returns_one(monkeypatch, capsys, tmp_path):
    root = _sandbox(tmp_path)
    (root / "Fake.cs").write_text(
        "public static string Summary(JsonElement root)\n{\n    return root.ToString();\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(schema, "ROOT", root)

    assert schema.main([]) == 1
    output = capsys.readouterr().out
    assert "result schema: FAILED (1 problem(s))" in output


def test_main_is_clean_on_the_real_tree(capsys):
    assert schema.main([]) == 0
    assert capsys.readouterr().out.strip() == "result schema: OK"


def test_a_manifest_path_outside_the_repository_is_refused(tmp_path):
    """The manifest is input; a traversal typo must not make the gate read elsewhere."""
    root = _sandbox(tmp_path)

    def escape(family):
        family["cs_readers"][0]["file"] = "../escaped.cs"

    _rewrite_manifest(root, escape)

    failures = schema.check(root)

    assert any("resolves outside the repository" in item for item in failures), failures


def test_a_nested_helper_return_is_not_part_of_the_payload():
    """Only the builder's own returns count; a helper's dict would be a lie."""
    source = "def build():\n    def helper():\n        return {'not_the_payload': 1}\n    return {'real': helper()}\n"
    assert schema.builder_keys(source, "build") == {"real"}


def test_an_earlier_call_site_is_not_mistaken_for_the_declaration():
    """Extraction must start at the declaration, not at a call to it."""
    source = (
        "public static string Caller()\n"
        "{\n"
        '    return Summary(JsonDocument.Parse("{}").RootElement);\n'
        "}\n"
        "\n"
        "public static string Summary(JsonElement root)\n"
        "{\n"
        '    if (!root.TryGetProperty("total", out _)) return "";\n'
        "    return root.ToString();\n"
        "}\n"
    )
    extracted = schema.csharp_function_body(source, "Summary")
    assert extracted is not None
    body, _line = extracted
    assert "TryGetProperty" in body
    assert "Caller" not in body


# --------------------------------------------------------------------------
# typed-record readers own their family: properties are pinned too
# --------------------------------------------------------------------------

FAKE_TYPE_CS = """public sealed record FakeSummary
{
    [JsonPropertyName("total")]
    public int? Total { get; init; }

    [JsonPropertyName("success")]
    public int? Success { get; init; }
}
"""


def _type_sandbox(tmp_path: Path) -> Path:
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake" / "builder.py").write_text(
        'def build(*, total, success):\n    return {"total": total, "success": success}\n',
        encoding="utf-8",
    )
    (tmp_path / "FakeSummary.cs").write_text(FAKE_TYPE_CS, encoding="utf-8")
    manifest = {
        "version": 1,
        "families": {
            "fake.type.family": {
                "python": {"module": "fake.builder", "builder": "build"},
                "cs_readers": [{"file": "FakeSummary.cs", "type": "FakeSummary", "reads": ["total"]}],
                "keys": {
                    "total": {"type": "int", "required": True},
                    "success": {"type": "int", "required": True},
                },
            }
        },
    }
    (tmp_path / "result_schema.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _mutate_type_family(root: Path, mutate) -> None:
    path = root / "result_schema.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest["families"]["fake.type.family"])
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_the_type_sandbox_passes_before_anything_is_broken(tmp_path):
    assert schema.check(_type_sandbox(tmp_path)) == []


def test_a_property_the_family_does_not_declare_fails(tmp_path):
    """The rule a function scan cannot express: modelling a key IS a contract change."""
    root = _type_sandbox(tmp_path)
    (root / "FakeSummary.cs").write_text(
        FAKE_TYPE_CS.replace(
            '[JsonPropertyName("success")]',
            '[JsonPropertyName("ghost")]\n    public int? Ghost { get; init; }\n\n    [JsonPropertyName("success")]',
        ),
        encoding="utf-8",
    )

    failures = schema.check(root)

    assert any("models 'ghost' on FakeSummary" in item for item in failures), failures


def test_a_claimed_read_without_a_property_fails(tmp_path):
    root = _type_sandbox(tmp_path)
    (root / "fake" / "builder.py").write_text(
        'def build(*, total, success, extra):\n    return {"total": total, "success": success, "extra": extra}\n',
        encoding="utf-8",
    )
    _mutate_type_family(
        root,
        lambda family: (
            family["keys"].update({"extra": {"type": "int", "required": True}}),
            family["cs_readers"][0]["reads"].append("extra"),
        ),
    )

    failures = schema.check(root)

    assert any('has no [JsonPropertyName("extra")] property' in item for item in failures), failures


def test_a_claimed_read_that_is_not_declared_fails(tmp_path):
    root = _type_sandbox(tmp_path)
    _mutate_type_family(root, lambda family: family["cs_readers"][0]["reads"].append("ghost"))

    failures = schema.check(root)

    assert any("declares reading 'ghost', which is not a declared key" in item for item in failures), failures


def test_a_renamed_type_is_reported(tmp_path):
    root = _type_sandbox(tmp_path)
    _mutate_type_family(root, lambda family: family["cs_readers"][0].update({"type": "OldName"}))

    failures = schema.check(root)

    assert any("type OldName not found in FakeSummary.cs" in item for item in failures), failures


def test_a_reader_needs_exactly_one_owner_shape(tmp_path):
    root = _type_sandbox(tmp_path)
    _mutate_type_family(root, lambda family: family["cs_readers"][0].update({"function": "Build"}))

    failures = schema.check(root)

    assert any("needs exactly one of function or type" in item for item in failures), failures

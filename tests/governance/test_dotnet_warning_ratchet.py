"""``scripts/dotnet_warning_ratchet.py`` -- the C# build-warning ratchet."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest

from scripts import dotnet_warning_ratchet as dwr

ROOT = dwr.ROOT
TESTS_PROJECT = ROOT / "tests" / "SmsWorkbench.Tests" / "SmsWorkbench.Tests.csproj"
APP_PROJECT = ROOT / "SmsWorkbench" / "SmsWorkbench.csproj"


def _diagnostic_line(
    source: Path,
    line: int,
    column: int,
    rule: str = "CS1030",
    project: Path = TESTS_PROJECT,
    severity: str = "warning",
) -> str:
    return f"{source}({line},{column}): {severity} {rule}: #warning: 'probe' [{project}]"


def test_a_warning_line_yields_project_path_line_and_column():
    source = ROOT / "tests" / "SmsWorkbench.Tests" / "StageMatrixTests.cs"
    output = "\n".join(
        [
            _diagnostic_line(source, 172, 10),
            "    1 Warning(s)",
            "    0 Error(s)",
        ]
    )
    diagnostics, reported = dwr.parse_build_output(output)
    assert reported == 1
    assert diagnostics == [("tests/SmsWorkbench.Tests", "tests/SmsWorkbench.Tests/StageMatrixTests.cs", 172, 10)]


def test_the_project_bracket_wins_over_the_source_path_prefix():
    source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
    diagnostics, _ = dwr.parse_build_output(_diagnostic_line(source, 116, 10, project=APP_PROJECT) + "\n0 Warning(s)")
    assert diagnostics[0][0] == "SmsWorkbench"


def test_errors_and_ordinary_output_are_not_counted_as_warnings():
    source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
    output = "\n".join(
        [
            _diagnostic_line(source, 1, 1, severity="error", rule="CS0001"),
            "Build succeeded.",
            '  Copying file from "a" to "b".',
            "    0 Warning(s)",
        ]
    )
    diagnostics, reported = dwr.parse_build_output(output)
    assert diagnostics == []
    assert reported == 0


def test_a_localized_but_parseable_line_still_works():
    source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
    line = _diagnostic_line(source, 7, 3).replace("'probe'", "'警告：探针'")
    diagnostics, _ = dwr.parse_build_output(line + "\n1 Warning(s)")
    assert len(diagnostics) == 1


def test_no_summary_line_is_an_error_not_a_clean_tree():
    with pytest.raises(RuntimeError, match=r"Warning\(s\)"):
        dwr.parse_build_output("Build succeeded.\n")


def test_a_nonzero_summary_with_nothing_parsed_is_an_error():
    with pytest.raises(RuntimeError, match="none could be parsed"):
        dwr.parse_build_output("    3 Warning(s)\n")


def test_a_failed_build_raises_instead_of_reporting_warnings():
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"error CS1002: ; expected", stderr=b"")
    with mock.patch.object(dwr.subprocess, "run", return_value=completed):
        with pytest.raises(RuntimeError, match="dotnet build failed"):
            dwr.run_build()


def test_a_missing_dotnet_raises_instead_of_reporting_clean():
    with mock.patch.object(dwr.subprocess, "run", side_effect=FileNotFoundError("dotnet")):
        with pytest.raises(RuntimeError, match="not installed"):
            dwr.run_build()


def test_the_build_is_always_forced_to_rebuild():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"    0 Warning(s)\n", stderr=b"")
    with mock.patch.object(dwr.subprocess, "run", return_value=completed) as run:
        dwr.run_build()
    assert "-t:Rebuild" in run.call_args.args[0]
    assert run.call_args.kwargs["env"]["DOTNET_CLI_UI_LANGUAGE"] == dwr.BUILD_LANGUAGE


def test_the_wpf_temp_project_does_not_double_a_warning():
    source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
    temp = ROOT / "SmsWorkbench" / "SmsWorkbench_abc123_wpftmp.csproj"
    output = "\n".join(
        [
            _diagnostic_line(source, 116, 10, project=temp),
            _diagnostic_line(source, 116, 10, project=APP_PROJECT),
            "    2 Warning(s)",
        ]
    )
    per_file, per_project = dwr.count_warnings(dwr.parse_build_output(output)[0])
    assert dict(per_file) == {"SmsWorkbench/StageMatrixStore.cs": 1}
    assert dict(per_project) == {"SmsWorkbench": 1}


def test_two_warnings_on_different_lines_both_count():
    source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
    output = "\n".join(
        [
            _diagnostic_line(source, 10, 1, project=APP_PROJECT),
            _diagnostic_line(source, 20, 1, project=APP_PROJECT),
            "    2 Warning(s)",
        ]
    )
    per_file, _ = dwr.count_warnings(dwr.parse_build_output(output)[0])
    assert dict(per_file) == {"SmsWorkbench/StageMatrixStore.cs": 2}


def test_a_file_absent_from_the_baseline_has_zero_allowance():
    assert dwr.grown(Counter({"sms_tool/new.py": 1}), {}) == [("sms_tool/new.py", 1, 0)]


def test_a_file_at_or_below_its_allowance_is_clean():
    warnings = Counter({"a.cs": 3, "b.cs": 1})
    assert dwr.grown(warnings, {"a.cs": 3, "b.cs": 5}) == []


def test_a_shrink_in_one_file_cannot_mask_growth_in_another():
    warnings = Counter({"grown.cs": 2})
    assert dwr.grown(warnings, {"shrunk.cs": 10, "grown.cs": 1}) == [("grown.cs", 2, 1)]


def test_corrupt_baseline_loads_as_empty_so_every_file_grows(tmp_path: Path):
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")
    assert dwr.load_baseline(path) == {}


def test_missing_baseline_loads_as_empty(tmp_path: Path):
    assert dwr.load_baseline(tmp_path / "nope.json") == {}


def test_non_int_entries_are_skipped_not_crashed():
    assert dwr.baseline_per_file({"per_file": {"a.cs": "x", "b.cs": 2}}) == {"b.cs": 2}


def test_the_committed_baseline_is_zero():
    baseline = json.loads(dwr.BASELINE.read_text(encoding="utf-8"))
    assert baseline.get("total") == 0
    assert baseline.get("per_file") == {}


def test_utf8_output_is_decoded_as_utf8():
    assert dwr._decode_output("#拒绝".encode()) == "#拒绝"


def test_gbk_output_is_decoded_when_the_locale_says_gbk():
    with mock.patch.object(dwr.locale, "getpreferredencoding", return_value="gbk"):
        assert dwr._decode_output("已通过".encode("gbk")) == "已通过"


def test_undecodable_bytes_never_raise_whatever_the_locale():
    raw = b"\xff\xfe# \xd2\xd1\xcd\xa8\xb9\xfd"
    with mock.patch.object(dwr.locale, "getpreferredencoding", return_value="cp1252"):
        decoded = dwr._decode_output(raw)
    assert isinstance(decoded, str)
    assert "#" in decoded


def test_the_current_tree_is_at_baseline():
    if shutil.which("dotnet") is None:
        pytest.skip("dotnet is not installed")
    assert dwr.main([]) == 0

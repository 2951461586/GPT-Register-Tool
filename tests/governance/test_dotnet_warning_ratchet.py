"""``scripts/dotnet_warning_ratchet.py`` -- the C# build-warning ratchet.

The ratchet's dangerous failure mode is passing while the build never compiled,
or while its output stopped matching the parser -- a gate that silently reads
"0 warnings" out of an output it no longer understands is worse than no gate.
These tests pin the plumbing and the safety rails; the tree check lives in
``test_the_current_tree_is_at_baseline`` and is the one that runs a real build.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

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


class Parsing(unittest.TestCase):
    def test_a_warning_line_yields_project_path_line_and_column(self):
        source = ROOT / "tests" / "SmsWorkbench.Tests" / "StageMatrixTests.cs"
        output = "\n".join(
            [
                _diagnostic_line(source, 172, 10),
                "    1 Warning(s)",
                "    0 Error(s)",
            ]
        )

        diagnostics, reported = dwr.parse_build_output(output)

        self.assertEqual(reported, 1)
        self.assertEqual(
            diagnostics,
            [("tests/SmsWorkbench.Tests", "tests/SmsWorkbench.Tests/StageMatrixTests.cs", 172, 10)],
        )

    def test_the_project_bracket_wins_over_the_source_path_prefix(self):
        """A warning in a production file must be attributed to production."""
        source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"

        diagnostics, _ = dwr.parse_build_output(
            _diagnostic_line(source, 116, 10, project=APP_PROJECT) + "\n0 Warning(s)"
        )

        self.assertEqual(diagnostics[0][0], "SmsWorkbench")

    def test_errors_and_ordinary_output_are_not_counted_as_warnings(self):
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

        self.assertEqual(diagnostics, [])
        self.assertEqual(reported, 0)

    def test_a_localized_but_parseable_line_still_works(self):
        """Only the trailing message may be localized; the prefix is stable."""
        source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
        line = _diagnostic_line(source, 7, 3).replace("'probe'", "'警告：探针'")

        diagnostics, _ = dwr.parse_build_output(line + "\n1 Warning(s)")

        self.assertEqual(len(diagnostics), 1)


class SafetyRails(unittest.TestCase):
    def test_no_summary_line_is_an_error_not_a_clean_tree(self):
        with self.assertRaises(RuntimeError) as caught:
            dwr.parse_build_output("Build succeeded.\n")
        self.assertIn("Warning(s)", str(caught.exception))

    def test_a_nonzero_summary_with_nothing_parsed_is_an_error(self):
        """The exact regression this gate exists to prevent: a silent 0."""
        with self.assertRaises(RuntimeError) as caught:
            dwr.parse_build_output("    3 Warning(s)\n")
        self.assertIn("none could be parsed", str(caught.exception))

    def test_a_failed_build_raises_instead_of_reporting_warnings(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"error CS1002: ; expected", stderr=b"")
        with mock.patch.object(dwr.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as caught:
                dwr.run_build()
        self.assertIn("dotnet build failed", str(caught.exception))

    def test_a_missing_dotnet_raises_instead_of_reporting_clean(self):
        with mock.patch.object(dwr.subprocess, "run", side_effect=FileNotFoundError("dotnet")):
            with self.assertRaises(RuntimeError) as caught:
                dwr.run_build()
        self.assertIn("not installed", str(caught.exception))

    def test_the_build_is_always_forced_to_rebuild(self):
        """An up-to-date build reports 0 warnings without compiling anything."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"    0 Warning(s)\n", stderr=b"")
        with mock.patch.object(dwr.subprocess, "run", return_value=completed) as run:
            dwr.run_build()
        self.assertIn("-t:Rebuild", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["DOTNET_CLI_UI_LANGUAGE"], dwr.BUILD_LANGUAGE)


class Deduplication(unittest.TestCase):
    def test_the_wpf_temp_project_does_not_double_a_warning(self):
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

        self.assertEqual(dict(per_file), {"SmsWorkbench/StageMatrixStore.cs": 1})
        self.assertEqual(dict(per_project), {"SmsWorkbench": 1})

    def test_two_warnings_on_different_lines_both_count(self):
        source = ROOT / "SmsWorkbench" / "StageMatrixStore.cs"
        output = "\n".join(
            [
                _diagnostic_line(source, 10, 1, project=APP_PROJECT),
                _diagnostic_line(source, 20, 1, project=APP_PROJECT),
                "    2 Warning(s)",
            ]
        )

        per_file, _ = dwr.count_warnings(dwr.parse_build_output(output)[0])

        self.assertEqual(dict(per_file), {"SmsWorkbench/StageMatrixStore.cs": 2})


class Growth(unittest.TestCase):
    def test_a_file_absent_from_the_baseline_has_zero_allowance(self):
        """A brand-new file must be clean, not inherit someone else's budget."""
        self.assertEqual(dwr.grown(Counter({"sms_tool/new.py": 1}), {}), [("sms_tool/new.py", 1, 0)])

    def test_a_file_at_or_below_its_allowance_is_clean(self):
        warnings = Counter({"a.cs": 3, "b.cs": 1})
        self.assertEqual(dwr.grown(warnings, {"a.cs": 3, "b.cs": 5}), [])

    def test_a_shrink_in_one_file_cannot_mask_growth_in_another(self):
        """The whole reason the baseline is per-file rather than one total."""
        warnings = Counter({"grown.cs": 2})
        self.assertEqual(dwr.grown(warnings, {"shrunk.cs": 10, "grown.cs": 1}), [("grown.cs", 2, 1)])


class BaselineLoading(unittest.TestCase):
    def test_corrupt_baseline_loads_as_empty_so_every_file_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(dwr.load_baseline(path), {})

    def test_missing_baseline_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dwr.load_baseline(Path(tmp) / "nope.json"), {})

    def test_non_int_entries_are_skipped_not_crashed(self):
        self.assertEqual(dwr.baseline_per_file({"per_file": {"a.cs": "x", "b.cs": 2}}), {"b.cs": 2})

    def test_the_committed_baseline_is_zero(self):
        """The 12 warnings that existed on 2026-10-20 were fixed, not frozen."""
        baseline = json.loads(dwr.BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(baseline.get("total"), 0)
        self.assertEqual(baseline.get("per_file"), {})


class OutputDecoding(unittest.TestCase):
    def test_utf8_output_is_decoded_as_utf8(self):
        self.assertEqual(dwr._decode_output("#拒绝".encode()), "#拒绝")

    def test_gbk_output_is_decoded_with_the_locale_fallback(self):
        """MSBuild writes the OEM code page on some consoles; never crash on it."""
        self.assertEqual(dwr._decode_output("已通过".encode("gbk")), "已通过")


class TheCurrentTree(unittest.TestCase):
    def test_the_current_tree_is_at_baseline(self):
        """Runs a real rebuild: the verdict this commit is claiming."""
        import shutil

        if shutil.which("dotnet") is None:
            self.skipTest("dotnet is not installed")
        self.assertEqual(dwr.main([]), 0)

"""Ratchet: the C# build warning count must not grow.

Why this exists
---------------
The Python side has a type gate (``scripts/pyright_ratchet.py``, 850 errors
frozen). The .NET side had none: ``Directory.Build.props`` sets
``WarningsAsErrors`` to **NuGet codes only** (``NU1605``, ``NU1901``-``NU1904``),
so every CS/CA/xUnit-analyzer warning was invisible. Measured on 2026-10-20,
the solution carried **12** of them -- all in ``tests/SmsWorkbench.Tests``
(``CS8767`` x2, ``CS8625`` x2, ``CS8600`` x2, ``CS8604``, ``CA1865``,
``xUnit2031`` x4) -- and nothing failed. They are fixed now, so the baseline is
**0**: a file absent from the baseline has an allowance of 0, and any new
warning fails CI.

Why the console text and NOT the compiler's SARIF
-------------------------------------------------
``-p:ErrorLog=<file>.sarif`` looks like the obvious machine-readable source, and
it is wrong for this gate in two independent ways, both measured:

1. **The error log is not "what the build reports".** It records every
   diagnostic an analyzer produced, *before* the severity policy is applied. On
   a clean tree the console printed ``0 Warning(s)`` while the SARIF held **359**
   results: ``CS0618`` x272 in generated ``*.g.cs`` (suppressed by the MVVM
   toolkit's own pragmas), ``MVVMTK0042``/``SYSLIB1045`` x84 at ``note`` level,
   and one ``CA1001`` that ``.editorconfig`` does not surface. Re-implementing
   Roslyn's severity resolution (editorconfig + pragmas + analyzer config) in a
   script is not a gate, it is a second compiler.
2. **A single ``-p:ErrorLog`` is one file every project truncates**, so only the
   last-compiled project survives. Measured with a ``#warning`` planted in
   ``SmsWorkbench/StageMatrixStore.cs``: the build reported 2 warnings while the
   shared SARIF had ``results: []`` -- the gate could not see production code at
   all.

The console line IS parseable without depending on the machine's language:
``<path>(<line>,<col>): warning <rule>: <message> [<project.csproj>]`` keeps the
``warning`` keyword and the rule id in English even when the rest of the output
is localized (verified on a ``zh-CN`` checkout). To remove the remaining risk the
child build is pinned to ``DOTNET_CLI_UI_LANGUAGE=en-US``, and the parsed count
is cross-checked against MSBuild's own ``N Warning(s)`` summary -- if the two
disagree in the "parser found nothing but the build says it warned" direction,
the gate **fails** instead of passing on an output format it no longer
understands.

Why ``-t:Rebuild`` is mandatory
-------------------------------
An up-to-date incremental build does not run ``csc``, so it reports
``0 Warning(s)`` without having compiled anything. A ratchet that accepted that
would go green forever. The compile is therefore always forced.

Why the duplicates are collapsed
--------------------------------
WPF builds a ``SmsWorkbench_<hash>_wpftmp.csproj`` alongside the real project, so
one source diagnostic is reported once per project (and the console repeats some
lines). Identical ``(rule, file, line, column)`` warnings are counted once --
otherwise a fixed multiplier would read as growth. Attribution still uses the
``[<project>.csproj]`` suffix, so a warning is filed under the project that owns
the source.

Why a ratchet and not ``WarningsAsErrors`` in the csproj
--------------------------------------------------------
``AnalysisLevel`` is ``latest-recommended``, so an SDK upgrade can add rules. As
a hard compile error that breaks the build with no review trail; as a ratchet it
shows up as a baseline diff a reviewer approves once, via
``--update-baseline``. Never widen ``WarningsAsErrors`` to ``CS*``/``CA*`` to
"fix" a failing ratchet.

What it measures
----------------
Diagnostics the build reports at ``warning`` severity. The baseline stores three
views:

* ``per_file`` -- the gated number. A file absent from the baseline has an
  allowance of 0, so a brand-new file must be clean.
* ``per_project`` -- the same counts summed per .NET project directory, so a
  reviewer sees which project carries debt. Not a second gate: growth is decided
  per file.
* ``total`` -- the sum, pinned separately so a baseline that somehow loses rows
  fails loudly instead of silently passing.

Usage
-----
::

    python scripts/dotnet_warning_ratchet.py              # rebuild + check
    python scripts/dotnet_warning_ratchet.py --detail     # per-file/per-project table
    python scripts/dotnet_warning_ratchet.py --update-baseline

``--update-baseline`` is for **lowering** the count after a real cleanup, or for
a deliberate, reviewed decision when an SDK bump introduces a new analyzer rule.
It prints the per-file delta so a reviewer can see exactly what moved.

Standard library only (``dotnet`` is invoked as a subprocess).
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "dotnet_warning_baseline.json"

#: The solution file handed to ``dotnet build``. Pinned rather than discovered so
#: a stray ``*.csproj`` outside the solution cannot enter or leave the gate.
SOLUTION = "GPTRegisterTool.slnx"

#: ``Configuration`` used for the gate. Release matches ``ci.yml``'s
#: ``dotnet test -c Release``; Debug adds its own analyzers/conditionals.
CONFIGURATION = "Release"

#: Pinned so the diagnostic lines and the summary keep one shape on every
#: machine. Without it a localized SDK changes the wording the parser matches.
BUILD_LANGUAGE = "en-US"

#: Known .NET project directories, longest prefix first, used only when a
#: diagnostic line has no ``[<project>.csproj]`` suffix.
PROJECT_DIRS: tuple[str, ...] = (
    "tests/SmsWorkbench.Tests",
    "SmsWorkbench.Contracts",
    "SmsWorkbench",
)

#: A hung MSBuild must fail the gate rather than stall CI forever.
BUILD_TIMEOUT_SECONDS = 900

#: ``<abs path>(<line>,<col>): warning <rule>: <message> [<project.csproj>]``.
#: The severity keyword and the rule id stay English even on a localized SDK;
#: ``DOTNET_CLI_UI_LANGUAGE`` removes the residual doubt.
_DIAGNOSTIC = re.compile(
    r"^(?P<file>.*?)\((?P<line>\d+),(?P<column>\d+)\):\s+"
    r"(?P<severity>warning|error)\s+(?P<rule>[A-Za-z]+\d+):\s*(?P<message>.*?)\s*$"
)

#: MSBuild's own count for the whole build, e.g. ``    3 Warning(s)``.
_SUMMARY = re.compile(r"^\s*(?P<count>\d+)\s+Warning\(s\)\s*$")

#: The project a diagnostic was reported for, in the line's trailing brackets.
_PROJECT_SUFFIX = re.compile(r"\[([^\[\]]+\.csproj)\]\s*$")


def _relpath(raw: str) -> str:
    """Normalise a printed file path to a POSIX repo-relative path.

    Returns ``""`` for an empty path instead of ``"."``: ``Path("").resolve()`` is
    the repository root, so an empty input would otherwise be filed as a ``.``
    row in the baseline (a path no source file has).
    """
    if not raw or not raw.strip():
        return ""
    path = Path(raw)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def project_of(relpath: str, project_file: str = "") -> str:
    """Attribute a source path to its .NET project directory."""
    if project_file:
        directory = Path(_relpath(project_file)).parent.as_posix()
        return directory if directory not in ("", ".") else "<unknown>"
    for candidate in PROJECT_DIRS:
        if relpath == candidate or relpath.startswith(candidate + "/"):
            return candidate
    return "<unknown>"


def _decode_output(raw: bytes) -> str:
    """Decode captured MSBuild output without assuming one encoding.

    ``dotnet`` writes UTF-8 when it detects a UTF-8 console and the OEM code page
    otherwise; ``text=True`` picks the *locale* codec, which raises mid-stream on
    the mismatched one and leaves ``proc.stdout`` empty. Try UTF-8 first, then the
    locale's preferred encoding, and only then degrade lossily.
    """
    for encoding in ("utf-8", locale.getpreferredencoding(False), "cp1252"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_build(targets: str = SOLUTION) -> tuple[list[tuple[str, str, int, int]], int]:
    """Rebuild the solution and return ``(diagnostics, reported_warning_total)``.

    Each diagnostic is ``(project_dir, repo_path, line, column)``. Raises
    ``RuntimeError`` when the build fails, hangs, or prints output this gate
    cannot interpret -- a broken checker must never be mistaken for a clean tree.
    """
    command = [
        "dotnet",
        "build",
        targets,
        "-c",
        CONFIGURATION,
        # An incremental build does not compile, so it reports 0 warnings without
        # having looked at the code (see the module docstring).
        "-t:Rebuild",
        "-nologo",
    ]
    env = dict(os.environ)
    env["DOTNET_CLI_UI_LANGUAGE"] = BUILD_LANGUAGE
    env.setdefault("DOTNET_NOLOGO", "1")
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            # Bytes: the output encoding follows the console, so it is decoded once
            # afterwards. See ``_decode_output``.
            capture_output=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"dotnet is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"dotnet build did not finish within {BUILD_TIMEOUT_SECONDS}s") from exc

    output = _decode_output(proc.stdout or b"")
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        raise RuntimeError(
            f"dotnet build failed (exit {proc.returncode}); fix the build before "
            f"asking the ratchet about warnings:\n{tail}"
        )
    return parse_build_output(output)


def parse_build_output(output: str) -> tuple[list[tuple[str, str, int, int]], int]:
    """Parse ``(diagnostics, reported_warning_total)`` out of MSBuild's output.

    Split out from ``run_build`` so the parser can be pinned without running
    ``dotnet`` -- the failure mode worth testing is a format this gate stops
    understanding, not MSBuild's verdict.
    """
    diagnostics: list[tuple[str, str, int, int]] = []
    reported_total: int | None = None
    for line in output.splitlines():
        summary = _SUMMARY.match(line)
        if summary:
            reported_total = int(summary.group("count"))
            continue
        match = _DIAGNOSTIC.match(line.strip())
        if not match or match.group("severity") != "warning":
            continue
        # A diagnostic may be printed without a file (``(12,3): warning CS1: ...``).
        # Filing that under ``.`` would put a bogus row in the baseline, so it is
        # labelled instead -- and ``_relpath`` refuses to normalise an empty path.
        relpath = _relpath(match.group("file")) or "<no location>"
        diagnostics.append(
            (
                project_of(relpath, _project_file_of(match.group("message"))),
                relpath,
                int(match.group("line")),
                int(match.group("column")),
            )
        )

    if reported_total is None:
        raise RuntimeError(
            "the build printed no 'N Warning(s)' summary; the output format changed and this "
            "gate cannot judge the tree. Refusing to report clean."
        )
    if reported_total > 0 and not diagnostics:
        raise RuntimeError(
            f"the build reported {reported_total} warning(s) but none could be parsed; the "
            "diagnostic line format changed. Refusing to report clean."
        )
    return diagnostics, reported_total


def _project_file_of(message: str) -> str:
    """Return the project file printed in a diagnostic line's trailing bracket."""
    match = _PROJECT_SUFFIX.search(message)
    return match.group(1) if match else ""


def count_warnings(diagnostics: list[tuple[str, str, int, int]]) -> tuple[Counter[str], Counter[str]]:
    """Return ``(per_file, per_project)`` counters, de-duplicated.

    Identical ``(rule, file, line, column)`` diagnostics collapse: WPF compiles the
    same source through a temporary ``*_wpftmp.csproj``, so one planted warning is
    reported twice. Without this, a fixed multiplier would read as growth.
    """
    per_file: Counter[str] = Counter()
    per_project: Counter[str] = Counter()
    seen: set[tuple[str, str, int, int]] = set()
    for project, relpath, line, column in diagnostics:
        key = (project, relpath, line, column)
        if key in seen:
            continue
        seen.add(key)
        per_file[relpath] += 1
        per_project[project] += 1
    return per_file, per_project


def load_baseline(path: Path = BASELINE) -> dict:
    """Load the baseline, treating a corrupt file as empty (fail loudly later)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def baseline_per_file(baseline: dict) -> dict[str, int]:
    raw = baseline.get("per_file")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


def grown(warnings: Counter[str], allowed: dict[str, int]) -> list[tuple[str, int, int]]:
    """Return ``(file, current, allowed)`` for every file over its allowance."""
    out: list[tuple[str, int, int]] = []
    for path in sorted(set(warnings) | set(allowed)):
        current = warnings.get(path, 0)
        limit = allowed.get(path, 0)
        if current > limit:
            out.append((path, current, limit))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--detail", action="store_true", help="print the per-file and per-project tables")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after LOWERING the count, or a reviewed SDK-rule change)",
    )
    args = parser.parse_args(argv)

    try:
        diagnostics, reported_total = run_build()
    except RuntimeError as exc:
        print(f"dotnet-warning-ratchet: {exc}", file=sys.stderr)
        return 2

    per_file, per_project = count_warnings(diagnostics)
    total = sum(per_file.values())

    if args.detail:
        print(f"=== warnings per project (build reported {reported_total}) ===")
        for project, count in sorted(per_project.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count:5}  {project}")
        print("\n=== warnings per file ===")
        for path, count in per_file.most_common():
            print(f"  {count:5}  {path}")

    if args.update_baseline:
        previous = baseline_per_file(load_baseline())
        payload = {
            "total": total,
            "per_project": dict(sorted(per_project.items())),
            "per_file": dict(sorted(per_file.items())),
        }
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {BASELINE.name} ({total} warning(s) over {len(per_file)} file(s))")
        for path in sorted(set(previous) | set(per_file)):
            before = previous.get(path, 0)
            after = per_file.get(path, 0)
            if before != after:
                print(f"  {before:5} -> {after:<5} {path}")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/dotnet_warning_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    allowed = baseline_per_file(baseline)

    regressions = grown(per_file, allowed)
    pinned_total = baseline.get("total")
    if isinstance(pinned_total, int) and total > pinned_total:
        regressions.append(("<total>", total, pinned_total))

    if regressions:
        for path, current, limit in regressions:
            print(
                f"{path}: {current} > baseline {limit} (+{current - limit}). "
                "Fix the diagnostic at its source. Do not widen WarningsAsErrors and do not "
                "suppress it with a pragma; if an SDK/analyzer upgrade introduced the rule on "
                "purpose, refresh the baseline with --update-baseline and say why in the commit.",
                file=sys.stderr,
            )
        return 1

    print(
        f"dotnet-warning ratchet OK ({total} warning(s) over {len(per_file)} file(s); "
        f"baseline {pinned_total if isinstance(pinned_total, int) else 'n/a'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

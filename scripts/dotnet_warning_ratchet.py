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

Why SARIF and not the console output
------------------------------------
The build output is **localized** (this checkout reports ``已通过!`` /
``12 个警告``, and the SARIF itself carries ``tool.language: zh-CN``). A
``warning CS8767:`` regex therefore depends on the machine's UI language, and a
machine set to another locale would silently match nothing and report a clean
tree. ``-p:ErrorLog=<abs>.sarif`` makes MSBuild emit a machine-readable log
whose ``ruleId`` and ``locations[].resultFile.uri`` are stable contracts
regardless of language.

Why ``-t:Rebuild`` is mandatory
-------------------------------
An up-to-date incremental build does not run ``csc``, so it emits no warnings
**and writes no SARIF at all** (measured: 0 warnings, file absent). A ratchet
that accepted that would report "clean" without ever having compiled. So the
build is always forced to rebuild, and "the build succeeded but no SARIF exists"
is an **error** (exit 2), never a clean result -- the same failure mode the
pyright ratchet guards against.

Two more measured traps, both handled here:

* ``ErrorLog`` resolves a **relative** path against each *project* directory
  (``SmsWorkbench.Contracts/runtime/tmp/x.sarif`` -> ``CS0016``), so the log
  path is always absolute.
* ``file:///`` URIs are percent-encoded absolute paths; they are normalised to
  repo-relative POSIX paths before counting.

Why a ratchet and not ``WarningsAsErrors`` in the csproj
--------------------------------------------------------
``AnalysisLevel`` is ``latest-recommended``, so an SDK upgrade can add rules. As
a hard compile error that breaks the build with no review trail; as a ratchet it
shows up as a baseline diff a reviewer approves once, via
``--update-baseline``. Never widen ``WarningsAsErrors`` to ``CS*``/``CA*`` to
"fix" a failing ratchet.

What it measures
----------------
``level == "warning"`` results only. ``error`` results already fail the build
(and would have raised above), and ``note`` is not a defect. The baseline stores
three views:

* ``per_file`` -- the gated number. A file absent from the baseline has an
  allowance of 0, so a brand-new file must be clean.
* ``per_project`` -- the same counts summed per .NET project, so a reviewer sees
  which project carries debt. Not a second gate: growth is decided per file.
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
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "dotnet_warning_baseline.json"

#: The solution file handed to ``dotnet build``. Pinned rather than discovered so
#: a stray ``*.csproj`` outside the solution cannot enter or leave the gate.
SOLUTION = "GPTRegisterTool.slnx"

#: ``Configuration`` used for the gate. Release matches ``ci.yml``'s
#: ``dotnet test -c Release``; Debug adds its own analyzers/conditionals.
CONFIGURATION = "Release"

#: Known .NET project directories, longest prefix first. Anything else is
#: attributed to ``<unknown>`` -- still gated, just not attributed, so a newly
#: added project cannot hide by being unrecognised.
PROJECT_DIRS: tuple[str, ...] = (
    "tests/SmsWorkbench.Tests",
    "SmsWorkbench.Contracts",
    "SmsWorkbench",
)

#: A hung MSBuild must fail the gate rather than stall CI forever.
BUILD_TIMEOUT_SECONDS = 900


def _relpath(uri: str) -> str:
    """Turn a SARIF ``resultFile.uri`` into a POSIX repo-relative path."""
    raw = uri
    if raw.startswith("file:"):
        parsed = urlparse(raw)
        # file:///F:/repo/x.cs -> /F:/repo/x.cs on Windows.
        raw = unquote(parsed.path or "")
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
    path = Path(raw)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def project_of(relpath: str) -> str:
    """Attribute a repo-relative source path to its .NET project directory."""
    for candidate in PROJECT_DIRS:
        if relpath == candidate or relpath.startswith(candidate + "/"):
            return candidate
    return "<unknown>"


def run_build(targets: str = SOLUTION) -> dict:
    """Rebuild the solution and return the parsed SARIF payload.

    Raises ``RuntimeError`` when the build fails, hangs, or produces no log --
    a broken checker must never be mistaken for a clean tree.
    """
    with tempfile.TemporaryDirectory(prefix="dotnet-warning-ratchet-") as tmp:
        log = Path(tmp) / "warnings.sarif"
        command = [
            "dotnet",
            "build",
            targets,
            "-c",
            CONFIGURATION,
            # Incremental builds emit no warnings AND write no SARIF (see the
            # module docstring), so the compile is always forced.
            "-t:Rebuild",
            "-v:q",
            "-nologo",
            # Must be absolute: a relative ErrorLog is resolved per project.
            f"-p:ErrorLog={log}",
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                # NOT ``text=True``: that decodes with the machine's locale codec
                # (GBK here), while MSBuild writes UTF-8 -- the reader thread then
                # dies on the first CJK byte and leaves ``proc.stdout`` empty. The
                # output is only used for a failure tail, so a lossy decode is fine.
                encoding="utf-8",
                errors="replace",
                timeout=BUILD_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"dotnet is not installed or not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"dotnet build did not finish within {BUILD_TIMEOUT_SECONDS}s") from exc

        if proc.returncode != 0:
            tail = "\n".join((proc.stdout or "").strip().splitlines()[-12:])
            raise RuntimeError(
                f"dotnet build failed (exit {proc.returncode}); fix the build before "
                f"asking the ratchet about warnings:\n{tail}"
            )
        if not log.exists():
            raise RuntimeError(
                "dotnet build succeeded but wrote no SARIF log; the gate cannot judge the "
                "tree. Refusing to report clean."
            )
        payload = log.read_text(encoding="utf-8", errors="replace").strip()

    if not payload:
        raise RuntimeError("the SARIF log is empty; cannot judge the tree")
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"the SARIF log is not parseable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise RuntimeError("the SARIF payload is missing 'runs'")
    return data


def count_warnings(data: dict) -> Counter[str]:
    """Count ``warning``-level diagnostics per repo-relative source path."""
    warnings: Counter[str] = Counter()
    for run in data.get("runs") or []:
        for result in (run or {}).get("results") or []:
            if not isinstance(result, dict) or result.get("level") != "warning":
                continue
            locations = result.get("locations") or []
            if not locations:
                # A warning with no location (the usual shape for a project-level
                # diagnostic) still counts -- attributing it to ``<unknown>`` is
                # more honest than dropping the defect on the floor.
                warnings["<no location>"] += 1
                continue
            file_uri = ((locations[0] or {}).get("resultFile") or {}).get("uri") or ""
            warnings[_relpath(str(file_uri)) or "<no location>"] += 1
    return warnings


def per_project(warnings: Counter[str]) -> dict[str, int]:
    """Sum the per-file counts per .NET project."""
    projects: Counter[str] = Counter()
    for path, count in warnings.items():
        projects[project_of(path)] += count
    return dict(projects)


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
        data = run_build()
    except RuntimeError as exc:
        print(f"dotnet-warning-ratchet: {exc}", file=sys.stderr)
        return 2

    warnings = count_warnings(data)
    projects = per_project(warnings)
    total = sum(warnings.values())

    if args.detail:
        print("=== warnings per project ===")
        for project, count in sorted(projects.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count:5}  {project}")
        print("\n=== warnings per file ===")
        for path, count in warnings.most_common():
            print(f"  {count:5}  {path}")

    if args.update_baseline:
        previous = baseline_per_file(load_baseline())
        payload = {
            "total": total,
            "per_project": dict(sorted(projects.items())),
            "per_file": dict(sorted(warnings.items())),
        }
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {BASELINE.name} ({total} warning(s) over {len(warnings)} file(s))")
        for path in sorted(set(previous) | set(warnings)):
            before = previous.get(path, 0)
            after = warnings.get(path, 0)
            if before != after:
                print(f"  {before:5} -> {after:<5} {path}")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/dotnet_warning_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    allowed = baseline_per_file(baseline)

    regressions = grown(warnings, allowed)
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
        f"dotnet-warning ratchet OK ({total} warning(s) over {len(warnings)} file(s), "
        f"baseline {pinned_total if isinstance(pinned_total, int) else 'n/a'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

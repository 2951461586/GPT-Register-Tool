# C# Gates (build warnings)

`scripts/dotnet_warning_ratchet.py` is the C# build-warning gate. It rebuilds the
solution, parses the warnings MSBuild reports, and compares the **per-file**
counts with `scripts/dotnet_warning_baseline.json`. The Python analogue is
[`typing.md`](typing.md) (pyright, per-file error ratchet); the two exist for the
same reason and are maintained the same way.

## What already gate the .NET build

| Gate | Scope |
| --- | --- |
| `Directory.Build.props` → `WarningsAsErrors` | **NuGet codes only** (`NU1605`, `NU1901`–`NU1904`). CS/CA/xUnit-analyzer warnings are not errors and were not gated at all. |
| `dotnet test GPTRegisterTool.slnx -c Release` (CI) | Test outcomes (568 as of 2026-10-20). A warning changes nothing here. |
| `scripts/dotnet_warning_ratchet.py` (CI) | Every reported `warning`-severity diagnostic, per file, at a **zero** baseline. |

That gap is not theoretical: on 2026-10-20 the test project carried **12**
warnings (`CS8767` x2, `CS8625` x2, `CS8600` x2, `CS8604`, `CA1865`,
`xUnit2031` x4) with every check green. They were fixed at the source — nullable
contracts matched, `Assert.Single(x, predicate)` used, `SettingsSaveResult`
constructed through its default — and the baseline was then set to zero.

## Why zero instead of a frozen number

A file absent from the baseline has an allowance of 0, so a new file must be
clean and the next warning anywhere fails CI. The Python ratchet froze 850
pyright errors because 1,453 could not be reviewed in one commit; the C# side had
12, so retiring them first cost one small commit and left no debt to inherit.

## Why it parses the console output and not the compiler's SARIF

`-p:ErrorLog=<file>.sarif` looks like the obvious machine-readable input, and it
is wrong for this gate in two measured ways:

1. **The error log is not "what the build reports".** It records every
   diagnostic an analyzer produced, *before* the severity policy applies. On a
   clean tree the build printed `0 Warning(s)` while the SARIF held **359**
   results: `CS0618` x272 in generated `*.g.cs`, `MVVMTK0042`/`SYSLIB1045` x84 at
   `note` level, and a `CA1001` that `.editorconfig` does not surface.
   Re-implementing Roslyn's severity resolution in a script would be a second
   compiler, not a gate.
2. **One `-p:ErrorLog` is one file that every project truncates**, so only the
   last-compiled project survives. With a `#warning` planted in
   `SmsWorkbench/Services/StageMatrixStore.cs` the build reported 2 warnings
   while the shared SARIF had `results: []` — the gate was blind to production
   code.

The console line itself is stable enough to parse:
`<path>(<line>,<col>): warning <rule>: <message> [<project.csproj>]` keeps the
`warning` keyword and the rule id in English even on a localized SDK (verified on
a `zh-CN` checkout). Two guards remove the residual risk:

- the child build runs with `DOTNET_CLI_UI_LANGUAGE=en-US`, and
- the parsed count is cross-checked against MSBuild's own `N Warning(s)` summary:
  a non-zero summary with nothing parsed **fails** (exit 2) instead of passing on
  an output format the gate no longer understands.

## Why the build is always forced to rebuild

An up-to-date incremental build does not compile, so it reports `0 Warning(s)`
without having looked at anything. `-t:Rebuild` is therefore unconditional; a
gate that trusts an incremental build goes green forever.

## Duplicate warnings

WPF builds `SmsWorkbench_<hash>_wpftmp.csproj` alongside the real project, so one
source diagnostic is reported once per project (the console repeats some lines
too). Identical `(project, file, line, column)` diagnostics are counted once —
otherwise a fixed multiplier would read as growth. Attribution uses the trailing
`[<project>.csproj]` bracket, so a warning lands on the project that owns the
source, not on whichever project compiled last.

## Running it

```text
python scripts/dotnet_warning_ratchet.py                # rebuild + check
python scripts/dotnet_warning_ratchet.py --detail       # per-file and per-project tables
python scripts/dotnet_warning_ratchet.py --update-baseline
```

Exit codes: `0` at baseline, `1` a file or the total grew, `2` the gate could not
judge the tree (build failed, hung, no summary line, or a non-zero summary it
could not parse). A `2` is never a pass.

`--update-baseline` is for **lowering** the count after a real cleanup, or for a
deliberate, reviewed decision when an SDK/analyzer upgrade introduces a new rule
under `AnalysisLevel=latest-recommended`. It prints the per-file delta so a
reviewer can see exactly what moved; the commit must say why. It is **not** a way
to silence a warning you introduced.

## In the test suite

`tests/governance/test_dotnet_warning_ratchet.py` pins the plumbing and the safety
rails — the parser, duplicate collapsing, per-file growth, a corrupt or missing
baseline, a failed build, a missing `dotnet`, and the forced rebuild — and ends
with one test that runs the real rebuild against the tree.

## Fixing a warning

Fix the contract the warning points at. The 2026-10-20 batch is the template:

```csharp
// CS8767: the interface declares IProgress<BackendOutputLine>?; `null!` hid it.
public Task<BackendCommandResult> RunAsync(
    BackendCommand command,
    IProgress<BackendOutputLine>? progress = null,
    CancellationToken cancellationToken = default)

// CS8625: SettingsSaveResult(bool Ok, string Error = "") already defaults to "";
// passing null asserted the opposite of the contract.
return new SettingsSaveResult(true);
```

Use `#pragma warning disable` only when the analyzer genuinely cannot model the
runtime shape, and say so in the comment. Never widen `WarningsAsErrors` to
`CS*`/`CA*` to make the ratchet pass: that turns every future SDK rule into an
unreviewable build break.

## Record (2026-10-20)

- 12 warnings fixed in `tests/SmsWorkbench.Tests`; baseline set to `0`.
- Measured while building the gate: SARIF 359 vs console 0 on a clean tree; a
  single shared `ErrorLog` losing all but the last project; WPF's `_wpftmp`
  project doubling every diagnostic; `dotnet` writing UTF-8 or the OEM code page
  depending on the console (the reason output is captured as bytes and decoded
  once).

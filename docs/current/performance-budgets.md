# Performance Budgets

Entrypoint costs that users feel, and the ceilings that guard them. The numbers
are **regression detectors**, not benchmarks: CI runs on a shared Windows
runner, so the ceilings are several times the measured value. A change of kind —
a heavy import returning to the CLI, help re-parsing everything, a resident read
falling back to a cold start — is what they are meant to catch.

Measured on 2026-10-19 (Windows, warm page cache, Python 3.12):

| Path | Measured | Ceiling | Guard |
| --- | ---: | ---: | --- |
| `import sms_tool.cli` | 0.17–0.28 s | 3 s | `test_performance_budget.py` |
| RSS after that import | ~24 MB | 150 MB | `test_performance_budget.py` |
| `chatgpt_phone_reg.py --help` | 0.7–1.1 s | 6 s | `test_performance_budget.py` |
| `import registration_handlers` | 42 MB RSS | — | documented only |
| `import commands.payment` | 32 MB RSS | — | documented only |
| Resident account delta, 434 rows | 3.5 KB | — | `test_desktop_read_delta.py` |
| Full account payload, 434 rows | 681 KB | — | comparison point |
| `--doctor --json` (warm) | 1.74 s | — | documented only |

Why some rows have no guard: they depend on the machine (network probes, Node
startup, antivirus) far more than the code, so a hard ceiling would be noise.
The measurable parts are pinned structurally instead — import surface
(`test_cli_import_surface.py`), delta shape (`test_desktop_read_delta.py`), and
the process-lifetime cache bounds (see `telemetry-and-runtime.md`).

## Reading a failure

- **Import time**: look for a new top-level import of `requests`, `curl_cffi`,
  `playwright` or a `commands.*` module in `sms_tool/cli.py`. The lazy proxies
  exist so the import surface stays at standard library + config + diagnostics.
- **RSS**: same cause, plus a large module-level table (a new fixture, a
  generated catalogue). Profile with `python -X importtime`.
- **Help**: argparse now builds every option; a parser module that imports a
  provider stack at registration time shows up here first.

## Adding a budget

Only add a ceiling when the number is stable on an idle machine and its
regression mode is known. Prefer a structural assertion (a module absence, a
payload shape) over a stopwatch when both would catch the same defect.

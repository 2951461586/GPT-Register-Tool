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
| Resident desktop read, 795 rows + 10 deltas | 37 MB RSS | 150 MB | `test_performance_budget.py` |
| Browser pool, 1 / 4 headless Chromium instances | 478 / 1907 MB RSS | — | documented only (measured 2026-10-21; Camoufox is heavier) |
| Account list read, 435 rows | 60.3 ms | — | documented only (measured 2026-10-20) |
| Full account payload, 434 rows | 681 KB | — | comparison point |
| `--doctor --json` (warm) | 1.74 s | — | documented only |

The resident row is measured by `tests/fixtures/resident_rss_probe.py` in a child
interpreter: it builds a 795-row database, runs one full `read_accounts` (every
session row parsed and sanitized into the row caches) and ten deltas. That is the
process an operator leaves running all day and the one path the entrypoint
budgets above never covered; without a ceiling an unbounded row cache is
invisible until the desktop host is hundreds of megabytes.

Why some rows have no guard: they depend on the machine (network probes, Node
startup, antivirus) far more than the code, so a hard ceiling would be noise.
The measurable parts are pinned structurally instead — import surface
(`test_cli_import_surface.py`), delta shape (`test_desktop_read_delta.py`), and
the process-lifetime cache bounds (see `telemetry-and-runtime.md`).

## The account list read must carry `raw_json` (measured 2026-10-20)

`store/accounts.py::list_account_records` is `SELECT * FROM accounts ORDER BY
updated_at DESC`: 435 rows, **60.3 ms**, and `raw_json` is 23.2 MB of it (median
58 KB/row). Projecting every column *except* `raw_json` measurably helps --
**60.3 ms -> 45.0 ms** through the production function on a copy of the real
database -- but it is **not** a free win: it was implemented, tested, and
reverted in the same change.

`desktop_read._record_payload` derives six fields by parsing that blob:
`promotion_status`, `promotion_state`, `payment_eligibility`, `imported_status`,
`paypal_amount` and `at_probe_status_code`.
`tests/workbench/test_desktop_read.py::test_lightweight_account_read_skips_full_session_sanitization`
failed immediately (`KeyError: 'imported_status'`), which is the test doing its
job: the account table's 优惠状态 column is built from `raw_json`, not from a
column.

So the read is not wasteful -- it is doing work the table needs. Making it lean
requires **materializing those six derived fields as columns** (computed on
write, backfilled for existing rows, and refreshed on every probe that mutates
the session), which is a schema change with a backfill and a drift risk, not a
query tweak. Deferred as its own task; the measurement above is what justifies
it.

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

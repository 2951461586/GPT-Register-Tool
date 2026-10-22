# Type Checking (pyright ratchet)

`scripts/pyright_ratchet.py` is the type gate. It runs pyright over `sms_tool`,
`services`, `scripts` and `tests` (paths narrowed by `pyrightconfig.json`),
counts **errors only**, and compares the per-file counts with
`scripts/pyright_baseline.json`.

## Why it is a ratchet

The tree was never type-clean: the first full run reported 1,453 errors, most of
them one of three shapes (`reportOptionalMemberAccess` on dict guards,
`reportAttributeAccessIssue` on mocks/Playwright stubs, and
`reportPossiblyUnboundVariable` on loop variables). A big-bang cleanup of that
size is unreviewable, so the count is frozen **per file** and may only go down:

- a file absent from the baseline has an allowance of 0, so new files must be
  clean;
- a shrink in one file cannot mask growth in another;
- the baseline also pins `total`, so a baseline that loses a row fails loudly.

`pyrightconfig.json` uses `typeCheckingMode: basic` deliberately. `standard` and
`strict` add the `reportUnknown*` families, whose volume tracks how deeply the
`Any`-heavy JSON payloads nest rather than how likely a defect is.

## Running it

```text
python -m pip install -r requirements-dev.txt -c constraints.txt   # installs pyright
python scripts/pyright_ratchet.py                # check vs baseline
python scripts/pyright_ratchet.py --detail       # per-file table + ungated warnings
python scripts/pyright_ratchet.py --update-baseline
```

`pyright` is declared in `requirements-dev.txt` and pinned in `constraints.txt`
(`DECLARED_NOT_IMPORTED` in `tests/governance/test_dependency_guard.py` records
why a tool nothing imports is still a declared dependency). It is invoked as
`python -m pyright`, so no separate Node install step is needed.

`--update-baseline` is only for **lowering** the count after a real cleanup; it
prints the per-file delta so a reviewer can see exactly what moved.

## In the test suite

`tests/governance/test_pyright_ratchet.py` pins the plumbing (a broken checker
must never look like a clean tree) and runs the real checker against the tree.
If pyright is missing the test fails with
`pyright produced no JSON output; cannot judge the tree` — that is the intended
signal, not a flake.

## Fixing an error

Prefer narrowing the value over silencing the rule:

```python
raw: object = payload.get("checkout")
checkout = raw if isinstance(raw, dict) else {}
```

is behaviour-identical to the `x.get(k) if isinstance(x.get(k), dict) else {}`
idiom and removes the error, because pyright evaluates the two `x.get(k)` calls
independently. Use a same-line `# pyright: ignore[ruleName]` only when the
checker genuinely cannot model the runtime shape (mocks, vendored payloads).

## Batch plan (2026-10-18)

The 920-error baseline is retired **file by file**, largest first, and each
batch must lower both its own `per_file` row and `total` through
`python scripts/pyright_ratchet.py --update-baseline`. A batch is not accepted
if it lowers a large file while any other file's count rises, so run the ratchet
before and after and compare the printed delta.

Order agreed on 2026-10-18 (counts are the frozen baseline; a batch may start
only when its file has a test that fails today):

| Batch | File | Errors | Shape of the work |
| --- | --- | ---: | --- |
| ~~1~~ | ~~`sms_tool/upi_link/stages.py`~~ | ~~101~~ | **Retired 2026-10-22, and the row's diagnosis was wrong.** The 101 were not optional-member access on staged dict guards: 97 were `reportAttributeAccessIssue` on `ops.<name>` reads, because `UpiOperations` builds `__slots__` from `UPI_OPERATION_NAMES` and declared no annotations, so no analyser could see the attributes. Fixed by adding a bare-annotation block (type-only, no class attribute, no runtime cost) plus `tests/governance/test_upi_operations_surface.py` pinning the annotations against the slot tuple. The other 4 were `reportInvalidTypeForm` on `state.<attr>: T = ...` assignments inside stage bodies — `UpiStageContext` already declares those fields, so the inline annotation was both rejected and redundant. |
| 2 | `tests/mailbox/test_mailbox_icloud_url.py` | 72 | Test fixtures typing; mechanical once the payload shapes are named. |
| 3 | `tests/registration/test_remail_mailbox.py` | 59 | Test fixtures typing. |
| 4 | `tests/proxy/test_proxy_entry.py` | 53 | Test fixtures typing. |
| 5 | `sms_tool/accounts/account_scan.py` | 32 | Payload narrowing in the scan loop. |
| 6 | remaining `sms_tool/` files (`registration/retry_guard.py` is already at 0) | — | Narrow values, never silence. |

Already retired (2026-10-18): `sms_tool/cli.py` (3 → 0),
`sms_tool/registration/retry_guard.py` (16 → 0 — the object-typed JSON reads now
go through `_as_int` / `_as_float`, which `cast` to `float | int | str` so an
unexpected type still raises exactly as before), and
`tests/registration/test_registration_hot_persistence.py` (38 → 0 — the recursive
`thaw` fixture now annotates its boundary as `dict[str, Any]`), and
`sms_tool/desktop_read.py` (1 → 0 — the pool-line parser now receives a `str`
path). Baseline after that round: **862**.

Retired 2026-10-22 (batches 1 and a one-line neighbour):
`sms_tool/upi_link/stages.py` (101 → 0) and
`sms_tool/registration/cancel.py` (1 → 0 — `_install_cancel_signal_handlers`
returned `list[tuple[int, object]]`, so the `signal.signal(sig, previous)`
restore passed an `object` where typeshed wants `_HANDLER`; the list and the
return annotation now use a local `SignalHandler` alias). Baseline after that
round: **313**.

Retired 2026-10-23 (the whole remaining tree, 313 -> 0): production code got
the annotations its own runtime guards already implied (`seeded_device_identity`
and `_value_matches` accept `None`, `_classify_exception` takes `BaseException`
because `CancelledError`/`KeyboardInterrupt` are not `Exception` subclasses,
`is_redirect_like_url` and the captcha solver's `_playwright_proxy` accept
`None`, the circuit breaker's `threshold` is validated rather than typed). Tests
narrow Optional returns with `assert ... is not None` instead of
`assertIsNotNone`, and reach deliberately-frozen or duck-typed doubles through
`cast(Any, ...)`. Two `pyrightconfig.json` gaps were closed rather than coded
around: the extractor tests' `sys.path` (`common`, `kakao_extract`,
`momo_qr_extract`) now has a matching `executionEnvironment`, which removed 13
phantom `reportMissingImports`. Baseline after this round: **0**.

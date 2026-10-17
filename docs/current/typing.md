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

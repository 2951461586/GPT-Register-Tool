# Rule 24 — An early command that emitted its result must stop dispatch

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 24`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `pytest tests/workbench/test_desktop_read.py::test_desktop_read_is_reported_as_handled`; `python chatgpt_phone_reg.py --desktop-read accounts --desktop-ipc` exits 0 with exactly one result frame
- **Source section**: CLI; WPF UI

## Rule

**An early command that emitted its result must stop dispatch.** `_main_early_commands` returns `True` for every command it handled (desktop serve / doctor / desktop read). A bare `return` returns `None`, which is falsy, so `main` fell through into `_load_heavy_deps` and `_main_registration_pipeline` after the result frame was already on stdout: `--desktop-read accounts --desktop-ipc` printed extra error frames and exited 2. The contract is pinned by a unit test rather than by the manual exit code, because the desktop host parses the envelope and would not have noticed the extra frames.

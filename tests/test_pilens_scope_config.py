"""Pins the ``pi-lens`` review-graph scope declared in ``.pi-lens.json``.

The scope is a *decision*, and it is load-bearing: `pi-lens`'s built-in
excluded-directory list changes between versions and does not cover `runtime/`,
`sessions/`, `.dotnet/`, the `.workbuddy*` state, `browser_extensions/`,
`outlook_results/` or the .NET `bin/`/`obj/` trees; everything else would fall
back to `.gitignore`, which `pi-lens` rescues for **tracked** files. `runtime/`
alone holds 340 Python files and grows without bound, so losing its exclusion
would silently pull operator state and the `runtime/tmp/refrepos/` clones into
the graph.

`scripts/` is excluded too, on purpose: the graph is an architecture view of the
**product**, and the gate/ratchet scripts are tooling. While they were in scope,
`DEAD WEIGHT` reported ten `scripts/*.py` files as unreachable on every build --
all false positives, because CI, the git hooks and `tests/test_*_ratchet.py`
invoke them by filename rather than importing them. The cost is that `pi-lens`'s
LSP and symbol search no longer cover `scripts/`; `ruff check scripts` in CI and
the per-ratchet tests are what cover it instead.

See ``docs/architecture.md`` "Review-graph scope (`pi-lens`)" for the reasoning
and for why `tests/` is deliberately *not* ignored.

This file also pins the **formatting** decision.  ``pi-lens`` auto-formats every
file it edits, but this repo enforces formatting on the protocol-registration
lane only (``scripts/format_guard.py``, wired into the pre-commit hook and CI).
Auto-formatting outside that lane reformatted four unrelated files -- 431
insertions / 180 deletions -- to fix pre-existing drift the project had
*decided* not to touch, burying the real edit.  ``format.enabled: false`` is the
project-scoped mutation control that stops it; the lane keeps its gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pi-lens.json"

#: Trees excluded only by `.gitignore` today (or named in the scope contract).
#: `dist/**` is also in pi-lens's built-in list, but the declaration is kept so
#: the repo does not depend on that list's contents.
REQUIRED_IGNORES = {
    "sms_tool/upi_link/_vendor/**",
    "scripts/**",
    "dist/**",
    "runtime/**",
    "sessions/**",
    ".dotnet/**",
    ".workbuddy/**",
    ".workbuddy-ai/**",
    "browser_extensions/**",
    "outlook_results/**",
    "SmsWorkbench/bin/**",
    "SmsWorkbench/obj/**",
    "SmsWorkbench.Contracts/bin/**",
    "SmsWorkbench.Contracts/obj/**",
    "tests/**/bin/**",
    "tests/**/obj/**",
}

#: The product. Ignoring any of these would remove the architecture view the
#: review graph exists to provide.
IN_SCOPE = (
    "sms_tool/registration_handlers.py",
    "sms_tool/auth_flow/steps.py",
    "sms_tool/upi_link/stages.py",
    "services/protocol-payment/common/protocol_core.py",
    "SmsWorkbench/App.xaml.cs",
    "SmsWorkbench.Contracts/BackendCommandPlanner.cs",
    # `tests/` is in scope on purpose: the review graph drops test files by role,
    # but the LSP/symbol index must keep them.
    "tests/test_format_guard.py",
)

#: Project-scope keys pi-lens honors. A typo here is logged once by the loader
#: and otherwise ignored, so the test catches it instead.
ALLOWED_TOP_LEVEL = {
    "$schema",
    "ignore",
    "rules",
    "maxProjectFiles",
    "reviewGraph",
    "trivy",
    "helm",
    "format",
    "autofix",
    "actionableWarnings",
}


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _matches(pattern: str, path: str) -> bool:
    """Match the only shapes this config uses: ``prefix/**`` and exact paths."""
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return path == pattern


def test_the_config_is_valid_json_with_a_schema_pointer():
    config = _config()
    assert config["$schema"].startswith("https://")
    assert isinstance(config["ignore"], list)
    assert all(isinstance(entry, str) and entry for entry in config["ignore"])


def test_no_unknown_project_keys():
    unknown = set(_config()) - ALLOWED_TOP_LEVEL
    assert not unknown, f"pi-lens logs these once and ignores them: {sorted(unknown)}"


def test_every_required_exclusion_is_declared():
    declared = set(_config()["ignore"])
    missing = REQUIRED_IGNORES - declared
    assert not missing, f"review-graph scope lost these exclusions: {sorted(missing)}"


def test_the_vendored_tree_stays_ignored():
    """The one entry that predates this test; deleting it would graph the vendor."""
    assert "sms_tool/upi_link/_vendor/**" in _config()["ignore"]


@pytest.mark.parametrize("path", IN_SCOPE)
def test_product_paths_stay_in_scope(path: str):
    offenders = [pattern for pattern in _config()["ignore"] if _matches(pattern, path)]
    assert not offenders, f"{path} is in scope but is excluded by {offenders}"


def test_scripts_are_excluded_on_purpose():
    """Tooling, not product.

    In scope they made `DEAD WEIGHT` report ten `scripts/*.py` as unreachable on
    every build (all false positives -- CI and the hooks invoke them by
    filename). The cost is stated in ``docs/architecture.md``: no `pi-lens` LSP
    or symbol search under `scripts/`, covered instead by `ruff check scripts`
    and the per-ratchet tests.
    """
    assert "scripts/**" in _config()["ignore"]
    assert _matches("scripts/**", "scripts/format_guard.py")
    assert _matches("scripts/**", "scripts/import_layer_ratchet.py")


def test_tests_are_not_ignored_wholesale():
    """The graph drops test files by role; the symbol index must not.

    Ignoring `tests/**` would cost LSP diagnostics and symbol search on 340
    files to buy the review graph nothing.
    """
    patterns = _config()["ignore"]
    assert "tests/**" not in patterns
    assert "tests/" not in patterns
    # The .NET test-project build outputs are a different thing and stay ignored.
    assert "tests/**/bin/**" in patterns
    assert "tests/**/obj/**" in patterns


def test_ignored_patterns_never_target_a_product_directory_root():
    """A pattern like `sms_tool/**` would erase the graph's subject.

    `scripts/**` is deliberately *not* in this set -- it is excluded by decision
    (see :func:`test_scripts_are_excluded_on_purpose`), and it is not a product
    root.
    """
    product_roots = {"sms_tool", "services", "SmsWorkbench", "SmsWorkbench.Contracts"}
    for pattern in _config()["ignore"]:
        head = pattern.split("/", 1)[0]
        if head in product_roots and pattern == f"{head}/**":
            pytest.fail(f"{pattern} excludes an entire product tree")


# ---------------------------------------------------------------------------
# Auto-formatting is off; the lane keeps its gate
# ---------------------------------------------------------------------------


def test_pi_lens_autoformat_is_disabled():
    """Formatting is a *gate* here, not an editor side effect.

    ``pi-lens`` runs a deferred formatter over every file it edits.  This repo
    formats the protocol-registration lane only, and ``scripts/format_guard.py``
    states why in its own docstring:

        Scope is deliberately the *lane*, not the whole package: widening it to
        ``sms_tool/`` would reformat ~100 unrelated files in one commit and bury
        the real edit.

    On 2026-10-10 the auto-formatter did exactly that: four files *outside* the
    lane were reformatted to fix pre-existing drift the project had decided not
    to touch (431 insertions / 180 deletions across
    ``accounts/account_payment_eligibility.py``, ``pay_link/__init__.py``,
    ``services/protocol-payment/momo/ac_paylink_core.py`` and
    ``tests/test_account_payment_eligibility.py``).

    ``format.enabled`` is a project-scoped mutation control, so this is the
    supported way to stop it -- not a global preference.  See
    ``docs/architecture.md`` "Review-graph scope (`pi-lens`)".
    """
    assert _config().get("format") == {"enabled": False}


def test_the_format_lane_is_still_gated():
    """Disabling auto-format must not leave the lane unformatted.

    ``format_guard.py`` is what enforces the lane, and it is wired into both the
    pre-commit hook and CI.  If that wiring ever goes away, turning off
    auto-format stops being safe -- so the two decisions are pinned together.
    """
    guard = ROOT / "scripts" / "format_guard.py"
    assert guard.is_file()
    source = guard.read_text(encoding="utf-8")
    # The lane itself: the scope is data, so assert the declaration, not a call site.
    assert 'SCOPE_DIRS = ("sms_tool/auth_flow",)' in source
    assert 'SCOPE_GLOBS = ("sms_tool/registration_*.py",)' in source

    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/format_guard.py" in hook
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/format_guard.py" in ci

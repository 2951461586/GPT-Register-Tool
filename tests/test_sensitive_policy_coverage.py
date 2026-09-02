"""Guard the redaction policy against newly-introduced credential fields.

Why this exists
---------------
Round 5 of the audit (2026-09-02) found that `sensitive_policy.json` matched
only FULL key names, so every prefixed variant leaked in the clear:
`smsbower_api_key`, `smailr_api_key`, bare `cookie`, bare `session_id`.

Fixing the six known gaps is not enough - the same class of bug comes back the
next time someone adds a field. This test closes the loop from the other end:
instead of asserting a hand-written list of cases, it harvests every string key
that actually appears as a dict literal in the shipped source, keeps the ones
that look like credentials, and asserts the policy redacts them.

It is intentionally narrow to avoid the failure mode that gets guards bypassed
(a noisy guard gets `--no-verify`'d into irrelevance):
  * only dict-literal string keys are considered - not variable names, not
    keywords, not test fixtures;
  * only keys carrying an unambiguous credential root are considered;
  * keys ending in a declared `safe_key_suffixes` entry are skipped.

See docs/audit-2026-09-02-round5-summary.md section 0.1 and 4.2.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from sms_tool.sanitizer import SENSITIVE_POLICY, sanitize

ROOT = Path(__file__).resolve().parents[1]

# Source trees that ship to users. `dist/` and `runtime/` are copies of this
# same source and would triple the key count; `tests/` holds fixtures that are
# not real data.
SCAN_ROOTS = ("sms_tool", "services", "scripts")
SKIP_PARTS = {"__pycache__", ".venv", "dist", "runtime"}

# Unambiguous credential roots. Deliberately NOT included: bare `key`
# (would swallow `keyword` / `sort_key` / `monkey`) and bare `auth`
# (would swallow `author` / `authored` / `auth_status`).
CREDENTIAL_ROOT = re.compile(
    r"(?:token|secret|password|cookie|session[_-]?id|api[_-]?key)",
    re.IGNORECASE,
)

# The fragments the policy must keep. If someone trims a fragment to silence a
# false positive, this fails loudly instead of silently re-opening a leak.
REQUIRED_FRAGMENTS = frozenset(
    {"token", "secret", "password", "cookie", "session_id", "api_key"}
)

PROBE = "ZZFAKE123456"


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            files.append(path)
    return sorted(files)


def _dict_keys(tree: ast.AST) -> set[str]:
    """Collect string keys used in dict literals across the module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if key is None:  # `**expansion`
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.add(key.value)
    return found


def _collect_candidate_keys() -> dict[str, list[str]]:
    """key -> sorted list of 'path:line' locations where it appears."""
    safe_suffixes = tuple(
        str(item).lower() for item in SENSITIVE_POLICY.get("safe_key_suffixes") or ()
    )
    # `safe_key_paths` is path-scoped (e.g. `proxy_affinity.session_id`), but
    # this guard only sees bare key names - it has no way to know whether a
    # given `session_id` literal sits inside `proxy_affinity` or not. So the
    # leaf names of declared safe paths are skipped here and covered instead by
    # the path-precision cases in test_sanitizer.py, which assert the exemption
    # is surgical (a look-alike path is still redacted).
    exempt_leaves = frozenset(
        str(item).lower().rsplit(".", 1)[-1]
        for item in SENSITIVE_POLICY.get("safe_key_paths") or ()
    )
    locations: dict[str, list[str]] = {}

    for path in _iter_source_files():
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue  # unparseable source must not break the guard

        for key in _dict_keys(tree):
            lowered = key.lower()
            if not CREDENTIAL_ROOT.search(lowered):
                continue
            if safe_suffixes and lowered.endswith(safe_suffixes):
                continue
            if lowered in exempt_leaves:
                continue
            where = f"{path.relative_to(ROOT).as_posix()}"
            locations.setdefault(key, [])
            if len(locations[key]) < 3:  # keep the report short
                locations[key].append(where)

    return dict(sorted(locations.items()))


CANDIDATE_KEYS = _collect_candidate_keys()


def test_policy_keeps_the_fragments_that_close_the_round5_gaps():
    present = frozenset(
        str(item).lower() for item in SENSITIVE_POLICY.get("sensitive_key_fragments") or ()
    )
    missing = sorted(REQUIRED_FRAGMENTS - present)
    assert not missing, (
        "sensitive_policy.json lost fragment(s) that the round-5 audit added: "
        f"{missing}. Removing one re-opens a credential leak."
    )


def test_harvest_found_credential_shaped_keys():
    # Meta-check: if this ever reports 0, the harvester broke (path filter or
    # AST visitor) and every assertion below silently passes for the wrong
    # reason. Keep the bar low but non-zero.
    assert len(CANDIDATE_KEYS) > 0, (
        "no credential-shaped dict keys were harvested - the scanner itself is "
        "broken, so the coverage assertion below is vacuous"
    )


@pytest.mark.parametrize("key", sorted(CANDIDATE_KEYS))
def test_credential_shaped_key_is_redacted(key: str):
    where = ", ".join(CANDIDATE_KEYS[key])
    redacted = sanitize({key: PROBE})[key]
    assert redacted == "[REDACTED]", (
        f"dict key {key!r} looks like a credential but survives sanitize() "
        f"unchanged, so it would reach logs/JSONL reports in the clear "
        f"(seen in: {where}). Add it to sensitive_keys or widen "
        f"sensitive_key_fragments in sensitive_policy.json."
    )

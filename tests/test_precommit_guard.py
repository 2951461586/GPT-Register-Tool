"""Tests for scripts/precommit_guard.py.

The last test scans every tracked file.  That matters beyond the local hook:
CI already runs pytest, so this check runs on every push *without* touching
.github/workflows/ci.yml.  The guard therefore still protects the repository
even for clones that never ran install_git_hooks.py.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "precommit_guard.py"

sys.path.insert(0, str(ROOT / "scripts"))
import precommit_guard  # noqa: E402


def run_guard_on(tmp_path: Path, filename: str, content: str) -> list[str]:
    """Write a file under tmp_path and return guard findings for it.

    Previously this wrote into the repo's runtime/ directory — the production
    data area (accounts.sqlite3, browser_profiles/). Leftover pytest logs there
    (runtime/_pytest_storage_split.log) contain deliberately fake ba_token
    values that make sensitive_field_scan fail, i.e. the test was reddening an
    unrelated gate. tmp_path works now that scan_file tolerates out-of-repo paths.
    """
    target = Path(tmp_path) / filename
    target.write_text(content, encoding="utf-8")
    try:
        return precommit_guard.scan_file(target, precommit_guard.load_sensitive_keys())
    finally:
        target.unlink(missing_ok=True)


# --------------------------------------------------------------- filename gate


@pytest.mark.parametrize(
    "rel",
    [
        "proxy.json",
        "runtime.json",
        "payment.json",
        "config.json",
        "mailbox_tokens.txt",
        "kyl_state.json",
        "nested/foo_state.json",
        "sms_tool/config.json",  # gitignored local config, same as the root one
    ],
)
def test_local_artefact_filenames_are_blocked(rel: str) -> None:
    assert precommit_guard.name_is_blocked(rel) is True


@pytest.mark.parametrize(
    "rel",
    [
        "config.example.json",
        "sensitive_policy.json",
        "scripts/installer/packages.lock.json",
        "src/app_state_machine.py",  # suffix must match a file, not a path part
    ],
)
def test_allowlisted_names_are_not_blocked(rel: str) -> None:
    assert precommit_guard.name_is_blocked(rel) is False


def test_config_example_is_not_blocked_but_real_config_is() -> None:
    """The exact incident from 2026-08-30: sharding created new filenames."""
    assert precommit_guard.name_is_blocked("config.example.json") is False
    for shard in ("proxy.json", "runtime.json", "payment.json"):
        assert precommit_guard.name_is_blocked(shard) is True


# ---------------------------------------------------------------- content gate


def test_json_api_key_value_is_detected() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_secret.json",
        '{"smsbower": {"api_key": "d0ZH9kQ2mX7pL4nR8vT1wY6bC3fA5eQC"}}\n',
    )
    assert findings, "real-looking api_key value must be detected"
    assert findings[0][2] == "api_key"


def test_vendor_prefix_is_detected() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_vendor.py",
        'ROXY_TOKEN = "cfat_9f3e2a1b7c8d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d"\n',
    )
    assert findings, "cfat_ vendor token must be detected"


def test_hex_constant_is_detected() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_hex.py",
        'TOKEN = "556c27e3a1b7c8d4e5f6a7b8c9d0e1f2a"\n',
    )
    assert findings, "32-char hex token must be detected"


def test_proxy_url_with_credentials_is_detected() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_proxy.py",
        'PROXY = "http://alice:s3cr3tPassword@gate.example.com:1000"\n',
    )
    assert any("proxy-url" in f[2] for f in findings)


def test_proxy_url_placeholder_is_allowed() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_proxy_placeholder.py",
        'PROXY = "socks5h://user:pass-JP@gate:1000"\n',
    )
    assert findings == [], "documented placeholder shape is not a leak"


def test_proxy_fstring_template_is_allowed() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_proxy_fstring.py",
        'return f"http://{user_q}:{pass_q}@{host}:{port}"\n',
    )
    assert findings == [], "f-strings that build a URL are code, not credentials"


def test_regex_definition_is_allowed() -> None:
    """r'^nm_[A-Za-z0-9_-]{20,}$' is a pattern, not a token."""
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_regex.py",
        "NM = re.compile(r'^nm_[A-Za-z0-9_\\-]{20,}$')\n",
    )
    assert findings == [], "regex definitions must not be flagged"


def test_docstring_example_is_allowed() -> None:
    content = textwrap.dedent(
        '''
        def enroll():
            """Enroll TOTP.

            Returns:
                {"ok": True, "totp_secret": "JBSWY3DPEHPK3PXP", ...}
            """
            return None
        '''
    )
    findings = run_guard_on(ROOT / "runtime", "_t_docstring.py", content)
    assert findings == [], "docstring examples must not be flagged"


def test_comment_is_allowed() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_comment.py",
        '# api_key = "d0ZH9kQ2mX7pL4nR8vT1wY6bC3fA5eQC"\n',
    )
    assert findings == []


def test_short_placeholder_value_is_allowed() -> None:
    findings = run_guard_on(
        ROOT / "runtime",
        "_t_short.json",
        '{"api_key": "changeme"}\n',
    )
    assert findings == []


def test_findings_never_contain_the_full_value() -> None:
    secret = "d0ZH9kQ2mX7pL4nR8vT1wY6bC3fA5eQC"
    findings = run_guard_on(
        ROOT / "runtime", "_t_mask.json", '{"api_key": "%s"}\n' % secret
    )
    assert findings
    rendered = " ".join(str(part) for row in findings for part in row)
    assert secret not in rendered, "guard must not print full secret values"


# -------------------------------------------------------------- repository gate


def test_no_tracked_file_contains_credentials() -> None:
    """Every tracked file must pass the guard.

    This is the test that matters in CI: it fails loudly if a credential ever
    reaches the index again.
    """
    result = subprocess.run(
        [sys.executable, str(GUARD), "--all"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "precommit guard found credentials in tracked files:\n"
        + (result.stdout or "")
        + (result.stderr or "")
    )

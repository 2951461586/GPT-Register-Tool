"""P7 — the desktop's direct-HTTP surface has one owner, one allowlist, one log.

The desktop is a shell: provider protocol belongs to ``sms_tool``. Two surfaces
still need their own socket -- the one-click SMS catalog/balance reads (an
interactive choice) and the release check -- and before 2026-11-01 each built its
own ``HttpClient`` with no proxy, no logging and hosts scattered across two files.

``DesktopEgress`` is now the only place a client is created. These tests pin the
three properties that used to be unenforceable: nothing else opens a socket, the
allowlist covers every host the callers actually use, and no new host can be added
by editing an endpoint in a different file.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMS_WORKBENCH = ROOT / "SmsWorkbench"
CONTRACTS = ROOT / "SmsWorkbench.Contracts"

EGRESS = SMS_WORKBENCH / "Services" / "DesktopEgress.cs"
ALLOWLIST = CONTRACTS / "DesktopEgressAllowlist.cs"
CATALOG_MODEL = SMS_WORKBENCH / "Models" / "SmsProviderCatalog.cs"
UPDATE_CHECKER = SMS_WORKBENCH / "Services" / "UpdateChecker.cs"

_URL = re.compile(r"https://([A-Za-z0-9.\-]+)")
_BARE_HOST = re.compile(r'"([a-z0-9][a-z0-9.\-]+\.[a-z]{2,})"')


#: Files that legitimately contain a URL but never dial it. Each needs a reason:
#: an unexplained entry is how a second egress would sneak back in.
NON_EGRESS_URL_OWNERS = {
    "BackendJson.cs": "the literal is a JSON *key* in an auth-session payload ('https://api.openai.com/auth'); the desktop never calls OpenAI",
    "SettingsCatalog.cs": "config default values (`remail_base_url`, `smailr_base_url`) handed to sms_tool; the desktop reads them, never dials them",
    "SmsProviderCatalog.cs": "the vendor endpoint table is vocabulary handed to sms_tool (P7-B moved the reads there); the desktop never dials these hosts",
}


def _csharp_files() -> list[Path]:
    return sorted(
        path
        for root in (SMS_WORKBENCH, CONTRACTS)
        for path in root.rglob("*.cs")
        if "obj" not in path.parts and "bin" not in path.parts
    )


def _code(path: Path) -> str:
    """Source with line comments removed: prose may *name* a URL or an API."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8-sig").splitlines() if not line.lstrip().startswith("//")
    )


def _allowlisted_hosts() -> set[str]:
    """The allowlist stores bare hosts (the scheme is enforced separately)."""
    return set(_BARE_HOST.findall(_code(ALLOWLIST)))


def test_only_the_egress_creates_an_http_client():
    offenders = [path.name for path in _csharp_files() if "new HttpClient(" in _code(path) and path != EGRESS]
    assert offenders == [], f"these files open their own socket instead of using DesktopEgress: {offenders}"


def test_no_other_file_hard_codes_an_outbound_url():
    """XAML/docs may link; C# outside the three URL owners must not."""
    owners = {ALLOWLIST, UPDATE_CHECKER, CATALOG_MODEL}
    offenders = [
        path.name
        for path in _csharp_files()
        if path not in owners and path.name not in NON_EGRESS_URL_OWNERS and _URL.search(_code(path))
    ]
    assert offenders == [], f"these files hard-code an outbound URL outside the allowlist: {offenders}"
    # An exemption whose file stops carrying a URL is stale permission, not permission.
    stale = [
        name
        for name in NON_EGRESS_URL_OWNERS
        if not any(path.name == name and _URL.search(_code(path)) for path in _csharp_files())
    ]
    assert stale == [], f"drop these exemptions, the file no longer names a URL: {stale}"


def test_the_allowlist_is_only_the_release_feed():
    """P7-B moved the vendor reads into the backend, so this set must have shrunk.

    A host nobody calls is an unnecessary hole; this pins the set, not only coverage.
    """
    assert _allowlisted_hosts() == {"api.github.com"}


def test_the_allowlist_covers_every_host_the_remaining_caller_uses():
    """The update checker is the only direct egress left; its host must be allowed."""
    checker_hosts = set(_URL.findall(_code(UPDATE_CHECKER)))
    allowed = _allowlisted_hosts()

    assert checker_hosts, "the update checker declares no release host"
    assert checker_hosts <= allowed, f"release host missing from the allowlist: {sorted(checker_hosts - allowed)}"


def test_the_egress_refuses_plain_http():
    assert "Uri.UriSchemeHttps" in _code(ALLOWLIST), "the allowlist must require HTTPS, not only a host match"

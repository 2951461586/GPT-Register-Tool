"""Pre-commit guard: refuse to stage credential files or hardcoded secrets.

Why this exists
---------------
On 2026-08-30 the config was sharded from ``config.json`` into
``proxy.json`` / ``runtime.json`` / ``payment.json``.  ``config.json`` was
already in ``.gitignore`` but the three new filenames were not, so eight
plaintext credentials were committed.  A filename-based ``.gitignore`` cannot
catch the next new filename, so this guard adds a *content-based* gate that
runs before every commit.

It also blocks runtime state dumps (``*_state.json``) from being committed --
that is how ``scripts/kyl_protocol_runner/kyl_state.json`` (183 account
records) ended up in public history in June 2026.

Standard library only: the hook may be executed by any interpreter on PATH.
Never prints a full secret value -- only file, line, variable name, length
and a 3-char prefix.

Usage:
    python scripts/precommit_guard.py              # check staged files
    python scripts/precommit_guard.py --all        # check every tracked file
    python scripts/precommit_guard.py --all --dry  # report only, never fail
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- filename gate

# Reject by filename regardless of content.  These are always local/runtime
# artefacts; committing them is never intentional.
BLOCKED_NAMES = {
    "config.json",
    "proxy.json",
    "runtime.json",
    "payment.json",
    "session.json",
    "mailbox_tokens.txt",
    ".env",
}

BLOCKED_SUFFIXES = (
    "_tokens.txt",
    "_state.json",
    "_credentials.json",
)

# The blocked names above are EXACT, so `proxy.json.bak-vn-migration` sailed past
# both this gate and .gitignore (2026-09-11: it held 100 proxy user:pass pairs
# plus an smsbower api_key while sitting untracked in the public repo root).
# Snapshot suffixes are unbounded — `.bak`, `.bak-<desc>`, `.old`, `.save` — so
# match the credential-config FAMILY instead of enumerating suffixes.
BLOCKED_CONFIG_FAMILY = re.compile(r"^(?:config|proxy|runtime|payment|session)\.json(?:\..+)?$")

# Explicitly allowed even though the name looks dangerous.
ALLOWED_EXACT = {
    "sensitive_policy.json",  # policy definitions, not values
    "config.example.json",  # template with placeholders
    "packages.lock.json",
    "skills-lock.json",
    "payment_methods.json",
    "global.json",
    "proxy.json.example",  # mirrors the .gitignore exception
    "config.json.example",
}

ALLOWED_SUFFIXES = (".example.json",)

# Explicitly allowed *directories*. `scripts/move_specs/` holds the migration specs
# consumed by `module_move.py`, and each file is named after the package it moves
# (`proxy.json`, `payment.json`, ...) -- so the family rule above matches them by
# construction, exactly like `.gitignore`'s `proxy.json*` did before it was anchored.
# They are schema-validated JSON (package / layer / module map) with no credential
# surface, and the **content** gate below still runs on them: this lifts the name
# check only, not the credential scan.
ALLOWED_DIRS = ("scripts/move_specs/",)

# ---------------------------------------------------------------- content gate

# Detection is deliberately NARROW.  A generic "high entropy near a word that
# contains 'key'" detector produced 40+ false positives on this repo
# (hCaptcha/reCAPTCHA *site* keys are public by design, docs quote
# http://user:pass@host examples, error-code constants are long strings).
# A noisy gate gets bypassed with --no-verify, which is worse than no gate.
# So: only flag things that are either a known vendor prefix, an exact field
# name declared sensitive in sensitive_policy.json, or a bare hex blob.

# CONSTANT = "<32+ hex chars>" -- how the 2026-08-30 Roxy token leak looked.
# Restricting to pure hex skips hCaptcha/reCAPTCHA site keys and registry paths.
PAT_CONST_HEX = re.compile(
    r"""(?P<name>(?:[A-Z][A-Z0-9_]*)?(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Z0-9_]*)\s*[:=]\s*["'](?P<val>[0-9a-fA-F]{32,})["']"""
)
# "api_key": "value" -- key name must be declared sensitive in the shared policy.
PAT_JSON_TEMPLATE = r"""["'](?P<name>{names})["']\s*:\s*["'](?P<val>[A-Za-z0-9_\-\.]{{16,}})["']"""
# http://user:pass@host — also the shape that leaked. Before 2026-11-02 this rule
# ran only outside docs/tests and only for the URL form: both real provider
# credentials found that day were in the skipped trees, and one used the
# four-segment form below.
PAT_PROXY_URL = re.compile(
    r"(?i)\b(?:https?|socks(?:4|5|5h)?)://(?P<user>[^\s/:@\"'<>]+):(?P<pw>[^\s/@\"'<>]+)@(?P<host>[^\s/\"'<>]+)"
)
# ``gate:1000:user:pass`` — the provider's four-segment form. A bare
# ``host:port:user`` stays valid config and is not matched (no password).
PAT_PROXY_FOUR = re.compile(
    r"(?<![\w.@:/-])(?P<host>[A-Za-z0-9][A-Za-z0-9.-]{3,}):(?P<port>\d{2,5}):"
    r"(?P<user>[A-Za-z0-9_.%^-]{3,}):(?P<pw>[A-Za-z0-9_.%^#!-]{3,})(?![\w.-])"
)
PAT_EXAMPLE_URL = re.compile(r"""(?i)\b(?:https?|socks[45]h?)://[^\s"'<>]+""")
# A fixture is a credential that *says* it is one. This repo's own conventions:
# ``SYNTHACCT10-geo-VN-sid-...``, ``user1_custom_zone_VN_sid_..._time_5``, ``user:pass``.
SYNTHETIC_CRED = re.compile(
    r"(?i)(synth|yours?[_-]?|example|placeholder|redacted|sample|dummy|fake|changeme|"
    r"notareal|custom[_-]zone|(?:^|[_.-])(?:sid|time)(?:[_.-]|$))"
)
SYNTHETIC_WORDS = frozenset(
    {
        "user",
        "usr",
        "username",
        "login",
        "account",
        "acct",
        "acc",
        "admin",
        "alice",
        "bob",
        "foo",
        "bar",
        "pass",
        "passwd",
        "password",
        "pwd",
        "pw",
        "secret",
        "token",
        "cred",
        "auth",
        "key",
        "apikey",
        "u",
        "p",
        "us",
        "test",
        "demo",
        "name",
        "proxy",
        "upstream",
        "gate",
        "host",
        "exit",
        "lane",
        "first",
        "second",
        "third",
        "primary",
        "secondary",
        "plain",
        "checkout",
        "promotion",
        "provider-a",
        "provider-b",
        "static-a",
        "static-b",
    }
)

PLACEHOLDER = re.compile(
    r"^(your|xxx|placeholder|example|sample|test_|dummy|fake|changeme|redacted|"
    r"removed|none|null|todo|abc123|<|\*{3,})",
    re.I,
)
# Names that are public-by-design or clearly not secrets even when long.
BENIGN_NAME = re.compile(
    r"(?i)(site[_-]?key|client[_-]?id|placeholder|probe|fallback|recaptcha|hcaptcha|"
    r"unauthorized[_-]?code|persistence[_-]?key)"
)

# Well-known vendor prefixes: a hit is a real credential with near-zero doubt.
# The length requirement is baked in so that regex *definitions* such as
# r'^nm_[A-Za-z0-9_-]{20,}$' do not match -- the '[' breaks the character class.
# Stripe pk_live_/pk_test_ are publishable keys (public by design) and are
# therefore NOT listed here; only sk_ (secret) keys are.
VENDOR_PREFIX = re.compile(
    r"\b(?:rk-e|nm_|cfat_|sk_live_|sk_test_|ghp_|github_pat_|"
    r"xoxb-|xoxp-|AIza|AKIA|glpat-|dop_v1_|shpat_)[A-Za-z0-9_-]{16,}"
)

MIN_LEN = 16
MIN_ENTROPY = 3.0

# ------------------------------------------------------------ alias mailbox gate

# Apple "Hide My Email" style aliases (`<word>-<word>-<n>+oai01` on icloud.com) identify
# real registered accounts. The content gate above skips docs/ and tests/ on
# purpose (fake credentials are expected there), which is exactly where 40+ real
# aliases ended up in audit reports. This gate therefore runs on EVERY text file.
ALIAS_MAILBOX = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._-]{3,64})(?:\+[A-Za-z0-9._-]+)?(?:@|%40)(?:icloud|me|mac)\.com",
    re.I,
)
# Placeholder local-parts that are fixtures by construction. Anything else on an
# Apple domain is treated as a real alias; use ``sample-<n>`` in new fixtures.
SYNTHETIC_MAILBOX_LOCALS = frozenset(
    {
        "user",
        "target",
        "ok",
        "dead",
        "also",
        "test",
        "plain",
        "first",
        "second",
        "who",
        "someone",
        "name",
        "alpha.one",
        "beta.two",
        "foo",
        "alias",
        "sample",
        "example",
        "noreply_at_tm_openai_com",
    }
)


def scan_alias_mailboxes(rel: str, text: str) -> list[tuple[str, int, str, int, str]]:
    """Return findings for non-placeholder Apple-domain mailbox addresses.

    The address itself is never echoed: only its length is reported.
    """
    findings: list[tuple[str, int, str, int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in ALIAS_MAILBOX.finditer(line):
            local = match.group(1).lower()
            if local in SYNTHETIC_MAILBOX_LOCALS or local.startswith("sample-"):
                continue
            findings.append((rel, number, "alias-mailbox", len(local), ""))
    return findings


# Directories where a credential-looking string is an example by definition.
DOC_OR_TEST = re.compile(r"(?i)(^|/)(docs?|tests?)/|(\.(md|html|rst))$")


def _is_template_credential(value: str) -> bool:
    """True for a URL built at runtime, e.g. ``f"http://{u}:{p}@{host}"``."""
    return any(token in value for token in ("{", "}", "%(", "$(", "<", ">"))


def _is_synthetic_credential(*values: str) -> bool:
    for value in values:
        lowered = unquote(value).lower()
        # An already-masked value (`***:***@host`) is the opposite of a leak: the
        # log/UI redaction helper produced it. Exempting it also keeps this gate
        # from demanding that tests stop asserting on redacted output.
        if re.fullmatch(r"\*+|x{3,}|\u2026+|<[^>]*>|\{[^}]*\}", lowered):
            return True
        if SYNTHETIC_CRED.search(lowered):
            return True
        if lowered in SYNTHETIC_WORDS:
            return True
        # ``u-cru`` / ``u-z``: role-suffixed placeholders used by the pool tests.
        if re.fullmatch(r"[a-z]{1,2}(?:-[a-z]{1,4})?", lowered):
            return True
    return False


def _is_fake_host(host: str) -> bool:
    """True when the host cannot be a live provider exit."""
    name = host.split("@")[-1].split("://")[-1]
    name = name.split("/")[0].strip("[]").lower()
    if not name:
        return True
    labels = name.split(":")[0].split(".")
    if any(label in {"example", "test", "invalid", "localhost", "local"} for label in labels):
        return True
    if len(labels) == 1:
        # Bare host: ``host``, ``gate``, ``h1``, ``exit2``.
        return True
    try:
        address = ipaddress.ip_address(labels[0] if len(labels) == 1 else name.split(":")[0])
    except ValueError:
        return False
    # Documentation / private / loopback ranges, plus the reserved test IPs this
    # repo uses in fixtures (they are globally routable numbers, so ``is_private``
    # cannot see them).
    return (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or str(address) in {"1.2.3.4", "5.6.7.8", "9.9.9.9", "2.2.2.2", "10.0.0.2"}
    )


def scan_proxy_endpoints(rel: str, text: str) -> list[tuple[str, int, str, int, str]]:
    """Flag provider-shaped proxy credentials anywhere, docs and tests included.

    Why this is a separate gate: the value patterns in :func:`scan_file` return
    early for ``docs/`` and ``tests/`` — fake credentials are that tree's normal
    content. That exemption is exactly how a real rola pool entry lived in two
    test files for 2.5 months in a public repository, and how a kookeey entry
    survived in a parsing test since 2026-08-06. Fixtures stay welcome; what is
    forbidden is a *provider-shaped* pair with no synthetic marker.

    Neither the username nor the password is echoed — only the host.
    """
    findings: list[tuple[str, int, str, int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in PAT_PROXY_URL.finditer(line):
            user, password, host = match.group("user"), match.group("pw"), match.group("host")
            if _is_template_credential(user) or _is_template_credential(password):
                continue
            if _is_synthetic_credential(user, password) or _is_fake_host(host):
                continue
            findings.append((rel, number, "proxy-url-credentials", len(user) + len(password), host))
        for match in PAT_PROXY_FOUR.finditer(line):
            user, password, host = match.group("user"), match.group("pw"), match.group("host")
            # Require a dotted host: ``gate.kookeey.info:1000:u:p`` yes, a bare
            # ``switch:1080:a:b`` is not a proxy credential we can judge.
            if "." not in host or _is_template_credential(user) or _is_template_credential(password):
                continue
            if _is_synthetic_credential(user, password) or _is_fake_host(host):
                continue
            findings.append((rel, number, "proxy-endpoint-credentials", len(user) + len(password), host))
    return findings


def load_sensitive_keys() -> set[str]:
    """Exact field names declared sensitive in the shared policy."""
    policy_path = ROOT / "sensitive_policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    return {str(k).lower() for k in policy.get("sensitive_keys") or []}


TEXT_SUFFIXES = {
    ".py",
    ".cs",
    ".json",
    ".js",
    ".ts",
    ".ps1",
    ".sh",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".xml",
    ".config",
    ".props",
    ".ini",
    ".toml",
    ".env",
    ".example",
}
MAX_FILE_BYTES = 4 * 1024 * 1024


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def looks_like_secret(value: str) -> bool:
    if len(value) < MIN_LEN:
        return False
    if PLACEHOLDER.match(value):
        return False
    if value.lower().startswith(("http://", "https://")):
        return False
    # json-pointer / dotted config path, e.g. "registration.drivers.roxy"
    if value.count(".") >= 2 and " " not in value:
        return False
    if VENDOR_PREFIX.search(value):
        return True
    return shannon_entropy(value) >= MIN_ENTROPY


def name_is_blocked(rel: str) -> bool:
    if rel.startswith(ALLOWED_DIRS):
        return False
    name = Path(rel).name
    if name in ALLOWED_EXACT:
        return False
    if name.endswith(ALLOWED_SUFFIXES):
        return False
    if BLOCKED_CONFIG_FAMILY.match(name):
        return True
    if name in BLOCKED_NAMES:
        return True
    return name.endswith(BLOCKED_SUFFIXES)


def iter_scannable_lines(text: str, suffix: str):
    """Yield (lineno, line), skipping comments and docstrings.

    Docstring examples are the single largest false-positive source here
    (e.g. ``{"totp_secret": "JBSWY3DPEHPK3PXP"}`` in accounts/account_2fa.py).
    """
    in_docstring = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if suffix in (".py", ".sh", ".ps1", ".yml", ".yaml"):
            if stripped.startswith("#"):
                continue
            if '"""' in line or "'''" in line:
                # An odd number of triple quotes toggles the docstring state.
                if (line.count('"""') + line.count("'''")) % 2 == 1:
                    in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
        elif suffix == ".cs":
            if stripped.startswith(("//", "/*", "*", "///")):
                continue
        yield number, line


def scan_file(path: Path, sensitive_keys: set[str], *, text: str | None = None) -> list[tuple[str, int, str, int, str]]:
    """Return (relpath, lineno, varname, length, prefix) findings."""
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    if text is None:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return []
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        # 仓库外的路径（如 pytest 的 tmp_path）。relative_to 会抛 ValueError，
        # 此前这直接把测试钉死在「必须往 runtime/ 里写真文件」上——而 runtime/
        # 是生产数据区，留在那里的 pytest 日志会让 sensitive_field_scan 报红。
        # 仓库外路径无法判定 docs/tests 归属，按源码处理。
        rel = path.as_posix()
    # Test fixtures and docs are full of deliberately fake credentials; they
    # are expected content, not a leak.  Only the filename gate applies there —
    # plus the provider-endpoint gate, which is the one that was missing.
    endpoint_findings = scan_proxy_endpoints(rel, text)
    if DOC_OR_TEST.search(rel):
        return endpoint_findings

    patterns: list[re.Pattern[str]] = [PAT_CONST_HEX]
    if sensitive_keys:
        names = "|".join(re.escape(k) for k in sorted(sensitive_keys))
        patterns.append(re.compile(PAT_JSON_TEMPLATE.format(names=names), re.I))

    findings: list[tuple[str, int, str, int, str]] = []
    for number, line in iter_scannable_lines(text, path.suffix.lower()):
        # Templates may contain local/documentation endpoints, never an
        # operator's public IP. Do not echo the address in hook diagnostics.
        if ".example." in path.name or path.name.endswith(".example"):
            for match in PAT_EXAMPLE_URL.finditer(line):
                try:
                    address = ipaddress.ip_address(urlsplit(match.group(0)).hostname or "")
                except ValueError:
                    continue
                if address.is_global:
                    findings.append((rel, number, "example-public-ip", 0, ""))
        # Vendor prefixes are checked on the raw line: a token like
        # ROXY_TOKEN = "cfat_..." is not pure hex, so the name-based patterns
        # below would never see it.
        for match in VENDOR_PREFIX.finditer(line):
            findings.append((rel, number, "vendor-token-prefix", len(match.group(0)), match.group(0)[:8]))
        for pattern in patterns:
            for match in pattern.finditer(line):
                name = match.group("name")
                if BENIGN_NAME.search(name):
                    continue
                value = match.group("val")
                if looks_like_secret(value):
                    findings.append((rel, number, name, len(value), value[:3]))
    return endpoint_findings + findings


def staged_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print(f"precommit-guard: git diff failed: {out.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    names = [n for n in out.stdout.split("\0") if n]
    return [ROOT / n for n in names]


def staged_text(path: Path) -> str:
    """Scan the bytes about to be committed, not an unstaged replacement."""
    rel = path.relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f":{rel}"],
        cwd=str(ROOT),
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"cannot read staged file: {rel}")
    return result.stdout.decode("utf-8", errors="replace")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print(f"precommit-guard: git ls-files failed: {out.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    names = [n for n in out.stdout.split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def main(argv: list[str]) -> int:
    check_all = "--all" in argv
    dry = "--dry" in argv

    files = tracked_files() if check_all else staged_files()
    if not files:
        print("precommit-guard: nothing to check")
        return 0

    sensitive_keys = load_sensitive_keys()
    failures: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if name_is_blocked(rel):
            failures.append(f"{rel}: filename is a local/runtime artefact (must stay gitignored)")
            continue
        try:
            content = None if check_all else staged_text(path)
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        findings = scan_file(path, sensitive_keys, text=content)
        if path.suffix.lower() in TEXT_SUFFIXES:
            alias_text = content
            if alias_text is None:
                try:
                    if path.stat().st_size <= MAX_FILE_BYTES:
                        alias_text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    alias_text = None
            if alias_text:
                findings += scan_alias_mailboxes(rel, alias_text)
        for rel_path, lineno, var, length, prefix in findings:
            if var.startswith("proxy-"):
                # The host is public information; the credential never is.
                failures.append(
                    f"{rel_path}:{lineno}: provider-credential-shaped proxy endpoint "
                    f"({var}) -> host {prefix} (credentials omitted)"
                )
                continue
            shown = f"{prefix}... (len={length})" if prefix else f"(value omitted, len={length})"
            failures.append(f"{rel_path}:{lineno}: hardcoded secret in '{var}' -> {shown}")

    # de-duplicate, keep order
    seen: set[str] = set()
    unique = [f for f in failures if not (f in seen or seen.add(f))]

    if not unique:
        mode = "tracked files" if check_all else "staged files"
        print(f"precommit-guard: clean ({len(files)} {mode})")
        return 0

    print("precommit-guard: BLOCKED - refusing to commit", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    for item in unique:
        print(f"  {item}", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print(
        "If a hit is a false positive, fix it properly instead of bypassing:\n"
        "  - move the value to an environment variable, or\n"
        "  - add the file to .gitignore and untrack it (git rm --cached)\n"
        "Emergency bypass: git commit --no-verify  (only you are accountable for it)",
        file=sys.stderr,
    )
    if dry:
        print("precommit-guard: --dry set, not failing", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

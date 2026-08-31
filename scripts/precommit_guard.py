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

import json
import math
import re
import subprocess
import sys
from pathlib import Path

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

# Explicitly allowed even though the name looks dangerous.
ALLOWED_EXACT = {
    "sensitive_policy.json",   # policy definitions, not values
    "config.example.json",     # template with placeholders
    "packages.lock.json",
    "skills-lock.json",
    "payment_methods.json",
    "global.json",
}

ALLOWED_SUFFIXES = (
    ".example.json",
)

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
PAT_JSON_TEMPLATE = (
    r"""["'](?P<name>{names})["']\s*:\s*["'](?P<val>[A-Za-z0-9_\-\.]{{16,}})["']"""
)
# http://user:pass@host
PAT_PROXY = re.compile(r"(?i)\b(?:https?|socks5h?)://[^\s/@]+:[^\s/@]+@")
# Documented placeholders, not real credentials.  The password part is allowed
# to carry a suffix -- config.example.json uses socks5h://user:pass-JP@gate:1000.
PAT_PROXY_PLACEHOLDER = re.compile(r"(?i)://(?:user|username|your[_-]?user|login|account|name|u):[^@]*@")

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

# Directories where a credential-looking string is an example by definition.
DOC_OR_TEST = re.compile(r"(?i)(^|/)(docs?|tests?)/|(\.(md|html|rst))$")


def load_sensitive_keys() -> set[str]:
    """Exact field names declared sensitive in the shared policy."""
    policy_path = ROOT / "sensitive_policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    return {str(k).lower() for k in policy.get("sensitive_keys") or []}

TEXT_SUFFIXES = {
    ".py", ".cs", ".json", ".js", ".ts", ".ps1", ".sh", ".md", ".txt",
    ".yml", ".yaml", ".xml", ".config", ".props", ".ini", ".toml", ".env",
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
    name = Path(rel).name
    if name in ALLOWED_EXACT:
        return False
    if name.endswith(ALLOWED_SUFFIXES):
        return False
    if name in BLOCKED_NAMES:
        return True
    return name.endswith(BLOCKED_SUFFIXES)


def iter_scannable_lines(text: str, suffix: str):
    """Yield (lineno, line), skipping comments and docstrings.

    Docstring examples are the single largest false-positive source here
    (e.g. ``{"totp_secret": "JBSWY3DPEHPK3PXP"}`` in account_2fa.py).
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


def scan_file(path: Path, sensitive_keys: set[str]) -> list[tuple[str, int, str, int, str]]:
    """Return (relpath, lineno, varname, length, prefix) findings."""
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
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
    # are expected content, not a leak.  Only the filename gate applies there.
    if DOC_OR_TEST.search(rel):
        return []

    patterns: list[re.Pattern[str]] = [PAT_CONST_HEX]
    if sensitive_keys:
        names = "|".join(re.escape(k) for k in sorted(sensitive_keys))
        patterns.append(re.compile(PAT_JSON_TEMPLATE.format(names=names), re.I))

    findings: list[tuple[str, int, str, int, str]] = []
    for number, line in iter_scannable_lines(text, path.suffix.lower()):
        # Vendor prefixes are checked on the raw line: a token like
        # ROXY_TOKEN = "cfat_..." is not pure hex, so the name-based patterns
        # below would never see it.
        for match in VENDOR_PREFIX.finditer(line):
            findings.append(
                (rel, number, "vendor-token-prefix", len(match.group(0)), match.group(0)[:8])
            )
        for match in PAT_PROXY.finditer(line):
            url = match.group(0)
            # f-strings that *build* a proxy URL (f"http://{u}:{p}@{host}") are
            # code, not a hardcoded credential.
            if "{" in url or "}" in url:
                continue
            if not PAT_PROXY_PLACEHOLDER.search(url):
                findings.append((rel, number, "proxy-url-with-credentials", 0, ""))
        for pattern in patterns:
            for match in pattern.finditer(line):
                name = match.group("name")
                if BENIGN_NAME.search(name):
                    continue
                value = match.group("val")
                if looks_like_secret(value):
                    findings.append((rel, number, name, len(value), value[:3]))
    return findings


def staged_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if out.returncode != 0:
        print(f"precommit-guard: git diff failed: {out.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    names = [n for n in out.stdout.split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, text=True,
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
        for rel_path, lineno, var, length, prefix in scan_file(path, sensitive_keys):
            shown = f"{prefix}... (len={length})" if prefix else "(credentials in URL)"
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

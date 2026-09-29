"""Ratchet: inline OpenAI/Stripe host literals must not grow.

Why this exists
---------------
Each process boundary has one authority module for the upstream hosts it calls:

* ``services/protocol-payment/common/endpoints.py`` for the payment extractors;
* ``sms_tool/endpoints.py`` for the ``sms_tool`` client.

The services docstring records the pain these exist to end: *"The extractors
previously hard-coded https://chatgpt.com (and friends) in 30+ places ... A host
change meant editing every one."* That is only true once the callers actually
**use** the authority. A one-sided host change on a caller that still inlines the
literal is invisible: the module keeps working and quietly points at the old
origin. ``tests/test_protocol_payment_endpoints.py`` only asserts the *shape* of
the services authority module -- it never checks a caller -- so nothing caught
the ~56 inline literals ideal / twint / blik carried until 2026-09-28.

What it checks
--------------
For every ``.py`` under both scan roots except the authority ``endpoints.py``
itself it counts **managed-host string literals** -- a quote immediately before
one of the managed hosts. A reference such as ``endpoints.CHATGPT_BASE`` has no
quote, and a prose comment *mentions* the host without quoting it, so neither is
counted. The count is frozen per file in ``endpoints_literal_baseline.json`` and
may only go **down**; wiring a caller to its authority is the intended direction.

Two things are deliberately **not** counted:

* the JWT claim key ``https://api.openai.com/auth`` -- a dict key in the access
  token, not a URL to call, so it must never be replaced by ``endpoints.API_BASE``;
* ``_vendor/`` trees -- vendored code is not ours to centralise.

Per-file baselines on purpose: with one grand total, wiring one file would mask
new literals added to another.

The predicate is pinned by ``FIXTURES`` on every invocation: a regex that
over-matches (counts comments or the JWT claim key) or under-matches (misses
f-strings) fails its own fixtures before it is allowed to judge the tree.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "endpoints_literal_baseline.json"

#: Both process boundaries. Each has its own ``endpoints.py`` authority.
SCAN_DIRS: tuple[Path, ...] = (
    Path("services") / "protocol-payment",
    Path("sms_tool"),
)
EXCLUDED_FILES = {"endpoints.py"}
EXCLUDED_DIRS = {"__pycache__", "_vendor"}

#: Hosts owned by one of the two ``endpoints.py`` authorities.
MANAGED_HOSTS: tuple[str, ...] = (
    # services/protocol-payment/common/endpoints.py
    "chatgpt.com",
    "api.stripe.com",
    "checkout.stripe.com",
    "pay.openai.com",
    # sms_tool/endpoints.py
    "auth.openai.com",
    "api.openai.com",
)

#: The JWT claim namespace in the access token. It looks like a URL to the
#: regex but is a dict key; centralising it would be wrong.
NON_URL_LITERALS = (
    '"https://api.openai.com/auth"',
    "'https://api.openai.com/auth'",
)

_HOST_ALT = "|".join(re.escape(host) for host in MANAGED_HOSTS)
# A quote (optionally the closing quote of an f-string prefix) immediately
# before the scheme is what makes it a *literal* rather than prose or a
# reference.
LITERAL_RE = re.compile(rf"""['"](https://(?:{_HOST_ALT}))""")

#: (name, source snippet, expected count). Both directions.
FIXTURES: tuple[tuple[str, str, int], ...] = (
    ("plain literal", 'x = "https://chatgpt.com/checkout/a/b"\n', 1),
    ("f-string literal", 'x = f"https://api.stripe.com/v1/payment_pages/{cs}/init"\n', 1),
    ("auth host literal", 'x = "https://auth.openai.com/email-verification"\n', 1),
    ("authority reference", "x = endpoints.CHATGPT_BASE\n", 0),
    ("interpolated reference", 'x = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs}"\n', 0),
    ("prose comment", "# see https://chatgpt.com/checkout for the flow\n", 0),
    ("unmanaged host", 'x = "https://www.cloudflare.com/cdn-cgi/trace"\n', 0),
    ("jwt claim key", 'x = claims.get("https://api.openai.com/auth")\n', 0),
    ("jwt claim key single-quoted", "x = claims['https://api.openai.com/auth']\n", 0),
    ("real api path is still counted", 'x = "https://api.openai.com/profile"\n', 1),
    ("two on one line", 'a = "https://chatgpt.com"; b = "https://pay.openai.com/c/pay/"\n', 2),
)


def strip_non_url_literals(source: str) -> str:
    """Remove literal forms that look like hosts but are not URLs to call."""
    for literal in NON_URL_LITERALS:
        source = source.replace(literal, "")
    return source


def count_literals(source: str) -> int:
    """Managed-host string literals in one module source."""
    return len(LITERAL_RE.findall(strip_non_url_literals(source)))


def predicates_are_trustworthy() -> str | None:
    """Return a failure description if the predicate fails its own fixtures."""
    problems: list[str] = []
    for name, source, expected in FIXTURES:
        actual = count_literals(source)
        if actual != expected:
            problems.append(f"{name}: expected {expected}, got {actual}")
    # A guard that over-matches everything is as useless as one that matches
    # nothing: a plain literal must still score.
    if count_literals('X = "https://chatgpt.com"\n') == 0:
        problems.append("predicate missed a plain literal")
    return "; ".join(problems) if problems else None


def collect(root: Path = ROOT) -> dict[str, int]:
    """{repo-relative file: managed-host literal count}, only non-zero files."""
    counts: dict[str, int] = {}
    for scan_dir in SCAN_DIRS:
        scan_root = root / scan_dir
        if not scan_root.is_dir():
            continue
        for path in sorted(scan_root.rglob("*.py")):
            if EXCLUDED_DIRS & set(path.parts) or path.name in EXCLUDED_FILES:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            count = count_literals(source)
            if count:
                counts[path.relative_to(root).as_posix()] = count
    return counts


def load_baseline(path: Path | None = None) -> dict[str, int]:
    if path is None:
        path = BASELINE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        # A corrupt baseline is treated as empty: every current count is then
        # "grown" against an allowance of 0, so the ratchet fails loudly
        # instead of silently passing with no baseline.
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in files.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ratchet on inline OpenAI/Stripe host literals.")
    parser.add_argument("--detail", action="store_true", help="print the per-file table")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after WIRING a caller, i.e. lowering it)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    broken = predicates_are_trustworthy()
    if broken is not None:
        print(f"SELF-TEST FAILED: {broken}", file=sys.stderr)
        print("endpoints-literal-ratchet: refusing to report with a broken predicate.", file=sys.stderr)
        return 1

    counts = collect(args.root)

    if args.detail:
        for path in sorted(counts):
            print(f"{counts[path]:3d}  {path}")
        print(f"total: {sum(counts.values())}")

    if args.update_baseline:
        previous = load_baseline(BASELINE)
        payload = {"files": {k: counts[k] for k in sorted(counts)}}
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {BASELINE.name}")
        for path in sorted(set(previous) | set(counts)):
            before = previous.get(path, 0)
            after = counts.get(path, 0)
            delta = after - before
            flag = " " if delta == 0 else ("+" if delta > 0 else "!")
            print(f"  {flag} {path:<58} {before:3d} -> {after:3d} ({delta:+d})")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    grown: list[tuple[str, int, int]] = []
    for path in sorted(set(counts) | set(baseline)):
        allowed = baseline.get(path, 0)
        current = counts.get(path, 0)
        if current > allowed:
            grown.append((path, current, allowed))

    if grown:
        for path, current, allowed in grown:
            print(
                f"{path}: {current} inline managed-host literal(s) > baseline {allowed} "
                f"(+{current - allowed}). Import the host from the matching endpoints.py "
                f"authority (services/protocol-payment/common/ or sms_tool/) instead of "
                f"inlining it, or justify a baseline bump.",
                file=sys.stderr,
            )
        return 1

    total = sum(counts.values())
    print(f"endpoints-literal ratchet OK ({total} inline literal(s) across {len(counts)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

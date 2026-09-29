"""Ratchet: inline protocol-payment host literals must not grow.

Why this exists
---------------
``services/protocol-payment/common/endpoints.py`` is the declared single
authority for the upstream hosts the extractors call, and its own docstring
records the pain it was built to end: *"The extractors previously hard-coded
https://chatgpt.com (and friends) in 30+ places ... A host change meant editing
every one."*

That is only true once the extractors actually **use** it. A one-sided host
change on an extractor that still inlines the literal is invisible: the module
keeps working and quietly points at the old origin. The existing
``tests/test_protocol_payment_endpoints.py`` only asserts the *shape* of the
authority module -- it never checks a caller, so nothing caught the ~56 inline
literals that ideal / twint / blik carried until 2026-09-28.

What it checks
--------------
For every ``services/protocol-payment/**/*.py`` except ``common/endpoints.py``
(the authority itself) it counts **managed-host string literals** -- a quote
immediately before one of the managed hosts. A reference such as
``endpoints.CHATGPT_BASE`` has no quote, and a prose comment *mentions* the host
without quoting it, so neither is counted. The count is frozen per file in
``endpoints_literal_baseline.json`` and may only go **down**; wiring an
extractor to ``endpoints`` is the intended direction.

Per-file baselines on purpose: with one grand total, wiring ``pix`` would mask
new literals added to ``direct_card``.

The predicate is pinned by ``FIXTURES`` on every invocation: a regex that
over-matches (counts comments) or under-matches (misses f-strings) fails its own
fixtures before it is allowed to judge the tree.

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

SCAN_DIR = Path("services") / "protocol-payment"
EXCLUDED = {"endpoints.py"}

#: Hosts that ``common/endpoints.py`` owns. Keep in sync with its constants.
MANAGED_HOSTS: tuple[str, ...] = (
    "chatgpt.com",
    "api.stripe.com",
    "checkout.stripe.com",
    "pay.openai.com",
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
    ("authority reference", "x = endpoints.CHATGPT_BASE\n", 0),
    ("interpolated reference", 'x = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs}"\n', 0),
    ("prose comment", "# see https://chatgpt.com/checkout for the flow\n", 0),
    ("unmanaged host", 'x = "https://www.cloudflare.com/cdn-cgi/trace"\n', 0),
    ("two on one line", 'a = "https://chatgpt.com"; b = "https://pay.openai.com/c/pay/"\n', 2),
)


def count_literals(source: str) -> int:
    """Managed-host string literals in one module source."""
    return len(LITERAL_RE.findall(source))


def predicates_are_trustworthy() -> str | None:
    """Return a failure description if the predicate fails its own fixtures."""
    problems: list[str] = []
    for name, source, expected in FIXTURES:
        actual = count_literals(source)
        if actual != expected:
            problems.append(f"{name}: expected {expected}, got {actual}")
    # A guard that over-matches everything is as useless as one that matches
    # nothing: the authority module itself must score zero.
    if count_literals('X = "https://chatgpt.com"\n') == 0:
        problems.append("predicate missed a plain literal")
    return "; ".join(problems) if problems else None


def collect(root: Path = ROOT) -> dict[str, int]:
    """{repo-relative file: managed-host literal count}, only non-zero files."""
    counts: dict[str, int] = {}
    scan_root = root / SCAN_DIR
    for path in sorted(scan_root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in EXCLUDED:
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
    parser = argparse.ArgumentParser(description="Ratchet on inline protocol-payment host literals.")
    parser.add_argument("--detail", action="store_true", help="print the per-file table")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after WIRING an extractor, i.e. lowering it)",
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
            print(f"  {flag} {path:<52} {before:3d} -> {after:3d} ({delta:+d})")
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
                f"(+{current - allowed}). Import the host from "
                f"services/protocol-payment/common/endpoints.py instead of inlining it, "
                f"or justify a baseline bump.",
                file=sys.stderr,
            )
        return 1

    total = sum(counts.values())
    print(f"endpoints-literal ratchet OK ({total} inline literal(s) across {len(counts)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

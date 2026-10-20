"""Ratchet: the legacy payment directories may not gain files.

Why this exists
---------------
A payment method can currently be implemented in **three** places, and only one
of them is the registry:

* ``sms_tool/pay_link/`` -- the ``PaymentMethodSpec`` registry and the shared
  extraction state machine. This is where a new method belongs.
* ``sms_tool/paypal_link/`` -- the PayPal-only Checkout/merchant-return lane.
* ``sms_tool/upi_link/`` -- the UPI custom-Checkout lane (20 modules).

``docs/architecture.md`` already says ``pay_link/`` is where methods live, but
nothing enforced it: the two legacy lanes are still the path of least
resistance, because a new method can be added there without touching the
registry, the catalog or the desktop method picker. That is how a second and
third state machine grew in the first place.

What this gate does
-------------------
Freezes the **file set** of the two legacy directories at
``scripts/payment_legacy_manifest.json``:

* a new file under either directory fails the gate;
* a removed file is accepted (that is the direction of travel) and the message
  asks for the baseline refresh so the removal is recorded;
* moving a file between the two directories counts as one removal plus one
  addition, so it fails -- a rename is not progress.

It deliberately does **not** freeze file contents: the legacy lanes still get
bug fixes. Only their growth is forbidden.

Why a file manifest and not a grep
----------------------------------
The rule is about *where implementation lives*, not about which symbol a file
calls. A grep for ``PaymentMethodSpec`` would pass a new 2,000-line extractor
that simply never registers anything, which is exactly the failure mode this
gate is written against.

Usage:
    python scripts/payment_legacy_manifest.py               # check
    python scripts/payment_legacy_manifest.py --detail      # per-directory listing
    python scripts/payment_legacy_manifest.py --update-baseline

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "payment_legacy_manifest.json"

#: Directories whose file set is frozen. ``pay_link/`` is intentionally absent:
#: it is the registry and must stay open for new methods.
LEGACY_DIRS: tuple[str, ...] = ("sms_tool/paypal_link", "sms_tool/upi_link")

#: Vendored trees inside a legacy directory are not owned by this repository's
#: authors; they move as a unit with the vendor drop.
SKIP_DIR_NAMES = frozenset({"__pycache__", "_vendor"})


def manifest(root: Path = ROOT) -> dict[str, list[str]]:
    """Return ``{directory: sorted file names}`` for every frozen directory."""
    found: dict[str, list[str]] = {}
    for relative in LEGACY_DIRS:
        base = root / relative
        if not base.is_dir():
            raise RuntimeError(f"frozen directory is missing: {relative} (was it renamed?)")
        names: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if SKIP_DIR_NAMES & set(path.parts):
                continue
            names.append(path.relative_to(base).as_posix())
        found[relative] = names
    return found


def load_baseline(path: Path = BASELINE) -> dict[str, list[str]]:
    """Load the frozen manifest, treating a corrupt file as empty (fail loudly)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            out[str(key)] = [str(item) for item in value]
    return out


def additions(current: dict[str, list[str]], allowed: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Return ``(directory, file)`` for every file that is not in the baseline."""
    out: list[tuple[str, str]] = []
    for directory, names in sorted(current.items()):
        known = set(allowed.get(directory, []))
        for name in names:
            if name not in known:
                out.append((directory, name))
    return out


def removals(current: dict[str, list[str]], allowed: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Return ``(directory, file)`` for every baseline file that is gone."""
    out: list[tuple[str, str]] = []
    for directory, names in sorted(current.items()):
        present = set(names)
        for name in allowed.get(directory, []):
            if name not in present:
                out.append((directory, name))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--detail", action="store_true", help="print the per-directory file listing")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record the current file set (only after REMOVING files, never after adding them)",
    )
    args = parser.parse_args(argv)

    try:
        current = manifest()
    except RuntimeError as exc:
        print(f"payment-legacy-manifest: {exc}", file=sys.stderr)
        return 2

    if args.detail:
        for directory, names in sorted(current.items()):
            print(f"{directory} ({len(names)} file(s))")
            for name in names:
                print(f"  {name}")

    if args.update_baseline:
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
        previous = load_baseline()
        print(f"baseline updated: {BASELINE.name}")
        for directory, name in removals(current, previous):
            print(f"  removed: {directory}/{name}")
        for directory, name in additions(current, previous):
            print(f"  recorded (was new): {directory}/{name}")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/payment_legacy_manifest.py --update-baseline", file=sys.stderr)
        return 2

    allowed = load_baseline()
    new_files = additions(current, allowed)
    gone = removals(current, allowed)

    if new_files:
        for directory, name in new_files:
            print(
                f"{directory}/{name}: new file in a frozen legacy payment directory. "
                "Implement the method in sms_tool/pay_link/ and register it; the legacy lanes "
                "are closed to new implementation because a method that lives there bypasses "
                "the registry, the payment catalog and the desktop method picker.",
                file=sys.stderr,
            )
        return 1

    if gone:
        print(
            "payment-legacy-manifest: files were removed from a frozen directory; record it with "
            "--update-baseline so the removal is reviewable",
            file=sys.stderr,
        )
        return 1

    total = sum(len(names) for names in current.values())
    print(f"payment-legacy manifest OK ({total} frozen file(s) over {len(LEGACY_DIRS)} legacy director(ies))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

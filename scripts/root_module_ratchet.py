"""Ratchet: the number of top-level ``sms_tool`` modules must not grow.

Why this exists
---------------
``sms_tool/`` had **155** top-level ``.py`` modules on 2026-10-20, up from 111 a
few hundred commits earlier. The coupling gates (``import_layer_ratchet``,
``provider_decoupling_ratchet``, the hub ratchets) all measure *edges*; none of
them noticed that the package root had become the default place to put a new
module, because "one more file" adds no edge on day one -- it only adds a place
where the next edge can be born, and one more row that ``directory-map.md`` has
to claim.

So this gate freezes the **exact set of root module names**, not a count:

* a new name at the root fails, with the domain subpackage to use instead;
* deleting a root module passes (that is the direction this ratchet wants);
* moving a root module into ``accounts/`` / ``auth_flow/`` / ``commands/`` /
  ``cli_parsers/`` / ``geo/`` / ``kernel/`` / ``pay_link/`` / ``paypal/`` /
  ``paypal_link/`` / ``providers/`` / ``registration_drivers/`` / ``sentinel/`` /
  ``store/`` / ``upi_link/`` registers as a removal -- allowed, and recorded by
  ``--update-baseline``.

Names, not a count, because a count lets a rename through and then the ratchet
can never say *which* module appeared. ``--update-baseline`` is only for removing
entries (after a move or a deletion); adding one needs a decision in
``docs/architecture.md`` first.

Standard library only: it runs as a pytest test and as a CI step.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sms_tool"
BASELINE = Path(__file__).resolve().parent / "root_module_baseline.json"


def collect_root_modules(root: Path = ROOT) -> set[str]:
    """Tracked ``sms_tool/<name>.py`` files (the package root only)."""
    raw = subprocess.check_output(["git", "ls-files", "--", "sms_tool"], cwd=str(root))
    names: set[str] = set()
    for entry in raw.decode("utf-8").splitlines():
        if not entry.endswith(".py") or not entry.startswith("sms_tool/"):
            continue
        remainder = entry[len("sms_tool/") :]
        if "/" in remainder:
            continue
        names.add(remainder)
    return names


def domain_subpackages(root: Path = PACKAGE) -> list[str]:
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file() and not path.name.startswith("__")
    )


def load_baseline(path: Path | None = None) -> dict:
    # Resolve at call time: a default argument would bind the real path once and
    # ignore a monkeypatched BASELINE (the same trap the repo documents for
    # file_loading). Tests redirect the baseline through the module global.
    return json.loads((path or BASELINE).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze the set of top-level sms_tool modules."
    )
    parser.add_argument("--update-baseline", action="store_true", help="record removals (never additions)")
    parser.add_argument("--detail", action="store_true", help="print the frozen set")
    args = parser.parse_args(argv)

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    current = collect_root_modules()
    baseline = load_baseline()
    frozen = set(baseline.get("modules") or [])

    added = sorted(current - frozen)
    removed = sorted(frozen - current)

    if args.update_baseline:
        if added:
            print(
                "refusing to add new root modules to the baseline: "
                + ", ".join(added)
                + ". Put them in a domain subpackage "
                + "(" + ", ".join(domain_subpackages()) + ") instead.",
                file=sys.stderr,
            )
            return 1
        BASELINE.write_text(
            json.dumps({"modules": sorted(current)}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"baseline updated: {len(frozen)} -> {len(current)} root module(s)")
        return 0

    if args.detail:
        for name in sorted(current):
            print(("  frozen  " if name in frozen else "  NEW     ") + name)

    if added:
        print(
            "root-module ratchet: new top-level module(s) in sms_tool/: "
            + ", ".join(added)
            + "\nThe package root is frozen at "
            + f"{len(frozen)} modules ({len(current)} today). Put a new module in the domain "
            + "subpackage it belongs to ("
            + ", ".join(domain_subpackages())
            + "); if it genuinely has no home, add it to "
            + "scripts/root_module_baseline.json with a reason recorded in docs/architecture.md.",
            file=sys.stderr,
        )
        return 1

    print(
        f"root-module ratchet OK ({len(current)} frozen, "
        f"{len(removed)} removal(s) pending baseline trim{': ' + ', '.join(removed) if removed else ''})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Check live documentation pointers and current module layout."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    releases = sorted((ROOT / "docs").glob("release-v*.md"), key=lambda p: p.name)
    if not releases:
        print("No release notes found")
        return 1
    latest = releases[-1].name
    failures: list[str] = []
    for path in (ROOT / "README.md", ROOT / "README_EN.md", ROOT / "docs" / "README.md"):
        text = path.read_text(encoding="utf-8")
        if latest not in text:
            failures.append(f"{path.relative_to(ROOT)} does not point to {latest}")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    if "Half-migrated" in architecture or "半迁移" in architecture:
        failures.append("architecture.md still describes providers as half-migrated")
    for path in (ROOT / "docs" / "directory-map.md", ROOT / "docs" / "architecture.md"):
        for ref in re.findall(r"`(sms_tool/providers/[^`]+\.py)`", path.read_text(encoding="utf-8")):
            if not (ROOT / ref).is_file():
                failures.append(f"missing documented path: {ref}")
    if failures:
        print("Documentation consistency check failed")
        print("\n".join(failures))
        return 1
    print(f"Documentation consistency check passed ({latest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

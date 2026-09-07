"""Read-only candidate and cleanup inventory. Never emits mailbox identities."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def candidate_counts(path: Path) -> dict:
    """Count structural candidates, not credential validity or unused accounts."""
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    if not path.is_file():
        return {"available": False, "providers": {}}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.lower().startswith(("gmail://", "remail://")):
            provider, payload = value.split("://", 1)
            separator = "----" if "----" in payload else "---"
            parts = payload.split(separator)
            identity = parts[0].strip().lower()
            if "@" in identity and len(parts) >= 2 and parts[1].strip() and identity not in seen:
                counts[provider.lower()] += 1
                seen.add(identity)
    return {
        "available": True,
        "providers": dict(sorted(counts.items())),
        "credentials_verified": False,
        "registration_history_checked": False,
    }


def cleanup_inventory(runtime: Path) -> dict:
    """Return category totals only; no filenames, deletion or recursive traversal."""
    groups: dict[str, dict[str, int]] = {}
    for path in runtime.iterdir() if runtime.is_dir() else ():
        if not path.is_file():
            continue
        name = path.name.lower()
        if path.suffix == ".py" or name.startswith("venv_install") or (
            path.suffix == ".log" and name.startswith(("camoufox_", "cloak_", "_"))
        ):
            group = "review_debug_artifacts"
        elif path.suffix in {".sqlite3", ".db"} or "backup" in name or ".pre_" in name:
            group = "preserve_database_and_backups"
        elif path.suffix in {".txt", ".json", ".jsonl"}:
            group = "preserve_state_credentials_and_evidence"
        else:
            group = "unclassified_preserve"
        bucket = groups.setdefault(group, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += path.stat().st_size
    return {"mode": "report_only", "categories": groups, "files_modified": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps({
        "candidates": candidate_counts(args.root / "mailbox_tokens.txt"),
        "cleanup": cleanup_inventory(args.root / "runtime"),
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()

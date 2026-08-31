"""Release payload gate — run this AFTER staging the installer payload, BEFORE signing.

Why this exists
---------------
On 2026-08-31 the v2026.08.31 assets were published carrying
`scripts/pick_final_replacements.py` (a local diagnostic script that had been
git-ignored and deleted from git, but was still on disk at build time) inside both
the zip and the setup exe. The git history had been purged, the release assets
had not — two independent leak channels.

The rule that would have caught it: **anything .gitignore rejects must not ship.**
`git ls-files` collects the payload, but the build runs against the working tree,
so ignored-but-on-disk files can slip in.

Checks
------
1. Payload file paths that are git-ignored (source-ish extensions only; .dll/.exe
   build output is legitimately ignored and out of scope).
2. Credential regexes over payload text files (reuses sensitive_field_scan).
3. Known-bad name patterns from past incidents (`scripts/_*.py`, `pick_final*`).

Usage
-----
    python scripts/scan_release_payload.py <payload_dir> [<payload_dir> ...]
    python scripts/scan_release_payload.py dist/installer/package

Exit code 0 = clean, 1 = blocked.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `_foo.py` scratch scripts are git-ignored and must never ship — but `__init__.py`
# and `__main__.py` are dunder modules, not scratch files. Excluding them is the
# single most important false-positive filter here.
_SCRATCH_SCRIPT = re.compile(r"(^|[\\/])_(?!_)[^\\/]*\.py$")

# Only these are worth asking git about. Build output (.dll/.exe/.pyd) is ignored
# on purpose and would produce nothing but false positives.
SOURCE_SUFFIXES = {
    ".py", ".pyw", ".cs", ".xaml", ".json", ".md", ".txt", ".ps1", ".bat",
    ".cmd", ".sh", ".yml", ".yaml", ".js", ".ts", ".ini", ".cfg", ".toml",
}

# Directories whose contents are build output, not source. They are git-ignored by
# design and shipping them is the whole point of the payload.
BUILD_OUTPUT_PREFIXES = ("dist/", "scripts/installer/bin/", "scripts/installer/obj/")

# Past incidents, kept explicit so the failure message names the reason.
BAD_NAME_PATTERNS = (
    (_SCRATCH_SCRIPT, "underscore-prefixed local scratch script"),
    (re.compile(r"pick_final|final_replacement", re.I), "credential-purge helper script"),
    (re.compile(r"_diag_", re.I), "local diagnostic script"),
)


def _git(*args: str, stdin_data: str | None = None) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout


def check_ignored(payload: Path, rel_paths: list[str]) -> list[str]:
    """Batch-ask git which payload paths are ignored by .gitignore."""
    if not rel_paths:
        return []
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin", "-z"],
        cwd=str(ROOT),
        input="\0".join(rel_paths),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # check-ignore exits 1 when nothing matched — that is the good case.
    if proc.returncode not in (0, 1):
        return [f"git check-ignore failed (rc={proc.returncode}): {proc.stderr.strip()}"]
    ignored = [p for p in proc.stdout.split("\0") if p]
    return [
        f"{p} is git-ignored but present in the payload "
        f"(ignored files must never ship)"
        for p in ignored
    ]


def check_names(rel_path: str) -> list[str]:
    out = []
    for pattern, reason in BAD_NAME_PATTERNS:
        if pattern.search(rel_path):
            out.append(f"{rel_path} matches a known-bad name pattern: {reason}")
    return out


def check_artifact_regexes(payload: Path) -> list[str]:
    """Reuse the credential regexes that CI already trusts.

    Deliberately limited to *artifact* extensions. Running these over .py/.cs/.md
    flags ordinary source such as `access_token = example` in docs and sample code
    — that is what scan_hardcoded_secrets and the pre-commit guard are for, and
    mixing the two turns this gate into noise that people learn to bypass.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import sensitive_field_scan as sfs
    except ImportError as exc:
        return [f"cannot import sensitive_field_scan: {exc}"]

    failures: list[str] = []
    text_re = (
        (sfs.ARTIFACT_SECRET, "sensitive artifact value"),
        (sfs.ARTIFACT_CARD_FRAGMENT, "card fragment artifact"),
    )
    for path in payload.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".json", ".jsonl", ".log", ".txt",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, label in text_re:
                if pattern.search(line):
                    rel = path.relative_to(payload).as_posix()
                    failures.append(f"{rel}:{number}: {label}")
    return failures


def main(argv: list[str]) -> int:
    targets = [a for a in argv if not a.startswith("-")]
    if not targets:
        print(__doc__.strip().split("Usage")[-1], file=sys.stderr)
        return 2

    failures: list[str] = []
    for raw in targets:
        payload = Path(raw)
        if not payload.is_absolute():
            payload = ROOT / payload
        if not payload.is_dir():
            failures.append(f"payload directory does not exist: {payload}")
            continue

        rel_paths: list[str] = []
        for path in payload.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(payload).as_posix()
            if rel.startswith(BUILD_OUTPUT_PREFIXES):
                continue
            if path.suffix.lower() in SOURCE_SUFFIXES:
                rel_paths.append(rel)
            failures.extend(check_names(rel))

        print(f"[{payload.name}] {len(rel_paths)} source-ish files to verify")
        failures.extend(check_ignored(payload, rel_paths))
        failures.extend(check_artifact_regexes(payload))

    if failures:
        print("\nRELEASE PAYLOAD BLOCKED", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nFix: remove the offending files, then re-run the payload staging step."
            "\nDo not bypass this gate — the release channel is the one exit with no"
            " other defence.",
            file=sys.stderr,
        )
        return 1

    print("release payload scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

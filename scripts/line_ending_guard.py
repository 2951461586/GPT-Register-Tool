"""Gate: refuse a commit that introduces mixed line endings in a text file.

Why this exists
---------------
The repo had no ``.gitattributes`` until 2026-09-17, so line endings were
decided implicitly by ``core.autocrlf=true``: index LF, worktree CRLF.  Any
tool or hand-edit that writes a bare ``\\n`` into a CRLF file produces a
file that is **mixed internally**.  Measured on 2026-09-17: 18 of 499 ``.py``
files were mixed; ``tests/test_remail_mailbox.py`` had 136 lone LFs among 716
lines.

Mixed files are not merely untidy:

* ``git diff`` marks the whole file changed, hiding the real edit;
* ``git blame`` attribution is destroyed (a line-ending change counts as an edit);
* byte-exact reconciliation -- which this repo leans on heavily
  (``git show HEAD:path`` + SHA256 to attribute a change to a baseline) --
  reports a difference between two files that are semantically identical.

So this gate blocks new mixed files at commit time.  It does **not** demand
CRLF or LF -- either is fine, per file.  The rule is only: pick one per file.

Rationale for "per file, not per repo": ``.gitattributes`` already pins what
git stores (LF) and what it writes on checkout.  This gate catches the case
git cannot see until it is too late -- content that arrives mixed from an
editor or a generated artefact.

Which files are scanned
-----------------------
All *text* files, decided by suffix first and by content sniff second.  The
sniff was added on 2026-09-17 after a live miss: the guard reported a clean
tree while ``services/protocol-payment/LICENSE`` held 21 CRLF + 11 lone LF,
because ``Path("LICENSE").suffix == ""`` never matched the suffix whitelist
and ``inspect`` therefore never read the file at all.  A gate whose scope
predicate can silently exclude the offender is decorative, so scope is now
evidence-based rather than declaration-based.

Standard library only: the hook may be run by any interpreter on PATH.

Usage:
    python scripts/line_ending_guard.py            # check staged files
    python scripts/line_ending_guard.py --all      # check every tracked file
    python scripts/line_ending_guard.py --all --dry  # report only, never fail
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Only extensions git treats as text in this repo.  Deliberately explicit
# rather than "anything not binary": a new binary format must not be able to
# trip this gate by accident.
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".md", ".txt", ".json", ".jsonl", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".sh", ".cs", ".xaml", ".csproj", ".props",
    ".slnx", ".manifest", ".js", ".example", ".ps1", ".bat", ".cmd",
})

EXEMPT_NAMES = frozenset({
    # No suffix, so they never reach the extension test; listed for clarity.
    ".gitignore", ".gitattributes", ".editorconfig",
})

# Extensionless files that ARE text and must be scanned.  Before 2026-09-17
# the suffix whitelist was the only entry point, so every extensionless file
# was silently skipped -- `services/protocol-payment/LICENSE` sat there with
# 21 CRLF + 11 lone LF and the guard reported a clean tree.  That is the exact
# "decorative gate" failure the classifier fixtures exist to prevent, so the
# blind spot is closed twice over: by this list AND by the content sniff below.
EXTENSIONLESS_TEXT_NAMES = frozenset({
    "LICENSE", "LICENCE", "NOTICE", "COPYING", "AUTHORS", "CONTRIBUTORS",
    "CHANGELOG", "CHANGES", "INSTALL", "README", "TODO", "VERSION",
    "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
    "Gemfile", "Rakefile", "Procfile", "Brewfile",
    # Directory-qualified, because the basename alone is not enough to know it
    # is text (a hook script is, but so is a binary with a bare name).
    ".githooks/pre-commit", ".githooks/commit-msg", ".githooks/post-checkout",
})

# How much of a file to sniff before deciding "this is text".  Small enough to
# stay fast over ~800 files, large enough to clear any binary magic number.
SNIFF_BYTES = 8192

# Files that are knowingly still mixed.  Each entry is a debt with a reason,
# not a permission: `tests/test_line_ending_guard.py` asserts every name here is
# *still* mixed, so fixing one forces the entry to be dropped.
#
# **Empty as of 2026-09-17.**  The five entries that lived here carried
# uncommitted work from another task, so normalising them the first time round
# would have silently rewritten a colleague's in-flight change.  They were
# cleaned once the whole working tree was measured (see the note on the
# CRLF-materialisation trap below), and the ratchet is now at zero: any mixed
# file at all fails the guard.
#
# Why a whole-worktree sweep was needed to find them
# --------------------------------------------------
# `.gitattributes` (`* text eol=lf`) makes git's smudge filter write LF on
# checkout, so `git checkout -- <path>` does NOT materialise CRLF even though
# `core.autocrlf=true`.  A file still went CRLF-only on disk, and `git diff`
# stays silent about it -- because the **index** entry is `i/lf` and git
# compares index-to-index after normalisation.  The pollution is therefore
# invisible to `git status` and to `git diff --check`, and only a byte-level
# census of the working tree finds it.  `runtime/tmp/p1_3_line_ending_census.py`
# is that census.
MIXED_EXEMPT: dict[str, str] = {}

CRLF = b"\r\n"
LF = b"\n"

# Fixtures pinning the classifier.  These exist because of a concrete failure
# mode found on 2026-09-17: `classify` is a single point of failure, and when it
# was deliberately broken (`mixed` always returning `lf`) the guard scanned 786
# files -- including a knowingly-mixed probe -- and reported success.  A
# predicate that can be silently gutted makes the whole gate decorative.
#
# These fixtures are checked on every run, in every mode, before any scanning,
# so breaking the classifier fails loudly even on a tree with no mixed files.
CLASSIFIER_FIXTURES: tuple[tuple[bytes, str], ...] = (
    (b"", "none"),
    (b"a", "none"),
    (b"a\r\nb\r\n", "crlf"),
    (b"a\nb\n", "lf"),
    # The case the guard exists for: one bare LF inside a CRLF file.
    (b"a\r\nb\r\nc\nd\r\n", "mixed"),
    (b"a\r\nb\n", "mixed"),
    # Lone CR is neither ending; must not be reported as mixed.
    (b"a\rb\r", "none"),
)
# A file with this content MUST be reported as mixed.  Used as a positive
# control at runtime: if a scan finds no offenders anywhere, this proves the
# scanner would have found one had it been present.
POSITIVE_CONTROL = b"control\r\nline\nend\r\n"


def classify(raw: bytes) -> str:
    """'lf' | 'crlf' | 'mixed' | 'none' for a byte blob."""
    crlf = raw.count(CRLF)
    total_lf = raw.count(LF)
    if total_lf == 0:
        return "none"
    if crlf == 0:
        return "lf"
    if crlf == total_lf:
        return "crlf"
    return "mixed"


def classifier_is_trustworthy() -> str | None:
    """None if the classifier passes its fixtures, else a description of failure.

    Guards against the 2026-09-17 failure mode: the predicate being silently
    weakened so that the gate always reports success.
    """
    for raw, expected in CLASSIFIER_FIXTURES:
        actual = classify(raw)
        if actual != expected:
            return f"classify({raw!r}) -> {actual!r}, expected {expected!r}"
    if classify(POSITIVE_CONTROL) != "mixed":
        return "positive control is not classified as mixed"
    return None


def _is_text_candidate(path: Path, raw: bytes | None = None, rel: str | None = None) -> bool:
    """Is this file worth classifying for line endings?

    Fast path: a known text suffix, or a known extensionless text name.
    Slow path: sniff the leading bytes.  The sniff exists because the fast
    path alone was blind to extensionless files -- see
    EXTENSIONLESS_TEXT_NAMES for the concrete miss.

    ``raw`` lets a caller that already read the file avoid a second read.
    """
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in EXEMPT_NAMES:
        return True
    if path.name in EXTENSIONLESS_TEXT_NAMES:
        return True
    if rel is not None and rel.replace("\\", "/") in EXTENSIONLESS_TEXT_NAMES:
        return True

    if raw is None:
        try:
            raw = path.read_bytes()[:SNIFF_BYTES]
        except OSError:
            return False
    head = raw[:SNIFF_BYTES]
    if not head:
        return False
    # A NUL byte in the first block is git's own binary heuristic.
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True)


def staged_names() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [n for n in out.stdout.decode("utf-8", "replace").splitlines() if n.strip()]


def tracked_names() -> list[str]:
    out = _git("ls-files")
    return [n for n in out.stdout.decode("utf-8", "replace").splitlines() if n.strip()]


def untracked_names() -> list[str]:
    """Untracked-but-not-ignored files.

    ``--all`` must include these: a brand-new file is exactly where a mixed
    line ending is most likely to appear (an editor or a generator writes it
    with no repository convention to follow yet).  Scanning only ``ls-files``
    made ``--all`` structurally blind to the case the guard exists for --
    caught by ``runtime/tmp/p1_eol_guard_mutation.py`` step 2.
    """
    out = _git("ls-files", "--others", "--exclude-standard")
    return [n for n in out.stdout.decode("utf-8", "replace").splitlines() if n.strip()]


def all_scan_names() -> list[str]:
    return sorted(set(tracked_names()) | set(untracked_names()))


def staged_blob(rel: str) -> bytes | None:
    """The bytes git would actually store -- not what is on disk.

    Reading the staged blob rather than the worktree file matters: with
    ``core.autocrlf=true`` the two differ, and it is the staged blob that
    becomes history.
    """
    out = _git("show", f":{rel}")
    return out.stdout if out.returncode == 0 else None


def worktree_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def inspect(rel: str, *, staged: bool) -> tuple[str, str] | None:
    """(kind, detail) for a file that is mixed, else None."""
    if rel in MIXED_EXEMPT:
        return None

    path = ROOT / rel

    raw = staged_blob(rel) if staged else worktree_bytes(path)
    if raw is None:
        return None

    # Sniff with the bytes we already have: the suffix whitelist is a fast
    # path only, not the sole entry point (see _is_text_candidate).
    if not _is_text_candidate(path, raw, rel):
        return None

    kind = classify(raw)
    if kind != "mixed":
        return None

    crlf = raw.count(CRLF)
    total_lf = raw.count(LF)
    where = "staged blob" if staged else "worktree"
    detail = f"{where}: {crlf} CRLF + {total_lf - crlf} lone LF of {total_lf} lines"
    return kind, detail


def main(argv: list[str]) -> int:
    check_all = "--all" in argv
    dry = "--dry" in argv

    # Self-test first: a gutted classifier must fail loudly rather than
    # silently reporting a clean tree.  See CLASSIFIER_FIXTURES.
    broken = classifier_is_trustworthy()
    if broken is not None:
        print(
            "line-ending-guard: SELF-TEST FAILED -- the classifier is not "
            f"behaving as specified: {broken}\n"
            "This gate cannot be trusted until that is fixed.  Do NOT bypass it "
            "with --no-verify; fix classify().",
            file=sys.stderr,
        )
        return 1

    names = all_scan_names() if check_all else staged_names()
    offenders: list[tuple[str, str]] = []
    for rel in names:
        found = inspect(rel, staged=not check_all)
        if found is not None:
            offenders.append((rel, found[1]))

    if not offenders:
        scope = "tracked" if check_all else "staged"
        print(f"line-ending-guard: no mixed line endings among {len(names)} {scope} file(s)")
        return 0

    print(
        f"line-ending-guard: {len(offenders)} file(s) mix CRLF and LF internally.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for rel, detail in offenders:
        print(f"  {rel}  ({detail})", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "A mixed file makes `git diff` show the whole file as changed and breaks\n"
        "`git blame` and byte-exact reconciliation.  Pick ONE ending per file.\n"
        "\n"
        "  fix with LF    : python -c \"import pathlib,sys;[p.write_bytes(p.read_bytes().replace(b'\\r\\n',b'\\n')) for p in map(pathlib.Path,sys.argv[1:])]\" <files>\n"
        "  fix with CRLF  : python -c \"import pathlib,sys;[p.write_bytes(p.read_bytes().replace(b'\\r\\n',b'\\n').replace(b'\\n',b'\\r\\n')) for p in map(pathlib.Path,sys.argv[1:])]\" <files>\n"
        "\n"
        "Note: for tracked files whose INDEX entry is already LF, normalising the\n"
        "worktree to LF produces no diff at all -- check with\n"
        "`git ls-files --eol <file>` before assuming a reformat is risky.",
        file=sys.stderr,
    )

    if dry:
        print("line-ending-guard: --dry set, not failing", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

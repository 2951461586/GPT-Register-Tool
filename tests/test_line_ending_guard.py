"""Behavioural tests for ``scripts/line_ending_guard.py``.

The guard exists because a file that mixes CRLF and LF internally makes
``git diff`` mark the whole file changed and destroys ``git blame``.  These
tests pin the classifier (the load-bearing part) and prove the gate actually
fires -- a guard whose predicate is never exercised is decoration.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "scripts" / "line_ending_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("line_ending_guard", GUARD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["line_ending_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


# ------------------------------------------------------------------ classifier


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", "none"),
        (b"a", "none"),
        (b"a\nb\n", "lf"),
        (b"a\r\nb\r\n", "crlf"),
        # The case the guard exists for: one stray bare \n in a CRLF file.
        (b"a\r\nb\r\nc\nd\r\n", "mixed"),
        (b"a\r\nb\n", "mixed"),
        # A single line, no terminator: not mixed, just none.
        (b"a", "none"),
        # Lone CR without LF is neither; the guard must not call it mixed.
        (b"a\rb\r", "none"),
    ],
)
def test_classify(raw: bytes, expected: str) -> None:
    assert guard.classify(raw) == expected


def test_classify_does_not_mistake_a_lone_cr_for_a_line_ending() -> None:
    """Old-Mac line endings must not be reported as mixed.

    ``a\\rb\\r`` has zero LF and zero CRLF.  A naive implementation that counts
    ``\\r`` would call it mixed and block a legitimate commit.
    """
    assert guard.classify(b"a\rb\r") == "none"


# ------------------------------------------------------- classifier self-test


def test_classifier_passes_its_own_fixtures() -> None:
    assert guard.classifier_is_trustworthy() is None


def test_self_test_detects_a_gutted_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that motivated the self-test.

    On 2026-09-17 the classifier was deliberately broken (``mixed`` always
    returning ``lf``); the guard then scanned 786 files -- including a
    knowingly-mixed probe -- and reported success.  A predicate that can be
    silently weakened makes the gate decorative, so the self-test must catch it.
    """
    monkeypatch.setattr(guard, "classify", lambda raw: "lf")
    assert guard.classifier_is_trustworthy() is not None


def test_self_test_detects_a_classifier_that_lost_the_mixed_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping only the mixed branch must also be caught."""

    def no_mixed(raw: bytes) -> str:
        crlf = raw.count(b"\r\n")
        total = raw.count(b"\n")
        if total == 0:
            return "none"
        return "crlf" if crlf else "lf"

    monkeypatch.setattr(guard, "classify", no_mixed)
    assert guard.classifier_is_trustworthy() is not None


def test_cli_fails_loudly_when_the_classifier_is_gutted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the CLI must exit non-zero on a broken predicate.

    This is the property that actually protects the repository -- not the unit
    test above, but the exit code the pre-commit hook reads.
    """
    guard_path_backup = GUARD_PATH.read_bytes()
    try:
        broken = guard_path_backup.replace(b'    return "mixed"\n', b'    return "lf"\n', 1)
        assert broken != guard_path_backup, "mutation target not found"
        GUARD_PATH.write_bytes(broken)
        result = subprocess.run(
            [sys.executable, str(GUARD_PATH), "--all"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "SELF-TEST FAILED" in result.stderr
    finally:
        GUARD_PATH.write_bytes(guard_path_backup)
    assert GUARD_PATH.read_bytes() == guard_path_backup, "failed to restore the guard"


# ---------------------------------------------------------------- file filter


@pytest.mark.parametrize(
    "name",
    ["a.py", "a.cs", "a.md", "a.json", "a.csproj", ".gitattributes", ".gitignore"],
)
def test_text_candidates_are_scanned(name: str) -> None:
    assert guard._is_text_candidate(ROOT / name) is True


@pytest.mark.parametrize("name", ["a.png", "a.ico", "a.dll", "a.zip", "a.woff2"])
def test_binary_candidates_are_not_scanned(name: str) -> None:
    # Binary-ness is now decided by content, not by the suffix alone: pass the
    # magic bytes explicitly so the test does not depend on a file existing.
    assert guard._is_text_candidate(ROOT / name, b"\x89PNG\r\n\x1a\n\x00\x00") is False


def test_extensionless_license_is_scanned() -> None:
    """The 2026-09-17 live miss.

    ``services/protocol-payment/LICENSE`` held 21 CRLF + 11 lone LF while the
    guard reported a clean tree, because ``Path("LICENSE").suffix == ""`` never
    matched the suffix whitelist so ``inspect`` never read the file.  A gate
    whose *scope* predicate can silently exclude the offender is decorative.
    """
    license_path = ROOT / "services" / "protocol-payment" / "LICENSE"
    assert guard._is_text_candidate(license_path) is True


@pytest.mark.parametrize(
    "name",
    ["LICENSE", "NOTICE", "COPYING", "Makefile", "Dockerfile", "CHANGELOG"],
)
def test_known_extensionless_text_names_are_scanned(name: str) -> None:
    """The whitelist leg, tested independently of the content sniff.

    The path is deliberately one that does NOT exist under ``ROOT``, so
    ``read_bytes`` raises ``OSError`` and the sniff cannot answer.  A ``True``
    here can therefore only come from the name whitelist.  Without this the
    whitelist is redundant -- the sniff happens to cover every real case -- and
    emptying it keeps the suite green, leaving that leg unverified.
    """
    missing = ROOT / "__no_such_dir__" / name
    assert not missing.exists()
    assert guard._is_text_candidate(missing, None, name) is True


def test_extensionless_text_is_scanned_by_content_sniff() -> None:
    """A name we never listed must still be caught if the bytes are text.

    This is the structural fix: scope is decided by evidence, not by a
    declaration that has to be kept in sync by hand.
    """
    assert guard._is_text_candidate(ROOT / "some-unlisted-name", b"plain ascii\r\n") is True


def test_extensionless_binary_is_still_skipped() -> None:
    """The sniff must not drag binaries in."""
    assert guard._is_text_candidate(ROOT / "some-blob", b"\x00\x01\x02\x03") is False


def test_undecodable_bytes_are_not_treated_as_text() -> None:
    assert guard._is_text_candidate(ROOT / "weird", b"\xff\xfe\x80\x81") is False


def test_no_tracked_extensionless_text_file_mixes_endings() -> None:
    """Guard the scope fix end-to-end on the real tree."""
    offenders = []
    for rel in guard.all_scan_names():
        path = ROOT / rel
        if path.suffix != "" and path.name not in guard.EXEMPT_NAMES:
            continue
        raw = guard.worktree_bytes(path)
        if raw is None or not guard._is_text_candidate(path, raw, rel):
            continue
        if guard.classify(raw) == "mixed":
            offenders.append(rel)
    assert not offenders, f"extensionless text file(s) mix endings: {offenders}"


# ------------------------------------------------------- the live tree is clean


def test_no_tracked_file_mixes_line_endings() -> None:
    """The ratchet: after the 2026-09-17 cleanup the tree must stay clean.

    `MIXED_EXEMPT` is empty, so this is now a hard zero: *any* mixed file fails.
    A non-empty exemption list is allowed but self-policing -- each name must
    still be mixed, or the entry is reported as stale and has to be dropped.
    """
    exempt = set(guard.MIXED_EXEMPT)
    offenders = []
    for rel in guard.all_scan_names():
        path = ROOT / rel
        raw = guard.worktree_bytes(path)
        if raw is None:
            continue
        if not guard._is_text_candidate(path, raw, rel):
            continue
        if guard.classify(raw) == "mixed":
            offenders.append(rel)
    unexpected = sorted(set(offenders) - exempt)
    assert not unexpected, (
        f"new mixed line-ending file(s): {unexpected}. Pick one ending per file; "
        f"if the file's index entry is already LF, normalising to LF is diff-free."
    )
    stale = sorted(exempt - set(offenders))
    assert not stale, (
        f"{stale} are no longer mixed -- drop them from MIXED_EXEMPT so the "
        f"guard tightens automatically."
    )


def test_no_tracked_text_file_is_crlf_while_its_index_is_lf() -> None:
    """Catch the pollution `git diff` structurally cannot see.

    `git status` and `git diff` compare the working tree against the index
    **after** applying the `text`/`eol` normalisation from `.gitattributes`, so a
    file that is entirely CRLF on disk still reads as "unmodified" when its index
    entry is LF.  That is how 330 files sat CRLF-only on disk through the whole
    P0/P1 audit without a single tool noticing.

    This test reads raw bytes and compares the worktree's line-ending *kind*
    against the index blob's kind.  It deliberately checks `crlf` and not the
    full classification: a file may legitimately be `mixed`-free but have grown
    or shrunk, which is somebody's real edit and none of this guard's business.

    Files whose declared policy IS CRLF are exempt -- `.gitattributes` says
    `*.ps1 text eol=crlf` and `*.bat text eol=crlf`, so for those the CRLF
    worktree is correct and an LF worktree would be the bug.  Hardcoding the
    suffix list here would drift from `.gitattributes`; read the attribute
    instead, and treat "index LF / worktree CRLF" as an offence only when the
    declared `eol` is not `crlf`.

    🔴 Note the index blob for a `*.ps1` is stored LF (git normalises `text` on
    the way in) while the worktree is CRLF -- exactly the byte pattern this test
    flags.  Without the attribute lookup the guard fires on five correct files.
    """
    declared_crlf = set(_paths_declaring_crlf_eol())
    offenders = []
    for rel in guard.all_scan_names():
        if rel in declared_crlf:
            continue
        path = ROOT / rel
        worktree = guard.worktree_bytes(path)
        staged = guard.staged_blob(rel)
        if worktree is None or staged is None:
            continue
        if not guard._is_text_candidate(path, worktree, rel):
            continue
        on_disk = guard.classify(worktree)
        in_index = guard.classify(staged)
        if on_disk == "crlf" and in_index == "lf":
            offenders.append(rel)
    assert not offenders, (
        f"{len(offenders)} file(s) are CRLF on disk while their index entry is LF: "
        f"{offenders[:8]}. `git diff` will NOT report these -- it normalises both "
        f"sides first -- so they read as clean while every byte-level comparison "
        f"disagrees. Fix by rewriting the worktree copy with LF endings "
        f"(byte-level: path.write_bytes(data.replace(b'\\r\\n', b'\\n'))); "
        f"when the index entry is already LF the rewrite is diff-free."
    )


def _paths_declaring_crlf_eol() -> list[str]:
    """Tracked paths whose `.gitattributes` `eol` attribute is `crlf`.

    Asking git rather than parsing the file keeps this in step with any future
    attribute change, including patterns like `*.ps1` that we would otherwise
    have to duplicate by hand.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True,
    ).stdout.decode("utf-8", "replace").split("\0")
    listed = [p for p in listed if p]
    if not listed:
        return []
    checked = subprocess.run(
        ["git", "check-attr", "--stdin", "-z", "eol"],
        cwd=str(ROOT), input="\0".join(listed).encode("utf-8"),
        capture_output=True,
    ).stdout.decode("utf-8", "replace").split("\0")
    out = []
    for i in range(0, len(checked) - 2, 3):
        path, _attr, value = checked[i], checked[i + 1], checked[i + 2]
        if value == "crlf":
            out.append(path)
    return out


# ------------------------------------------------------------- CLI behaviour


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD_PATH), *args],
        cwd=str(ROOT), capture_output=True, text=True,
    )


def test_cli_passes_on_the_current_tree() -> None:
    result = _run_cli("--all")
    assert result.returncode == 0, result.stderr


def test_cli_dry_run_never_fails() -> None:
    result = _run_cli("--all", "--dry")
    assert result.returncode == 0, result.stderr


def test_cli_staged_mode_passes_with_nothing_staged() -> None:
    """Staged mode on a clean index must pass, not crash."""
    result = _run_cli()
    assert result.returncode == 0, result.stderr

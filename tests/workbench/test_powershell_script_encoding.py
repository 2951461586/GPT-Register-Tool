"""Guard: non-ASCII PowerShell scripts must carry a UTF-8 BOM.

Windows PowerShell 5.1 (still the default `powershell.exe` on this machine and on
GitHub's Windows runners) decodes a BOM-less `.ps1` with the **system ANSI code
page** -- GBK here. The UTF-8 bytes of a Chinese comment are then mis-decoded, and
when a mangled pair swallows a quote or a newline the parser dies with a useless
``UnexpectedToken`` far away from the real line.

That is not hypothetical: ``scripts/build_installer.ps1`` was BOM-less and stopped
parsing the moment commit ``9d16fb7`` added three Chinese comment lines, so the
installer could not be built at all. The file still "looked" fine in an editor,
and ``SmsWorkbench/build_dotnet.ps1`` kept working purely because its particular
byte sequences happened to survive the mis-decode.

A BOM check is a proxy for "parses under PowerShell 5.1", and it is the right
proxy: it is cheap, deterministic, and independent of the installed PowerShell.
The negative test below pins that the checker actually fires.
"""
from __future__ import annotations

import os
from pathlib import Path

BOM = b"\xef\xbb\xbf"
SKIP_DIRS = {".git", ".venv", ".dotnet", "dist", "runtime", "node_modules", "__pycache__"}


def _needs_bom(raw: bytes) -> bool:
    """True when the file has non-ASCII bytes but no UTF-8 BOM."""
    return any(b > 127 for b in raw) and not raw.startswith(BOM)


def _iter_scripts(root: Path):
    r"""Yield every ``.ps1`` under ``root``, pruning :data:`SKIP_DIRS` as we go.

    Pruning is correctness, not an optimisation.  ``rglob`` descends into every
    directory and only lets the caller discard it *afterwards*, so the skipped
    trees are still walked -- and ``runtime/`` holds Camoufox browser profiles
    whose plain paths exceed Windows' 260-character ``MAX_PATH``.  ``os.scandir``
    fails on those with ``WinError 3`` even though the directory exists and is
    readable through the ``\\?\`` long-path prefix, so the walk raised
    ``FileNotFoundError`` from inside ``pathlib`` before the ``SKIP_DIRS`` filter
    on the next line ever ran.  Filtering after the fact cannot fix that; the
    tree has to be pruned before it is entered.

    ``onerror`` keeps a single unreadable directory from failing the whole gate.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _error: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            if name.endswith(".ps1"):
                yield base / name


def _offenders(root: Path) -> list[str]:
    """Every `.ps1` under ``root`` that would be mis-decoded by PowerShell 5.1."""
    found = []
    for path in sorted(_iter_scripts(root)):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _needs_bom(raw):
            found.append(str(path.relative_to(root)).replace("\\", "/"))
    return found


def test_every_non_ascii_powershell_script_has_a_bom():
    """The real repo must be clean -- this is what broke the installer build."""
    root = Path(__file__).resolve().parents[2]
    assert _offenders(root) == []


def test_the_checker_flags_a_bomless_non_ascii_script(tmp_path):
    """Negative test: a checker that never fires would pass the test above."""
    (tmp_path / "bad.ps1").write_bytes("# 中文注释\nWrite-Host 'x'\n".encode("utf-8"))
    assert _offenders(tmp_path) == ["bad.ps1"]


def test_a_bom_makes_the_same_script_acceptable(tmp_path):
    (tmp_path / "good.ps1").write_bytes(BOM + "# 中文注释\nWrite-Host 'x'\n".encode("utf-8"))
    assert _offenders(tmp_path) == []


def test_pure_ascii_scripts_do_not_need_a_bom(tmp_path):
    """A BOM is only required where mis-decoding can actually change the parse."""
    (tmp_path / "plain.ps1").write_bytes(b"Write-Host 'x'\n")
    assert _offenders(tmp_path) == []


def test_skipped_trees_are_pruned_before_being_entered(tmp_path, monkeypatch):
    """Regression: a skipped tree must never be walked into.

    The old ``rglob`` + post-filter walked ``runtime/`` and then threw the
    results away.  On Windows that walk dies inside ``pathlib`` with
    ``FileNotFoundError`` on the first over-``MAX_PATH`` Camoufox profile, so
    the gate failed for a reason that had nothing to do with PowerShell.
    """
    (tmp_path / "runtime" / "nested").mkdir(parents=True)
    (tmp_path / "runtime" / "nested" / "bad.ps1").write_bytes("# \u4e2d\u6587\n".encode())

    entered: list[Path] = []
    real_scandir = os.scandir

    def recording_scandir(path=".", *args, **kwargs):
        entered.append(Path(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", recording_scandir)

    assert _offenders(tmp_path) == []
    assert tmp_path / "runtime" not in entered


def test_an_unreadable_directory_does_not_fail_the_gate(tmp_path, monkeypatch):
    """One unreadable directory must not take the whole encoding gate down."""
    (tmp_path / "ok.ps1").write_bytes(b"Write-Host 'x'\n")
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    real_scandir = os.scandir

    def failing_scandir(path=".", *args, **kwargs):
        if Path(path) == blocked:
            raise FileNotFoundError(3, "path too long", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing_scandir)

    assert _offenders(tmp_path) == []

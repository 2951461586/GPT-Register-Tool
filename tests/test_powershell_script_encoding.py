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

from pathlib import Path

BOM = b"\xef\xbb\xbf"
SKIP_DIRS = {".git", ".venv", ".dotnet", "dist", "runtime", "node_modules", "__pycache__"}


def _needs_bom(raw: bytes) -> bool:
    """True when the file has non-ASCII bytes but no UTF-8 BOM."""
    return any(b > 127 for b in raw) and not raw.startswith(BOM)


def _offenders(root: Path) -> list[str]:
    """Every `.ps1` under ``root`` that would be mis-decoded by PowerShell 5.1."""
    found = []
    for path in sorted(root.rglob("*.ps1")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _needs_bom(raw):
            found.append(str(path.relative_to(root)).replace("\\", "/"))
    return found


def test_every_non_ascii_powershell_script_has_a_bom():
    """The real repo must be clean -- this is what broke the installer build."""
    root = Path(__file__).resolve().parents[1]
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

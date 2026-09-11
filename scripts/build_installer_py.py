#!/usr/bin/env python3
"""构建安装包 —— build_installer.ps1 的薄委托层。

单一正本
--------
`build_installer.ps1` 是发布管线的**唯一正本**（版本推导、载荷收集、闸门、
打包全部在那里）。本文件历史上曾逐段复刻整套流程，但两边行为必须人工保持
一致，已经漂移过、也必然再漂移——2026-09-13 起改为纯委托：能执行 PowerShell
的环境直接转调正本。

历史背景：本文件诞生是因为某个 agent sandbox 里 PowerShell 子进程执行受限
（git/dotnet 无输出），于是用 Python 复现。对操作员环境而言 PowerShell 始终
可用；若你在 PowerShell 完全不可用的环境构建，请在该脚本的 git 历史里找旧版
复刻实现，并记住它不再与正本同步。

用法:
    python scripts/build_installer_py.py [--version 2026.09.13] [--skip-publish]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "build_installer.ps1"


def main() -> int:
    args = sys.argv[1:]
    ps_args: list[str] = []
    it = iter(args)
    for arg in it:
        if arg in ("--version", "-v"):
            try:
                ps_args += ["-Version", next(it)]
            except StopIteration:
                return fail("--version requires a value")
        elif arg == "--skip-publish":
            ps_args += ["-SkipPublish"]
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            return fail(f"unknown argument: {arg}（本脚本只转发 --version/--skip-publish）")

    if shutil.which("powershell") is None:
        return fail(
            "PowerShell 不可用，无法委托 build_installer.ps1。"
            "请在 Windows 环境直接运行: scripts/build_installer.ps1"
        )

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1), *ps_args]
    print(f"[build] delegating to {PS1.name} {' '.join(ps_args)}")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def fail(message: str) -> int:
    print(f"[build] error: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

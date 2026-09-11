#!/usr/bin/env python3
"""build_installer.ps1 的 Python 等价物 —— 供无法执行 PowerShell 子进程的环境使用。

为什么需要它
------------
本机 sandbox 里 PowerShell **能启动但子进程执行被限制**：`git --version` 与
`dotnet --version` 在 PowerShell 中均无输出（实测），于是 `build_installer.ps1`
的 `git ls-files` 直接失败。而 Bash + Python subprocess 是通的，所以把同一套
流程用 Python 复现一遍。

流程与 `scripts/build_installer.ps1` 一一对应，逐段注释标注。**两边的行为必须
保持一致**；改了一边要同步另一边。

用法:
    python scripts/build_installer_py.py [--version 2026.09.11] [--skip-publish]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from inject_dotnet_env import build_env  # noqa: E402

PUBLISH_DIR = ROOT / "dist" / "net10"
INSTALLER_ROOT = ROOT / "dist" / "installer"
PACKAGE_DIR = INSTALLER_ROOT / "package"
RELEASE_DIR = ROOT / "dist" / "release"
INSTALLER_PROJECT = ROOT / "scripts" / "installer" / "GPTRegisterToolSetup.csproj"
PAYLOAD_ZIP = ROOT / "scripts" / "installer" / "payload.zip"

# 与 PowerShell 版一致：这些前缀不进载荷。
EXCLUDE_RE = re.compile(
    r"^(\.agents|\.claude|tests|SmsWorkbench\\bin|SmsWorkbench\\obj|scripts\\installer)(\\|$)"
)
DIST_RE = re.compile(r"^dist(\\|$)")

INSTALL_README = """GPT-Register-Tool Windows package

Start the desktop UI with:
  dist\\net10\\SmsWorkbench.exe

First-run setup:
  1. Install Python 3.10+ (Add to PATH), then run:
     python -m pip install -r requirements.txt -c constraints.txt
  2. config.json is created from config.example.json on install; edit it with
     local mailbox, proxy, SMS, and payment settings (the desktop Settings
     window can edit most of them).
  3. Verify the environment any time with the built-in self-check:
     python chatgpt_phone_reg.py --doctor          (human-readable)
     python chatgpt_phone_reg.py --doctor --json   (machine-readable)
     The desktop app runs the same probe automatically on first launch and
     points at missing dependencies; the Python interpreter can be configured
     in Settings when it is not on PATH.

Local runtime data is written under runtime\\ and sessions\\.
"""

START_CMD = (
    "@echo off\r\n"
    "setlocal\r\n"
    'cd /d "%~dp0"\r\n'
    'start "" "%~dp0dist\\net10\\SmsWorkbench.exe"\r\n'
)


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def dotnet() -> str:
    local = ROOT / ".dotnet" / "dotnet.exe"
    return str(local) if local.is_file() else "dotnet"


def run_dotnet(args: list[str], *, label: str) -> None:
    cmd = [dotnet(), *args]
    log(f"{label}: {' '.join(cmd[:3])} …")
    proc = subprocess.run(cmd, cwd=ROOT, env=build_env())
    if proc.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {proc.returncode}")


def clean_bin_workspaces() -> None:
    """复现 clean_dotnet_workspaces.ps1：清 bin 中间产物，保留 dist/net10。"""
    project = ROOT / "SmsWorkbench"
    for rel in ("bin/Debug/net10.0-windows", "bin/Release/net10.0-windows"):
        target = (project / rel).resolve()
        if not str(target).startswith(str((project / "bin").resolve())):
            raise SystemExit(f"refusing to clean outside SmsWorkbench/bin: {target}")
        if str(target).startswith(str(PUBLISH_DIR.resolve())):
            raise SystemExit(f"refusing to clean canonical publish dir: {target}")
        if target.exists():
            shutil.rmtree(target)
            log(f"cleaned {target.relative_to(ROOT)}")
    for rel in ("bin/Debug", "bin/Release", "bin"):
        parent = project / rel
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            log(f"removed empty {parent.relative_to(ROOT)}")


def reset_installer_root() -> None:
    """复现 Reset-Directory：只在 dist 之下重建 installer 目录。"""
    allowed = (ROOT / "dist").resolve()
    target = INSTALLER_ROOT.resolve()
    if not str(target).startswith(str(allowed)):
        raise SystemExit(f"refusing to reset path outside dist: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def stage_payload() -> int:
    """复现 `git ls-files` → 复制到 package（跳过排除项）。

    ⚠️ 必须带 `--others --exclude-standard`。裸 `ls-files` 只列**已跟踪**路径，
    于是"刚写好、还没 commit 的 release note"会被静默丢掉 —— 包里 README 指向
    一个包里根本没有的 release note（2026-09-11 实测命中）。
    未被忽略的未跟踪文件要一起进包，才能让载荷与工作树一致。
    带凭据的快照不会因此溜进来：`.gitignore` 现在按家族拒绝
    `proxy.json*` / `config.json*` / … （见该文件"凭据型本地配置的整族快照"段）。
    """
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    )
    tracked = listing.stdout.splitlines()
    copied = 0
    for rel in tracked:
        normalized = rel.replace("/", "\\")
        if EXCLUDE_RE.match(normalized) or DIST_RE.match(normalized):
            continue
        if os.path.basename(normalized).endswith("~"):
            continue
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = PACKAGE_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def copy_publish_tree() -> None:
    """复现：把 dist/net10 复制进 package，并剔除 runtime（操作员状态，不进包）。"""
    dst = PACKAGE_DIR / "dist" / "net10"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PUBLISH_DIR, dst, dirs_exist_ok=True)
    runtime = dst / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
        log("removed package/dist/net10/runtime")


def write_package_extras() -> None:
    (PACKAGE_DIR / "INSTALL-README.txt").write_text(
        INSTALL_README, encoding="utf-8", newline="\r\n"
    )
    (PACKAGE_DIR / "Start-SmsWorkbench.cmd").write_text(START_CMD, encoding="ascii")


def run_payload_gate() -> None:
    """发布闸门：被 .gitignore 拒绝的文件绝不能出货（2026-08-31 事故的护栏）。"""
    gate = ROOT / "scripts" / "scan_release_payload.py"
    if not gate.is_file():
        raise SystemExit(f"release payload gate missing: {gate}")
    python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
    exe = str(python_exe) if python_exe.is_file() else sys.executable
    log("scanning release payload …")
    proc = subprocess.run([exe, str(gate), str(PACKAGE_DIR)], cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit("release payload scan failed — refusing to build the installer")


def make_zip(zip_path: Path) -> None:
    """等价于 Compress-Archive -Path package\\*（zip 内路径相对 package）。"""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(PACKAGE_DIR):
            for name in files:
                full = Path(root) / name
                zf.write(full, full.relative_to(PACKAGE_DIR).as_posix())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="")
    parser.add_argument("--skip-publish", action="store_true")
    args = parser.parse_args()

    version = (args.version or "").lstrip("v")
    if not version:
        tag = subprocess.run(
            ["git", "-C", str(ROOT), "describe", "--tags", "--match=v*", "--abbrev=0"],
            capture_output=True, text=True,
        )
        version = tag.stdout.strip().lstrip("v") if tag.returncode == 0 else ""
    if not version:
        import datetime
        version = datetime.date.today().strftime("%Y.%m.%d")
    log(f"version = {version}")

    if not args.skip_publish:
        run_dotnet(
            [
                "publish", str(ROOT / "SmsWorkbench" / "SmsWorkbench.csproj"),
                "-c", "Release", "-r", "win-x64", "--self-contained", "false",
                "-p:PublishSingleFile=false", f"-p:Version={version}",
                "-o", str(PUBLISH_DIR),
            ],
            label="publish desktop",
        )
    clean_bin_workspaces()

    desktop_exe = PUBLISH_DIR / "SmsWorkbench.exe"
    if not desktop_exe.is_file():
        raise SystemExit(f"missing published desktop executable: {desktop_exe}")

    reset_installer_root()
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    count = stage_payload()
    log(f"staged {count} tracked file(s)")
    copy_publish_tree()
    write_package_extras()
    run_payload_gate()

    safe_version = "v" + re.sub(r"[^0-9A-Za-z_.-]", "-", version)
    zip_path = RELEASE_DIR / f"GPT-Register-Tool-win-x64-{safe_version}.zip"
    setup_path = RELEASE_DIR / f"GPT-Register-Tool-Setup-{safe_version}.exe"
    for stale in (zip_path, setup_path):
        if stale.exists():
            stale.unlink()

    make_zip(zip_path)
    log(f"built portable zip: {zip_path.name} ({zip_path.stat().st_size / 1048576:.1f} MiB)")

    shutil.copy2(zip_path, PAYLOAD_ZIP)
    try:
        setup_publish = INSTALLER_ROOT / "setup-publish"
        run_dotnet(
            [
                "publish", str(INSTALLER_PROJECT),
                "-c", "Release", "-r", "win-x64", "--self-contained", "true",
                "-p:PublishSingleFile=true", "-p:EnableCompressionInSingleFile=true",
                "-p:DebugType=none", "-p:DebugSymbols=false",
                "-o", str(setup_publish),
            ],
            label="publish installer",
        )
        produced = setup_publish / "GPTRegisterToolSetup.exe"
        if not produced.is_file():
            raise SystemExit(f"installer publish produced no exe: {produced}")
        shutil.copy2(produced, setup_path)
    finally:
        PAYLOAD_ZIP.unlink(missing_ok=True)

    manifest = RELEASE_DIR / f"GPT-Register-Tool-{safe_version}.sha256.txt"
    lines = [
        f"{sha256_of(setup_path)}  {setup_path.name}",
        f"{sha256_of(zip_path)}  {zip_path.name}",
    ]
    manifest.write_text("\n".join(lines) + "\n", encoding="ascii")

    log(f"built installer: {setup_path.name} ({setup_path.stat().st_size / 1048576:.1f} MiB)")
    log(f"wrote checksums: {manifest.name}")


if __name__ == "__main__":
    main()

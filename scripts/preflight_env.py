#!/usr/bin/env python3
"""环境预检：确认 Python、原生 Sentinel FFI、Playwright Chromium（可选）与关键 Python 包就绪。

这些是 README「环境要求」里 `pip install -r requirements.txt` 覆盖不到的运行期
前置依赖：Sentinel PoW / Turnstile / SO 签发通过进程内原生 Rust FFI
（`sms_tool/sentinel/runtime/sentinel_vm.dll`，零 Node.js 依赖）执行；
Playwright Chromium 仅在启用浏览器驱动注册或 PayPal 浏览器授权流程时需要。

退出码：0 表示全部必选项通过；非 0 表示存在缺失必选项（数量即失败项数）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def check_python_version() -> tuple[bool, str, str]:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        return True, f"Python {major}.{minor}", ""
    return False, f"Python {major}.{minor}", "安装 Python 3.12+（https://www.python.org/downloads/）"


def check_sentinel_runtime() -> tuple[bool, str, str]:
    try:
        from sms_tool.sentinel.selfcheck import run_selfcheck

        report = run_selfcheck()
    except Exception as exc:  # pragma: no cover - defensive
        return (
            False,
            f"无法执行 Sentinel 自检：{exc}",
            "运行 scripts/build_sentinel_vm.ps1 重新构建 sms_tool/sentinel/runtime/sentinel_vm.dll",
        )
    detail = f"bundle={report.get('bundle')} native={report.get('native')} mint={report.get('mint')}"
    if report.get("ok"):
        return True, detail, ""
    return (
        False,
        detail,
        "运行 scripts/build_sentinel_vm.ps1 重新构建 sentinel_vm.dll，或执行 python -m sms_tool.sentinel.selfcheck",
    )


def check_import(module: str, pip_name: str) -> tuple[bool, str, str]:
    try:
        __import__(module)
    except Exception as exc:
        return False, f"{module} 不可导入：{exc}", f"python -m pip install {pip_name}"
    return True, module, ""


def check_playwright_chromium() -> tuple[bool, str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, f"playwright 未安装：{exc}", "python -m pip install playwright"
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
    except Exception as exc:
        return False, f"无法查询 chromium：{exc}", "python -m playwright install chromium"
    if path and Path(path).exists():
        return True, "chromium 已安装", ""
    return False, "chromium 未下载", "python -m playwright install chromium"


CHECKS = (
    ("Python 版本", check_python_version, True),
    ("原生 Sentinel VM FFI (Rust)", check_sentinel_runtime, True),
    ("curl_cffi (协议传输 TLS)", lambda: check_import("curl_cffi", "curl_cffi"), True),
    ("requests", lambda: check_import("requests", "requests"), True),
    ("Playwright Chromium (可选，浏览器驱动/PayPal)", check_playwright_chromium, False),
    ("PyNaCl (可选，Agent Identity Ed25519)", lambda: check_import("nacl", "PyNaCl"), False),
)


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    print("GPT-Register-Tool 环境预检\n" + "=" * 40)
    failures: list[tuple[str, str, str]] = []
    for label, check, required in CHECKS:
        ok, detail, fix = check()
        tag = "  OK  " if ok else ("FAIL " if required else "WARN ")
        print(f"[{tag}] {label}: {detail}")
        if not ok and required:
            failures.append((label, detail, fix))

    print("=" * 40)
    if not failures:
        print("全部关键依赖就绪。")
        return 0

    print(f"发现 {len(failures)} 项缺失，修复方式：")
    for label, _detail, fix in failures:
        print(f"  - {label}: {fix or '见文档'}")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(main())

"""Gate: refuse a commit whose pinned Sentinel native runtime disagrees with its SHA256.

``sms_tool/sentinel/bundle.py`` pins the SHA256 of the shipped compiled Rust
Sentinel VM library (``sentinel_vm.dll``) and ``validate_runtime_bundle()``
refuses to load the FFI runtime if the binary drifts or is corrupted.

Standard library only: the hook may be run by any interpreter on PATH.

Usage:
    python scripts/sentinel_asset_guard.py          # check, fail on mismatch
    python scripts/sentinel_asset_guard.py --dry    # report only, never fail
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "sms_tool" / "sentinel" / "bundle.py"

PINNED_ASSETS: tuple[tuple[str, str], ...] = (("sentinel_vm.dll", "NATIVE_LIB_SHA256"),)


def load_bundle_module():
    """Load ``bundle.py`` by path, without importing the ``sms_tool`` package."""
    spec = importlib.util.spec_from_file_location("_sentinel_bundle_guard", BUNDLE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {BUNDLE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_is_trustworthy(bundle) -> str | None:
    """Self-test ``_binary_digest`` and ``_digest`` before trusting their verdict."""
    if not hasattr(bundle, "_binary_digest"):
        return "bundle module has no _binary_digest"
    if not hasattr(bundle, "_digest"):
        return "bundle module has no _digest"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.bin"
            b = Path(tmp) / "b.bin"
            a.write_bytes(b"\x00\x01\r\n\x02")
            b.write_bytes(b"\x00\x01\n\x02")
            if bundle._binary_digest(a) == bundle._binary_digest(b):
                return "_binary_digest collapsed distinct binary bytes"
            crlf = Path(tmp) / "crlf.txt"
            lf = Path(tmp) / "lf.txt"
            crlf.write_bytes(b"a\r\nb\r\n")
            lf.write_bytes(b"a\nb\n")
            if bundle._digest(crlf) != bundle._digest(lf):
                return "_digest is sensitive to text line endings (CRLF != LF)"
    except OSError as exc:
        return f"self-test could not run: {exc}"
    return None


def check_assets(bundle) -> list[tuple[str, str, str]]:
    """[(asset, pinned, actual)] for every pinned asset that disagrees with its pin."""
    runtime_dir = Path(bundle.RUNTIME_DIR)
    offenders: list[tuple[str, str, str]] = []
    for name, pin_name in PINNED_ASSETS:
        path = runtime_dir / name
        pinned = str(getattr(bundle, pin_name, "") or "")
        if not path.is_file():
            offenders.append((name, pinned, "<missing>"))
            continue
        actual = bundle._binary_digest(path)
        if actual != pinned:
            offenders.append((name, pinned, actual))
    return offenders


def main(argv: list[str]) -> int:
    dry = "--dry" in argv

    try:
        bundle = load_bundle_module()
    except Exception as exc:  # noqa: BLE001 - the gate must never crash the hook
        print(
            f"sentinel-asset-guard: could not load {BUNDLE_PATH}: {exc}",
            file=sys.stderr,
        )
        return 1

    broken = digest_is_trustworthy(bundle)
    if broken is not None:
        print(
            f"sentinel-asset-guard: SELF-TEST FAILED -- {broken}\nFix sms_tool/sentinel/bundle.py before committing.",
            file=sys.stderr,
        )
        return 1

    offenders = check_assets(bundle)
    if not offenders:
        print(f"sentinel-asset-guard: {len(PINNED_ASSETS)} pinned asset(s) match their digests")
        return 0

    print(
        f"sentinel-asset-guard: {len(offenders)} Sentinel runtime asset(s) disagree with sms_tool/sentinel/bundle.py.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for name, pinned, actual in offenders:
        print(f"  {name}", file=sys.stderr)
        print(f"    pinned: {pinned}", file=sys.stderr)
        print(f"    actual: {actual}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Pick ONE, deliberately:\n"
        "  * rebuilt native/sentinel_vm on purpose -> update NATIVE_LIB_SHA256 in bundle.py;\n"
        "  * binary changed by accident -> rebuild via scripts/build_sentinel_vm.ps1 or git checkout.",
        file=sys.stderr,
    )

    if dry:
        print("sentinel-asset-guard: --dry set, not failing", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

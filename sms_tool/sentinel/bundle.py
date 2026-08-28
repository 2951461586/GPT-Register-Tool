"""Paths and integrity checks for the vendored Sentinel runtime."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SDK_PATH = RUNTIME_DIR / "sdk.js"
RUNNER_PATH = RUNTIME_DIR / "sentinel-runner.js"
SDK_SHA256 = "de9ae60f5bcd3b8f57f5f86628630e28022f72b47056a87f37d4d8a0b5b88537"
RUNNER_SHA256 = "334ceb331dd60fe92dc9b9f802e1eef43dbd7e32e5206a4900a62dfdf83e0a21"
DEFAULT_SENTINEL_VERSION = "20260219f9f6"


class SentinelBundleError(RuntimeError):
    """Raised when a required runtime asset is missing or changed unexpectedly."""


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def validate_runtime_bundle(*, verify_hash: bool = True) -> tuple[Path, Path]:
    for path in (SDK_PATH, RUNNER_PATH):
        if not path.is_file() or path.stat().st_size <= 0:
            raise SentinelBundleError(f"sentinel_runtime_missing:{path.name}")
    if verify_hash:
        expected = {SDK_PATH: SDK_SHA256, RUNNER_PATH: RUNNER_SHA256}
        for path, digest in expected.items():
            if _digest(path) != digest:
                raise SentinelBundleError(f"sentinel_runtime_hash_mismatch:{path.name}")
    return SDK_PATH, RUNNER_PATH


def sentinel_version() -> str:
    configured = str(os.getenv("OPENAI_SENTINEL_VERSION") or "").strip()
    if not configured:
        try:
            from ..config import current_config_data

            config = current_config_data()
            email = config.get("email_registration")
            email = email if isinstance(email, dict) else {}
            configured = str(email.get("sentinel_version") or config.get("sentinel_version") or "").strip()
        except Exception:
            configured = ""
    if configured and all(char.isalnum() or char in {"-", "_"} for char in configured):
        return configured
    return DEFAULT_SENTINEL_VERSION


__all__ = [
    "RUNTIME_DIR",
    "RUNNER_PATH",
    "SDK_PATH",
    "DEFAULT_SENTINEL_VERSION",
    "SentinelBundleError",
    "sentinel_version",
    "validate_runtime_bundle",
]

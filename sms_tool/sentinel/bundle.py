"""Paths and integrity checks for the vendored Sentinel runtime."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SDK_PATH = RUNTIME_DIR / "sdk.js"
RUNNER_PATH = RUNTIME_DIR / "sentinel-runner.js"
# Digests are computed over newline-normalised bytes (see ``_digest``), so the
# pinned values below are stable regardless of the working tree's line endings.
SDK_SHA256 = "49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd"
RUNNER_SHA256 = "afb15ecfd0c40490fd8e26913a8e61b27e615464235954afdddf087264e7e31f"
# Must name the build ``runtime/sdk.js`` was copied from: the version feeds the
# frame URL, the ``Referer`` and ``p[5]``, and the browser derives all three from
# the same script.  2026-10-12: bumped from 20260219f9f6 to the build the user's
# HAR and the reference project both show (20260810913b); both are still served.
DEFAULT_SENTINEL_VERSION = "20260810913b"


class SentinelBundleError(RuntimeError):
    """Raised when a required runtime asset is missing or changed unexpectedly."""


def _digest(path: Path) -> str:
    """Hash the asset's content with line endings normalised to LF.

    Pinning a raw byte digest here is a trap: ``core.autocrlf`` and
    ``.gitattributes`` (``*.js text eol=lf``) rewrite the working tree's line
    endings at checkout while leaving the index -- and therefore ``git status``
    -- unchanged.  On 2026-09-17 that turned ``sentinel-runner.js`` from CRLF
    into LF (57592 -> 56154 bytes; byte-identical once normalised), which
    silently broke this check, fell back to the legacy issuer, and killed every
    account that reached ``create_account``.  Normalising here makes the digest
    describe the *content*, not the checkout's line-ending flavour.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


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
            # ``Mapping``, not ``dict``: ``current_config_data()`` freezes every
            # section into a ``mappingproxy`` (``config._freeze``), so a
            # ``dict`` check discarded ``email_registration.sentinel_version``
            # in production while plain-dict tests kept it green (2026-10-07
            # P1-D class).
            email = email if isinstance(email, Mapping) else {}
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

"""Pure-stdlib ctypes FFI bridge for the compiled Rust Sentinel VM (`sentinel_vm`).

Replaces the legacy Node.js subprocess/worker runtime with an in-process Rust
cdylib (`sentinel_vm.dll` / `libsentinel_vm.so` / `libsentinel_vm.dylib`).
Every C ABI symbol returns a heap-allocated UTF-8 NUL-terminated pointer whose
raw address is captured as ``c_void_p`` and freed unconditionally via ``srs_free``.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from .bundle import NATIVE_LIB_ENV, SentinelBundleError, validate_runtime_bundle


class SentinelNativeError(RuntimeError):
    """Stable failure raised by the native Rust Sentinel VM FFI boundary."""


_LIB: ctypes.CDLL | None = None
_LIB_PATH: Path | None = None
_LIB_LOCK = threading.Lock()


def reset_cached_library() -> None:
    """Drop the cached CDLL handle (used by tests that swap the library path)."""
    global _LIB, _LIB_PATH
    with _LIB_LOCK:
        _LIB = None
        _LIB_PATH = None


def native_library_path(*, verify_hash: bool = True) -> Path:
    """Resolve and validate the compiled Sentinel VM shared library path."""
    override = str(os.getenv(NATIVE_LIB_ENV) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise SentinelBundleError("sentinel_runtime_missing:native")
        return candidate
    return validate_runtime_bundle(verify_hash=verify_hash)


def _bind_symbols(lib: ctypes.CDLL) -> ctypes.CDLL:
    # Always capture `*mut c_char` as `c_void_p`: ctypes converts `c_char_p`
    # straight into a Python `bytes` object and discards the pointer address,
    # which would leak every Rust allocation.
    lib.srs_version.argtypes = []
    lib.srs_version.restype = ctypes.c_void_p

    lib.srs_free.argtypes = [ctypes.c_void_p]
    lib.srs_free.restype = None

    lib.srs_requirements_token.argtypes = [ctypes.c_char_p]
    lib.srs_requirements_token.restype = ctypes.c_void_p

    lib.srs_mint_t.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.srs_mint_t.restype = ctypes.c_void_p

    lib.srs_mint_so.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.srs_mint_so.restype = ctypes.c_void_p

    lib.srs_solve_challenge.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.srs_solve_challenge.restype = ctypes.c_void_p

    lib.srs_probe_env.argtypes = [ctypes.c_char_p]
    lib.srs_probe_env.restype = ctypes.c_void_p
    return lib


def _load_library(*, verify_hash: bool = True) -> ctypes.CDLL:
    global _LIB, _LIB_PATH
    path = native_library_path(verify_hash=verify_hash)
    with _LIB_LOCK:
        if _LIB is not None and _LIB_PATH == path:
            return _LIB
        try:
            loaded = ctypes.CDLL(str(path))
            _bind_symbols(loaded)
        except (OSError, AttributeError) as exc:
            raise SentinelNativeError(f"sentinel_native_load_failed:{type(exc).__name__}") from exc
        _LIB = loaded
        _LIB_PATH = path
        return loaded


def _take_string(lib: ctypes.CDLL, ptr: int | None) -> str:
    if not ptr:
        raise SentinelNativeError("sentinel_native_null_return")
    try:
        raw = ctypes.string_at(ptr)
    finally:
        lib.srs_free(ptr)
    text = raw.decode("utf-8", errors="replace")
    if text.startswith("ERR:"):
        detail = text[4:].strip()
        raise SentinelNativeError(detail or "sentinel_native_error")
    return text


def _encode_json(data: Mapping[str, Any] | None) -> bytes:
    if data is None:
        return b"null"
    return json.dumps(dict(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def native_version(*, verify_hash: bool = True) -> str:
    """Return the compiled ``sentinel_vm`` crate version string."""
    lib = _load_library(verify_hash=verify_hash)
    return _take_string(lib, lib.srs_version())


def requirements_token(profile: Mapping[str, Any] | None = None, *, verify_hash: bool = True) -> str:
    """Generate a requirements PoW token (``gAAAAAC...~S``) in Rust."""
    lib = _load_library(verify_hash=verify_hash)
    return _take_string(lib, lib.srs_requirements_token(_encode_json(profile)))


def mint_turnstile(
    dx: str,
    proof: str,
    profile: Mapping[str, Any] | None = None,
    *,
    verify_hash: bool = True,
) -> str:
    """Execute a Turnstile ``dx`` VM bytecode program and return ``t``."""
    lib = _load_library(verify_hash=verify_hash)
    return _take_string(
        lib,
        lib.srs_mint_t(
            str(dx).encode("utf-8"),
            str(proof).encode("utf-8"),
            _encode_json(profile),
        ),
    )


def mint_so(
    collector_dx: str,
    snapshot_dx: str,
    proof: str,
    profile: Mapping[str, Any] | None = None,
    *,
    verify_hash: bool = True,
) -> str:
    """Execute the session-observer collector + snapshot VM programs and return ``so``."""
    lib = _load_library(verify_hash=verify_hash)
    return _take_string(
        lib,
        lib.srs_mint_so(
            str(collector_dx).encode("utf-8"),
            str(snapshot_dx).encode("utf-8"),
            str(proof).encode("utf-8"),
            _encode_json(profile),
        ),
    )


def solve_challenge(
    challenge: Mapping[str, Any],
    opts: Mapping[str, Any],
    *,
    verify_hash: bool = True,
) -> str:
    """Solve one full Sentinel challenge (PoW + Turnstile + SO) in-process via Rust."""
    lib = _load_library(verify_hash=verify_hash)
    return _take_string(
        lib,
        lib.srs_solve_challenge(_encode_json(challenge), _encode_json(opts)),
    )


def probe_env(profile: Mapping[str, Any] | None = None, *, verify_hash: bool = True) -> dict[str, Any]:
    """Return the constructed Rust VM environment surface for offline consistency checks."""
    lib = _load_library(verify_hash=verify_hash)
    raw = _take_string(lib, lib.srs_probe_env(_encode_json(profile)))
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SentinelNativeError("sentinel_native_invalid_probe")
    return parsed


__all__ = [
    "SentinelNativeError",
    "mint_so",
    "mint_turnstile",
    "native_library_path",
    "native_version",
    "probe_env",
    "requirements_token",
    "reset_cached_library",
    "solve_challenge",
]

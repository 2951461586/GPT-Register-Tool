"""Paths and integrity checks for the compiled Rust Sentinel VM runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
NATIVE_LIB_ENV = "OPENAI_SENTINEL_NATIVE_LIB"
NATIVE_LIB_NAME = (
    "sentinel_vm.dll"
    if os.name == "nt"
    else ("libsentinel_vm.dylib" if sys.platform == "darwin" else "libsentinel_vm.so")
)
NATIVE_LIB_PATH = RUNTIME_DIR / NATIVE_LIB_NAME
# Raw binary SHA256 of the shipped Windows x64 cdylib built from `native/sentinel_vm/`
# via `scripts/build_sentinel_vm.ps1`. Rebuilding the crate must update this pin so
# `sentinel_asset_guard.py` and `validate_runtime_bundle()` stay in lockstep.
NATIVE_LIB_SHA256 = "ab970d5fdba670b1e24098fb34ddcda059fb55e2f165692f177721de516c592a"

# Must name the Sentinel web SDK version the protocol claims in `frame.html?sv=`,
# `Referer`, and `p[5]`. Pinned by `tests/governance/test_sentinel_version_pin.py`
# against `config.example.json`.
DEFAULT_SENTINEL_VERSION = "20260810913b"

# Optional remote SDK download helpers (retained for config-schema compatibility
# and explicit operator probes; default off).
SDK_URL_TEMPLATE = "https://sentinel.openai.com/sentinel/{version}/sdk.js"
CACHE_ENV = "SENTINEL_SDK_CACHE_DIR"
AUTO_UPDATE_ENV = "OPENAI_SENTINEL_SDK_AUTO_UPDATE"
REFRESH_ENV = "OPENAI_SENTINEL_SDK_REFRESH_SECONDS"
AUTO_UPDATE_KEY = "sentinel_sdk_auto_update"
REFRESH_KEY = "sentinel_sdk_refresh_seconds"
DEFAULT_REFRESH_SECONDS = 86400
MAX_SDK_BYTES = 5 * 1024 * 1024
SDK_MARKER = b"SentinelSDK"


class SentinelBundleError(RuntimeError):
    """Raised when a required Sentinel runtime asset is missing or changed unexpectedly."""


def _binary_digest(path: Path) -> str:
    """Raw SHA256 over binary bytes (used for compiled native libraries)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(path: Path) -> str:
    """SHA256 over text content with line endings normalised to LF."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def validate_runtime_bundle(*, verify_hash: bool = True) -> Path:
    """Verify the compiled Rust Sentinel VM library exists and matches its SHA256 pin."""
    if not NATIVE_LIB_PATH.is_file() or NATIVE_LIB_PATH.stat().st_size <= 0:
        raise SentinelBundleError(f"sentinel_runtime_missing:{NATIVE_LIB_PATH.name}")
    if verify_hash and NATIVE_LIB_NAME == "sentinel_vm.dll":
        if _binary_digest(NATIVE_LIB_PATH) != NATIVE_LIB_SHA256:
            raise SentinelBundleError(f"sentinel_runtime_hash_mismatch:{NATIVE_LIB_PATH.name}")
    return NATIVE_LIB_PATH


def email_registration_section() -> Mapping:
    """The frozen ``email_registration`` config section, or an empty mapping."""
    try:
        from ..config import current_config_data

        config = current_config_data()
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        return {}
    _raw_email: object = config.get("email_registration")
    email = _raw_email if isinstance(_raw_email, Mapping) else {}
    return email if email else _fallback_top_level(config)


def _fallback_top_level(config: Mapping) -> Mapping:
    top_level = str(config.get("sentinel_version") or "").strip()
    return {"sentinel_version": top_level} if top_level else {}


def sentinel_version() -> str:
    configured = str(os.getenv("OPENAI_SENTINEL_VERSION") or "").strip()
    if not configured:
        configured = str(email_registration_section().get("sentinel_version") or "").strip()
    if configured and all(char.isalnum() or char in {"-", "_"} for char in configured):
        return configured
    return DEFAULT_SENTINEL_VERSION


def _env_flag(name: str) -> bool | None:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    return raw in {"1", "true", "yes", "on"}


def sdk_auto_update() -> bool:
    """Whether to fetch the configured SDK version from sentinel.openai.com (default off)."""
    env = _env_flag(AUTO_UPDATE_ENV)
    if env is not None:
        return env
    raw: Any = email_registration_section().get(AUTO_UPDATE_KEY)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def sdk_refresh_seconds() -> int:
    raw = str(os.environ.get(REFRESH_ENV) or "").strip()
    if not raw:
        raw = str(email_registration_section().get(REFRESH_KEY) or "").strip()
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_SECONDS
    return max(60, seconds) if seconds > 0 else DEFAULT_REFRESH_SECONDS


def cache_root() -> Path:
    """Where downloaded SDK builds live (``SENTINEL_SDK_CACHE_DIR`` overrides)."""
    override = str(os.environ.get(CACHE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from ..config import current_config_data
        from ..kernel.paths import runtime_dir

        return runtime_dir(current_config_data()) / "sentinel-sdk"
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        return RUNTIME_DIR.parents[1] / "runtime" / "sentinel-sdk"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            handle = out.name
            out.write(data)
        os.replace(handle, path)
    finally:
        if handle and os.path.exists(handle):
            os.unlink(handle)


def _cached_sdk(version: str, *, require_fresh: bool = True) -> Path | None:
    directory = cache_root() / version
    try:
        meta = json.loads((directory / "current.json").read_text(encoding="utf-8"))
        digest = str(meta["sha256"])
        fetched_at = float(meta["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    path = directory / f"{digest}.js"
    if not path.is_file():
        return None
    if require_fresh and not 0 <= time.time() - fetched_at < sdk_refresh_seconds():
        return None
    try:
        data = path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return None
    return path if hashlib.sha256(data).hexdigest() == digest else None


def _sdk_request_headers() -> dict[str, str]:
    from ..transport import endpoints
    from ..transport.auth_headers import auth_user_agent

    return {
        "accept": "*/*",
        "referer": endpoints.AUTH_BASE + "/",
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-site",
        "user-agent": auth_user_agent(),
    }


def _verified_sdk_content(response: Any) -> bytes:
    content = getattr(response, "content", b"") or b""
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200 or not isinstance(content, bytes) or not content:
        raise SentinelBundleError(f"sentinel_sdk_fetch_rejected:{status}")
    if len(content) > MAX_SDK_BYTES or SDK_MARKER not in content or content.lstrip().startswith(b"<"):
        raise SentinelBundleError("sentinel_sdk_fetch_invalid")
    return content


def fetch_sdk(version: str, *, session: Any = None, timeout: float = 30.0) -> bytes:
    if not version or not all(char.isalnum() or char in {"-", "_"} for char in version):
        raise SentinelBundleError(f"sentinel_sdk_invalid_version:{version}")
    url = SDK_URL_TEMPLATE.format(version=version)
    if session is not None:
        return _verified_sdk_content(_get(session, url, {}, timeout))
    from ..transport import http_backend
    from ..transport.auth_headers import auth_impersonate

    own = http_backend.new_session()
    try:
        response = _get(own, url, _sdk_request_headers(), timeout, impersonate=auth_impersonate())
    finally:
        own.close()
    return _verified_sdk_content(response)


def _get(
    session: Any,
    url: str,
    headers: dict[str, str],
    timeout: float,
    *,
    impersonate: str = "",
) -> Any:
    try:
        kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if impersonate:
            kwargs["impersonate"] = impersonate
        return session.get(url, **kwargs)
    except SentinelBundleError:
        raise
    except Exception as exc:  # broad-except: transport errors all mean "no SDK today"
        raise SentinelBundleError(f"sentinel_sdk_fetch_failed:{type(exc).__name__}") from exc


def _download_sdk(version: str, *, session: Any = None) -> Path:
    content = fetch_sdk(version, session=session)
    digest = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
    directory = cache_root() / version
    path = directory / f"{digest}.js"
    _atomic_write(path, content)
    _atomic_write(
        directory / "current.json",
        json.dumps({"sha256": digest, "fetched_at": time.time()}, sort_keys=True).encode("utf-8"),
    )
    _LOGGER.info("[Sentinel] SDK refreshed version=%s sha256=%s", version, digest[:12])
    return path


def resolve_sdk_path(*, session: Any = None, verify_vendored: bool = True) -> Path:
    """Resolve the active Sentinel runtime asset or optional cached SDK probe."""
    version = sentinel_version()
    pinned = version == DEFAULT_SENTINEL_VERSION
    if not sdk_auto_update():
        if not pinned:
            _warn_version_drift(version)
        return validate_runtime_bundle(verify_hash=verify_vendored)
    cached = _cached_sdk(version)
    if cached is not None:
        return cached
    try:
        return _download_sdk(version, session=session)
    except (SentinelBundleError, OSError, ValueError) as exc:
        stale = _cached_sdk(version, require_fresh=False)
        if stale is not None:
            _LOGGER.warning("[Sentinel] SDK refresh failed (%s); using cached build %s", exc, version)
            return stale
        if pinned:
            _LOGGER.warning("[Sentinel] SDK refresh failed (%s); using native runtime %s", exc, version)
            return validate_runtime_bundle(verify_hash=verify_vendored)
        raise SentinelBundleError(f"sentinel_sdk_unavailable:{version}") from exc


def _warn_version_drift(version: str) -> None:
    key = (version, DEFAULT_SENTINEL_VERSION)
    if key in _WARNED_DRIFT:
        return
    _WARNED_DRIFT.add(key)
    _LOGGER.warning(
        "[Sentinel] configured sdk version %s != pinned build %s; update DEFAULT_SENTINEL_VERSION "
        "or enable %s=true if probing a newer CDN build",
        version,
        DEFAULT_SENTINEL_VERSION,
        AUTO_UPDATE_KEY,
    )


_WARNED_DRIFT: set[tuple[str, str]] = set()


__all__ = [
    "AUTO_UPDATE_ENV",
    "AUTO_UPDATE_KEY",
    "CACHE_ENV",
    "DEFAULT_REFRESH_SECONDS",
    "DEFAULT_SENTINEL_VERSION",
    "MAX_SDK_BYTES",
    "NATIVE_LIB_ENV",
    "NATIVE_LIB_NAME",
    "NATIVE_LIB_PATH",
    "NATIVE_LIB_SHA256",
    "REFRESH_ENV",
    "REFRESH_KEY",
    "RUNTIME_DIR",
    "SDK_MARKER",
    "SDK_URL_TEMPLATE",
    "SentinelBundleError",
    "cache_root",
    "email_registration_section",
    "fetch_sdk",
    "resolve_sdk_path",
    "sdk_auto_update",
    "sdk_refresh_seconds",
    "sentinel_version",
    "validate_runtime_bundle",
]

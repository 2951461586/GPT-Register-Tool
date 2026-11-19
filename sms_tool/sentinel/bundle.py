"""Paths and integrity checks for the vendored Sentinel runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SDK_PATH = RUNTIME_DIR / "sdk.js"
RUNNER_PATH = RUNTIME_DIR / "sentinel-runner.js"
# Digests are computed over newline-normalised bytes (see ``_digest``), so the
# pinned values below are stable regardless of the working tree's line endings.
SDK_SHA256 = "49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd"
RUNNER_SHA256 = "e7e68ab1269f6bce6194e4d2106944bd266b66d4c5666af995b1ead03edd45b4"
# ``sentinel-runner.js`` carries three local patches (2026-11-11
# ``performance.memory`` gating; 2026-11-12 the missing navigator identity
# properties + Gecko ``window.InstallTrigger``, and the page clock behind
# ``performance.timeOrigin``).  See THIRD_PARTY_NOTICES.md.  The digest below is
# the patched file, so an upstream re-vendor must re-apply the patches *and*
# update this constant; the ``sentinel_runtime_hash_mismatch`` guard is what
# turns a silent drift into a loud failure.
#
# Must name the build ``runtime/sdk.js`` was copied from: the version feeds the
# frame URL, the ``Referer`` and ``p[5]``, and the browser derives all three from
# the same script.  2026-10-12: bumped from 20260219f9f6 to the build the user's
# HAR and the reference project both show (20260810913b); both are still served.
DEFAULT_SENTINEL_VERSION = "20260810913b"

# ───────────── optional live refresh of the SDK ─────────────
#
# The vendored ``runtime/sdk.js`` is the *authority*: it is hash-pinned and the
# only build whose behaviour this repository has measured.  OpenAI rotates the
# file under a new version path, and the version string it names is not
# cosmetic -- it feeds the ``frame.html?sv=`` URL, the ``Referer`` and ``p[5]``,
# so claiming a version whose bytes we are not running is a self-contradicting
# proof.  The reference project simply re-downloads the configured version every
# day (``SENTINEL_SDK_AUTO_UPDATE``); this module keeps the pin as the default
# and makes the download an explicit, cached, verified opt-in:
#
#   email_registration.sentinel_sdk_auto_update = true   (or env below)
#
# Downloading is never silently load-bearing: a failed refresh falls back to the
# last verified cache entry, then to the vendored file (when the configured
# version *is* the vendored version) and otherwise fails with a stable token.
SDK_URL_TEMPLATE = "https://sentinel.openai.com/sentinel/{version}/sdk.js"
CACHE_ENV = "SENTINEL_SDK_CACHE_DIR"
AUTO_UPDATE_ENV = "OPENAI_SENTINEL_SDK_AUTO_UPDATE"
REFRESH_ENV = "OPENAI_SENTINEL_SDK_REFRESH_SECONDS"
AUTO_UPDATE_KEY = "sentinel_sdk_auto_update"
REFRESH_KEY = "sentinel_sdk_refresh_seconds"
DEFAULT_REFRESH_SECONDS = 86400
#: A Sentinel SDK build is ~33 KiB; 5 MiB is the reference project's ceiling and
#: exists to stop a captive-portal or error page from being cached as a script.
MAX_SDK_BYTES = 5 * 1024 * 1024
#: Every real build embeds this token; an HTML error page does not.
SDK_MARKER = b"SentinelSDK"


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


def email_registration_section() -> Mapping:
    """The frozen ``email_registration`` config section, or an empty mapping.

    ``Mapping``, not ``dict``: ``current_config_data()`` freezes every section
    into a ``mappingproxy`` (``config._freeze``), so a ``dict`` check discarded
    ``email_registration.sentinel_version`` in production while plain-dict tests
    kept it green (2026-10-07 P1-D class).
    """
    try:
        from ..config import current_config_data

        config = current_config_data()
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        # Not configured (or a shard is unreadable): the built-in defaults are
        # the correct answer, and a caller of ``sentinel_version()`` has no way
        # to act on a config error.  ``ConfigError`` subclasses ``ValueError``.
        return {}
    _raw_email: object = config.get("email_registration")
    email = _raw_email if isinstance(_raw_email, Mapping) else {}
    return email if email else _fallback_top_level(config)


def _fallback_top_level(config: Mapping) -> Mapping:
    """Keep the legacy top-level ``sentinel_version`` readable.

    Older configs carried the version at the root; the section read above wiped
    it out. Returning a one-key mapping preserves that path without teaching
    every caller two shapes.
    """
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
    """Whether to fetch the configured SDK version from sentinel.openai.com.

    Default **off**: the vendored build is the only one this repo measured, and
    a registration that silently swaps its own SDK is not a change that should
    arrive through a config default.  Env wins over config so an operator can
    turn it on for one batch without editing a shard.
    """
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
    """Where downloaded SDK builds live (``SENTINEL_SDK_CACHE_DIR`` overrides).

    Under the resolved runtime directory rather than the package tree: the
    package is replaced on upgrade, and a downloaded build must not look like a
    vendored one.
    """
    override = str(os.environ.get(CACHE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from ..config import current_config_data
        from ..kernel.paths import runtime_dir

        return runtime_dir(current_config_data()) / "sentinel-sdk"
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        # Same reasoning as ``email_registration_section``: an unreadable config
        # degrades to the default location, never to "no cache".
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
    """A previously downloaded build whose bytes still hash to its manifest."""
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
    """Headers a browser sends for the ``sdk.js`` script tag."""
    from .. import endpoints
    from ..auth_headers import auth_user_agent

    return {
        "accept": "*/*",
        "referer": endpoints.AUTH_BASE + "/",
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-site",
        "user-agent": auth_user_agent(),
    }


def _verified_sdk_content(response: Any) -> bytes:
    """Reject everything that is not an SDK build (status, size, marker, HTML)."""
    content = getattr(response, "content", b"") or b""
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200 or not isinstance(content, bytes) or not content:
        raise SentinelBundleError(f"sentinel_sdk_fetch_rejected:{status}")
    if len(content) > MAX_SDK_BYTES or SDK_MARKER not in content or content.lstrip().startswith(b"<"):
        raise SentinelBundleError("sentinel_sdk_fetch_invalid")
    return content


def fetch_sdk(version: str, *, session: Any = None, timeout: float = 30.0) -> bytes:
    """GET ``/sentinel/<version>/sdk.js`` under the profile's own TLS identity.

    The fetch uses the *same* impersonation as the registration it serves: a
    download from a different ClientHello than the one that will mint the token
    is a second identity on the same edge node.
    """
    if not version or not all(char.isalnum() or char in {"-", "_"} for char in version):
        raise SentinelBundleError(f"sentinel_sdk_invalid_version:{version}")
    url = SDK_URL_TEMPLATE.format(version=version)
    if session is not None:
        return _verified_sdk_content(_get(session, url, {}, timeout))
    from curl_cffi import requests as curl_requests

    from ..auth_headers import auth_impersonate

    own = curl_requests.Session(impersonate=auth_impersonate())
    try:
        response = _get(own, url, _sdk_request_headers(), timeout)
    finally:
        own.close()
    return _verified_sdk_content(response)


def _get(session: Any, url: str, headers: dict[str, str], timeout: float) -> Any:
    try:
        return session.get(url, headers=headers, timeout=timeout)
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
    """The ``sdk.js`` this process must execute, and which its proof may claim.

    Order: a fresh verified cache entry, then a download, then the last verified
    cache entry even if stale, then the vendored build -- the vendored file only
    when it *is* the configured version, because running 0810 bytes while
    claiming 0219 is exactly the drift this function exists to stop.
    """
    version = sentinel_version()
    pinned = version == DEFAULT_SENTINEL_VERSION
    if not sdk_auto_update():
        if not pinned:
            _warn_version_drift(version)
        validate_runtime_bundle(verify_hash=verify_vendored)
        return SDK_PATH
    cached = _cached_sdk(version)
    if cached is not None:
        return cached
    try:
        return _download_sdk(version, session=session)
    except (SentinelBundleError, OSError, ValueError) as exc:
        # ``fetch_sdk``/``_get`` already fold transport errors into
        # ``SentinelBundleError``; the rest is the cache write (``OSError``) or a
        # malformed manifest (``ValueError``).  Anything else is a real bug and
        # must surface as one.
        stale = _cached_sdk(version, require_fresh=False)
        if stale is not None:
            _LOGGER.warning("[Sentinel] SDK refresh failed (%s); using cached build %s", exc, version)
            return stale
        if pinned:
            _LOGGER.warning("[Sentinel] SDK refresh failed (%s); using vendored build %s", exc, version)
            validate_runtime_bundle(verify_hash=verify_vendored)
            return SDK_PATH
        raise SentinelBundleError(f"sentinel_sdk_unavailable:{version}") from exc


def _warn_version_drift(version: str) -> None:
    """Say once per process that the claim and the bytes disagree.

    Not fatal on purpose: every account already registered under this
    combination worked, so failing closed would turn a callable warning into an
    outage.  The drift is loud here, and ``tests/governance`` keeps the shipped
    example from handing it out in the first place.
    """
    key = (version, DEFAULT_SENTINEL_VERSION)
    if key in _WARNED_DRIFT:
        return
    _WARNED_DRIFT.add(key)
    _LOGGER.warning(
        "[Sentinel] configured sdk version %s != vendored build %s; the proof will claim a version "
        "whose bytes are not being executed (%s=true downloads it instead)",
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
    "REFRESH_ENV",
    "REFRESH_KEY",
    "RUNTIME_DIR",
    "RUNNER_PATH",
    "SDK_MARKER",
    "SDK_PATH",
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

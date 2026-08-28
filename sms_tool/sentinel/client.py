"""Deep Sentinel issuance module shared by registration and recovery flows."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from curl_cffi import requests as curl_requests

from ..auth_headers import auth_impersonate, auth_user_agent, sentinel_fingerprint
from ..phone_proxy import normalize_proxy_url
from .bundle import sentinel_version
from .runner import SentinelRunnerError, run_sentinel_sdk


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
FLOW_PAGE_URLS = {
    "username_password_create": "https://auth.openai.com/create-account/password",
    "authorize_continue": "https://auth.openai.com/email-verification",
    "oauth_create_account": "https://auth.openai.com/about-you",
    "checkout_session_approval": "https://chatgpt.com/",
}


class SentinelIssueError(RuntimeError):
    """Stable failure raised by the Sentinel issuance interface."""


@dataclass(frozen=True)
class SentinelToken:
    flow: str
    device_id: str
    token: str
    so_token: str = ""
    challenge: Mapping[str, Any] | None = None


def _config_root(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    root = config
    if root is None:
        try:
            from ..config import current_config_data

            root = current_config_data()
        except Exception:
            root = {}
    return root if isinstance(root, Mapping) else {}


def sentinel_backend(config: Mapping[str, Any] | None = None) -> str:
    root = _config_root(config)
    email = root.get("email_registration")
    email = email if isinstance(email, Mapping) else {}
    value = str(
        os.getenv("OPENAI_SENTINEL_BACKEND")
        or email.get("sentinel_backend")
        or root.get("sentinel_backend")
        or "node_runner"
    ).strip().lower()
    if value in {"legacy", "quickjs", "browser", "old"}:
        return "legacy"
    return "node_runner"


def _legacy_fallback_enabled(config: Mapping[str, Any] | None) -> bool:
    root = _config_root(config)
    email = root.get("email_registration")
    email = email if isinstance(email, Mapping) else {}
    value = os.getenv("OPENAI_SENTINEL_LEGACY_FALLBACK")
    if value is None:
        value = email.get("sentinel_legacy_fallback", True)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _token_from_bundle(
    data: Mapping[str, Any] | None,
    *,
    flow: str,
    device_id: str,
) -> SentinelToken | None:
    values = data if isinstance(data, Mapping) else {}
    token_key = {
        "username_password_create": "sentinel_token",
        "authorize_continue": "sentinel_authorize_continue_token",
        "oauth_create_account": "sentinel_oauth_token",
    }.get(flow, "")
    so_key = {
        "authorize_continue": "sentinel_authorize_continue_so_token",
        "oauth_create_account": "sentinel_so_token",
    }.get(flow, "")
    token = str(values.get(token_key) or "").strip() if token_key else ""
    if not token:
        return None
    try:
        payload = json.loads(token)
    except (TypeError, ValueError) as exc:
        raise SentinelIssueError(f"sentinel_supplied_malformed:{flow}") from exc
    token_device = str(payload.get("id") or "")
    token_flow = str(payload.get("flow") or "")
    if token_device and token_device != device_id:
        raise SentinelIssueError(f"sentinel_supplied_device_mismatch:{flow}")
    if token_flow and token_flow != flow:
        raise SentinelIssueError(f"sentinel_supplied_flow_mismatch:{flow}")
    so_token = str(values.get(so_key) or "").strip() if so_key else ""
    return SentinelToken(
        flow=flow,
        device_id=device_id,
        token=token,
        so_token=so_token,
    )


def _requirements_token(device_id: str, profile: Mapping[str, Any]) -> str:
    """Generate the SDK-compatible initial requirements proof."""
    import base64
    from datetime import datetime
    from zoneinfo import ZoneInfo

    screen = str(profile.get("screen") or "1920x1080")
    width, _, height = screen.partition("x")
    timezone_name = str(profile.get("timezone") or "UTC")
    try:
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now().astimezone()
    config = [
        int(width or 1920) + int(height or 1080),
        now.strftime("%a %b %d %Y %H:%M:%S GMT%z (%Z)"),
        int(profile.get("js_heap_size_limit") or 4_395_630_592),
        1,
        str(profile.get("user_agent") or "Mozilla/5.0"),
        str(
            profile.get("script_src")
            or f"https://sentinel.openai.com/sentinel/{sentinel_version()}/sdk.js"
        ),
        None,
        str(profile.get("lang") or "en-US"),
        ",".join(
            item.split(";", 1)[0].strip()
            for item in str(profile.get("lang_full") or profile.get("lang") or "en-US").split(",")
            if item.split(";", 1)[0].strip()
        ),
        1,
        "userAgent−function userAgent() { [native code] }",
        "body",
        "crypto",
        float(profile.get("performance_now") or 12345.67),
        str(profile.get("session_id") or device_id),
        "",
        int(profile.get("hardware_concurrency") or 8),
        float(profile.get("time_origin") or (time.time() * 1000 - 12345.67)),
        0, 0, 0, 0, 0, 0, 1,
    ]
    encoded = base64.b64encode(
        json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"gAAAAAC{encoded}~S"


def _cookie_header(session: Any, device_id: str) -> str:
    pairs: list[str] = []
    cookies = getattr(session, "cookies", None)
    try:
        if hasattr(cookies, "get_dict"):
            pairs.extend(
                f"{name}={value}"
                for name, value in cookies.get_dict().items()
                if name and value
            )
    except Exception:
        pass
    if not any(item.lower().startswith("oai-did=") for item in pairs):
        pairs.insert(0, f"oai-did={device_id}")
    return "; ".join(dict.fromkeys(pairs))


def _challenge(
    session: Any,
    *,
    flow: str,
    device_id: str,
    profile: Mapping[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    proof = _requirements_token(device_id, profile)
    response = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": proof, "id": device_id, "flow": flow}, separators=(",", ":")),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://sentinel.openai.com",
            "Referer": (
                "https://sentinel.openai.com/backend-api/sentinel/"
                f"frame.html?sv={sentinel_version()}"
            ),
            "User-Agent": str(profile.get("user_agent") or auth_user_agent()),
        },
        timeout=max(10, min(int(timeout_seconds or 60), 120)),
        impersonate=auth_impersonate(),
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise SentinelIssueError(f"sentinel_challenge_http_{status}")
    try:
        payload = response.json()
    except Exception as exc:
        raise SentinelIssueError("sentinel_challenge_invalid_json") from exc
    if not isinstance(payload, dict) or not str(payload.get("token") or "").strip():
        raise SentinelIssueError("sentinel_challenge_incomplete")
    return payload


def issue_sentinel_token(
    *,
    flow: str,
    device_id: str,
    session: Any | None = None,
    proxy: str | None = None,
    profile: Mapping[str, Any] | None = None,
    page_url: str = "",
    timeout_seconds: int = 60,
) -> SentinelToken:
    """Issue one flow-bound token using the same session and fingerprint."""
    flow = str(flow or "").strip()
    device_id = str(device_id or "").strip() or str(uuid.uuid4())
    if flow not in FLOW_PAGE_URLS:
        raise SentinelIssueError(f"sentinel_flow_unsupported:{flow}")
    owned_session = session is None
    active_session = session or curl_requests.Session()
    normalized_proxy = normalize_proxy_url(proxy)
    if normalized_proxy and owned_session:
        active_session.proxies = {"http": normalized_proxy, "https": normalized_proxy}
    try:
        active_session.cookies.set("oai-did", device_id, domain=".openai.com", path="/")
    except Exception:
        pass
    active_profile = dict(profile or sentinel_fingerprint())
    active_profile.setdefault("session_id", str(uuid.uuid4()))
    try:
        challenge = _challenge(
            active_session,
            flow=flow,
            device_id=device_id,
            profile=active_profile,
            timeout_seconds=timeout_seconds,
        )
        token = run_sentinel_sdk(
            challenge,
            flow=flow,
            device_id=device_id,
            profile=active_profile,
            cookie=_cookie_header(active_session, device_id),
            page_url=str(page_url or FLOW_PAGE_URLS[flow]),
            timeout_seconds=timeout_seconds,
        )
    except (SentinelRunnerError, SentinelIssueError):
        raise
    except Exception as exc:
        raise SentinelIssueError(f"sentinel_issue_failed:{type(exc).__name__}") from exc
    finally:
        if owned_session:
            try:
                active_session.close()
            except Exception:
                pass
    parsed = json.loads(token)
    so_value = str(parsed.get("so") or "")
    so_token = ""
    if so_value:
        so_token = json.dumps(
            {
                "so": so_value,
                "c": str(parsed.get("c") or challenge.get("token") or ""),
                "id": device_id,
                "flow": flow,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
    return SentinelToken(
        flow=flow,
        device_id=device_id,
        token=token,
        so_token=so_token,
        challenge=challenge,
    )


def issue_sentinel_flow(
    *,
    flow: str,
    device_id: str,
    session: Any | None = None,
    proxy: str | None = None,
    profile: Mapping[str, Any] | None = None,
    supplied_data: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    timeout_seconds: int = 60,
) -> SentinelToken:
    """Issue at a protocol step, with explicit legacy rollback compatibility."""
    supplied = _token_from_bundle(supplied_data, flow=flow, device_id=device_id)
    if supplied is not None:
        return supplied
    backend = sentinel_backend(config)
    if backend == "node_runner":
        try:
            return issue_sentinel_token(
                flow=flow,
                device_id=device_id,
                session=session,
                proxy=proxy,
                profile=profile,
                timeout_seconds=timeout_seconds,
            )
        except Exception as runner_error:
            if not _legacy_fallback_enabled(config):
                raise
            print(
                "  [Sentinel] Node runner failed; using configured legacy fallback "
                f"for {flow}: {type(runner_error).__name__}"
            )

    from ..sentinel_tokens import _extract_sentinel

    data = _extract_sentinel(
        proxy=proxy,
        force_fresh=True,
        persist=False,
        device_id=device_id,
    )
    legacy = _token_from_bundle(data, flow=flow, device_id=device_id)
    if legacy is None:
        raise SentinelIssueError(f"sentinel_legacy_incomplete:{flow}")
    return legacy


def issue_sentinel_bundle(
    *,
    flows: tuple[str, ...] = (
        "username_password_create",
        "authorize_continue",
        "oauth_create_account",
    ),
    device_id: str = "",
    session: Any | None = None,
    proxy: str | None = None,
    profile: Mapping[str, Any] | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Compatibility adapter returning the historical Sentinel bundle shape."""
    did = str(device_id or "").strip() or str(uuid.uuid4())
    owned_session = session is None
    active_session = session or curl_requests.Session()
    normalized_proxy = normalize_proxy_url(proxy)
    if normalized_proxy and owned_session:
        active_session.proxies = {"http": normalized_proxy, "https": normalized_proxy}
    active_profile = dict(profile or sentinel_fingerprint())
    active_profile.setdefault("session_id", str(uuid.uuid4()))
    issued: dict[str, SentinelToken] = {}
    cookie_str = ""
    try:
        for flow in flows:
            issued[flow] = issue_sentinel_token(
                flow=flow,
                device_id=did,
                session=active_session,
                profile=active_profile,
                timeout_seconds=timeout_seconds,
            )
        cookie_str = _cookie_header(active_session, did)
    finally:
        if owned_session:
            try:
                active_session.close()
            except Exception:
                pass
    username = issued.get("username_password_create")
    authorize = issued.get("authorize_continue")
    oauth = issued.get("oauth_create_account")
    return {
        "sentinel_token": username.token if username else "",
        "sentinel_authorize_continue_token": authorize.token if authorize else "",
        "sentinel_authorize_continue_so_token": authorize.so_token if authorize else "",
        "sentinel_oauth_token": oauth.token if oauth else "",
        "sentinel_so_token": oauth.so_token if oauth else "",
        "cookie_str": cookie_str or f"oai-did={did}",
        "oai_did": did,
        "sentinel_source": "node_sdk_runner",
    }


__all__ = [
    "FLOW_PAGE_URLS",
    "SentinelIssueError",
    "SentinelToken",
    "issue_sentinel_bundle",
    "issue_sentinel_flow",
    "issue_sentinel_token",
    "sentinel_backend",
]

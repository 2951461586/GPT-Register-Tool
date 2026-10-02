"""Deep Sentinel issuance module shared by registration and recovery flows."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from curl_cffi import requests as curl_requests

from .. import endpoints
from ..auth_headers import auth_impersonate, auth_user_agent, sentinel_fingerprint
from ..http_client import request_with_retry
from ..phone_proxy import normalize_proxy_url, redact_proxy_text
from .bundle import sentinel_version
from .runner import SentinelRunnerError, run_sentinel_sdk


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
#: Sentinel flow the ChatGPT ``payments/checkout`` create gate reads. The
#: reference project reverse-engineered this from the web bundle
#: (``requireSentinelCheckout``) and proved live that the create call returns
#: HTTP 200 only when *both* ``openai-sentinel-token`` and
#: ``openai-sentinel-so-token`` are attached under this flow. A missing SO is
#: the difference between 200 and ``400 unusual activity``.
CHECKOUT_SENTINEL_FLOW = "chatgpt_checkout"
FLOW_PAGE_URLS = {
    "username_password_create": endpoints.AUTH_CREATE_ACCOUNT_PASSWORD,
    "authorize_continue": endpoints.AUTH_EMAIL_VERIFICATION,
    "oauth_create_account": endpoints.AUTH_ABOUT_YOU,
    "checkout_session_approval": endpoints.CHATGPT_ORIGIN,
    CHECKOUT_SENTINEL_FLOW: endpoints.CHATGPT_ORIGIN,
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
    value = (
        str(
            os.getenv("OPENAI_SENTINEL_BACKEND")
            or email.get("sentinel_backend")
            or root.get("sentinel_backend")
            or "node_runner"
        )
        .strip()
        .lower()
    )
    if value in {"legacy", "quickjs", "browser", "old"}:
        return "legacy"
    return "node_runner"


def _legacy_fallback_enabled(config: Mapping[str, Any] | None) -> bool:
    root = _config_root(config)
    email = root.get("email_registration")
    email = email if isinstance(email, Mapping) else {}
    value = os.getenv("OPENAI_SENTINEL_LEGACY_FALLBACK")
    if value is None:
        # Default **off**.  The pure-Python legacy issuer synthesises a PoW that
        # passes the surface endpoints, but the OTP-dispatch service runs the
        # real SDK JS server-side and rejects it (measured 2026-09-17: the silent
        # fallback turned a runner defect into 126/126 ``create_account``
        # failures).  A dead runner must fail loudly so a batch is not lost
        # behind a degradation that looks like an upstream fault.
        value = email.get("sentinel_legacy_fallback", False)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_REASON_LIMIT = 120


def _root_reason(exc: BaseException, proxy: str | None = None) -> str:
    """Render the deepest ``__cause__`` of ``exc`` as one bounded, redacted token.

    ``sentinel_issue_failed:{type name}`` kept only the *wrapper's* type name, and
    the actionable detail lives in the innermost message.  On 2026-09-17 the chain
    ended at ``SentinelBundleError("sentinel_runtime_hash_mismatch:sentinel-runner.js")``
    -- yet that literal string appeared in **no** log line of the entire batch
    (3161 lines of ``backend_stdout.jsonl`` checked, 0 hits), so the failure read
    as an upstream channel fault while the real defect was a corrupt vendored
    asset.  Follow the explicit ``from`` chain to the root so the cause survives
    into the log.

    The result is bounded because the progress ledger truncates the error field to
    300 characters (``registration_progress.py:180``) and the account store to 800
    (``store/accounts.py:159``); an unbounded message would push the useful tail
    out of both.
    """
    deepest = exc
    seen = {id(exc)}
    while isinstance(deepest.__cause__, BaseException) and id(deepest.__cause__) not in seen:
        deepest = deepest.__cause__
        seen.add(id(deepest))
    try:
        text = " ".join(str(deepest).split())
    except Exception:
        text = ""
    if text:
        text = redact_proxy_text(text, proxy)
    if not text:
        return type(deepest).__name__
    if len(text) > _REASON_LIMIT:
        text = text[: _REASON_LIMIT - 3].rstrip() + "..."
    return f"{type(deepest).__name__}({text})"


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

    screen = str(profile.get("screen") or "1920x1080")
    width, _, height = screen.partition("x")
    timezone_name = str(profile.get("timezone") or "UTC")
    # Single authority (P1-4).  The old inline ``ZoneInfo`` here raised
    # ZoneInfoNotFoundError on any host without the IANA database -- Windows
    # included -- and the except branch then fell back to
    # ``datetime.now().astimezone()``: the *local machine's* clock stamped into
    # a proof that claims New York.  The fallback still has to be something,
    # but it now goes through the module that logs the root cause once.
    from ..geo.clock import now_in_timezone

    now = now_in_timezone(timezone_name)
    config = [
        int(width or 1920) + int(height or 1080),
        now.strftime("%a %b %d %Y %H:%M:%S GMT%z (%Z)"),
        int(profile.get("js_heap_size_limit") or 4_395_630_592),
        1,
        str(profile.get("user_agent") or "Mozilla/5.0"),
        str(profile.get("script_src") or f"https://sentinel.openai.com/sentinel/{sentinel_version()}/sdk.js"),
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
        0,
        0,
        0,
        0,
        0,
        0,
        1,
    ]
    encoded = base64.b64encode(json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )
    return f"gAAAAAC{encoded}~S"


def _cookie_header(session: Any, device_id: str) -> str:
    pairs: list[str] = []
    cookies = getattr(session, "cookies", None)
    try:
        get_dict = getattr(cookies, "get_dict", None)
        if callable(get_dict):
            pairs.extend(f"{name}={value}" for name, value in get_dict().items() if name and value)
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
    requirements_proof: str = "",
) -> dict[str, Any]:
    # ``requirements_proof`` lets a caller reuse one proof across several flows,
    # which is what a real browser does on the password page.  Empty (the
    # default for a single flow) generates a fresh proof as before.
    proof = str(requirements_proof or "") or _requirements_token(device_id, profile)
    response = request_with_retry(
        session,
        "post",
        SENTINEL_REQ_URL,
        label="sentinel challenge",
        data=json.dumps({"p": proof, "id": device_id, "flow": flow}, separators=(",", ":")),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://sentinel.openai.com",
            "Referer": (f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={sentinel_version()}"),
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
    cookie_header: str = "",
    timeout_seconds: int = 60,
    requirements_proof: str = "",
) -> SentinelToken:
    """Issue one flow-bound token using the same session and fingerprint.

    ``cookie_header`` lets a caller that does not hold the issuing session (the
    checkout transports use ``curl_cffi``'s functional API) supply the account
    cookie verbatim so the challenge is bound to the same ``oai-did`` the
    checkout request will carry.
    """
    flow = str(flow or "").strip()
    device_id = str(device_id or "").strip() or str(uuid.uuid4())
    if flow not in FLOW_PAGE_URLS:
        raise SentinelIssueError(f"sentinel_flow_unsupported:{flow}")
    owned_session = session is None
    active_session = session or curl_requests.Session()
    normalized_proxy = normalize_proxy_url(proxy or "")
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
            requirements_proof=requirements_proof,
        )
        token = run_sentinel_sdk(
            challenge,
            flow=flow,
            device_id=device_id,
            profile=active_profile,
            cookie=_cookie_value(active_session, device_id, cookie_header),
            page_url=str(page_url or FLOW_PAGE_URLS[flow]),
            timeout_seconds=timeout_seconds,
        )
    except (SentinelRunnerError, SentinelIssueError):
        raise
    except Exception as exc:
        raise SentinelIssueError(f"sentinel_issue_failed:{_root_reason(exc, normalized_proxy)}") from exc
    finally:
        if owned_session:
            try:
                active_session.close()
            except Exception:
                pass
    try:
        parsed = json.loads(token)
    except (TypeError, ValueError) as exc:
        # The runner returned something that is not a JSON token object.  Name it
        # as a Sentinel failure instead of leaking a bare JSONDecodeError.
        raise SentinelIssueError(f"sentinel_sdk_returned_invalid_json:{flow}") from exc
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
    cookie_header: str = "",
    timeout_seconds: int = 60,
) -> SentinelToken:
    """Issue at a protocol step, with explicit legacy rollback compatibility."""
    supplied = _token_from_bundle(supplied_data, flow=flow, device_id=device_id)
    if supplied is not None:
        return supplied
    backend = sentinel_backend(config)
    fallback_error: BaseException | None = None
    if backend == "node_runner":
        try:
            return issue_sentinel_token(
                flow=flow,
                device_id=device_id,
                session=session,
                proxy=proxy,
                profile=profile,
                cookie_header=cookie_header,
                timeout_seconds=timeout_seconds,
            )
        except Exception as runner_error:
            if not _legacy_fallback_enabled(config):
                raise
            fallback_error = runner_error
            print(
                "  [Sentinel] Node runner failed; using configured legacy fallback "
                f"for {flow}: {_root_reason(runner_error, proxy)}"
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
        if fallback_error is not None:
            # Both issuers came up empty.  Naming only the legacy shortfall is
            # what made the 2026-09-17 incident read as an upstream channel
            # problem: the node runner had been failing on a corrupted vendored
            # asset (``SentinelBundleError: sentinel_runtime_hash_mismatch``),
            # the legacy issuer then produced no token either, and the batch was
            # reported as ``sentinel_legacy_incomplete`` for all 42 accounts that
            # reached ``create_account`` -- 126/126 failed and the real defect
            # stayed hidden behind two layers of degradation.  Carry the original
            # cause so it is visible without a second investigation.
            #
            # ``_root_reason`` (not ``type(...).__name__``) because the runner
            # failure arrives already wrapped as
            # ``SentinelIssueError("sentinel_issue_failed:SentinelBundleError")``
            # -- reporting only that wrapper's type would reproduce the very
            # blindness this branch exists to remove.
            raise SentinelIssueError(
                f"sentinel_fallback_incomplete:{flow}:{_root_reason(fallback_error, proxy)}"
            ) from fallback_error
        raise SentinelIssueError(f"sentinel_legacy_incomplete:{flow}")
    return legacy


def _cookie_value(session: Any, device_id: str, override: str) -> str:
    """Resolve the cookie sent to the Sentinel runner, keeping ``oai-did`` present."""
    value = str(override or "").strip()
    if not value:
        return _cookie_header(session, device_id)
    has_did = any(part.strip().lower().startswith("oai-did=") for part in value.split(";"))
    return value if has_did else f"oai-did={device_id}; {value}"


# ── Checkout Sentinel mint (single public authority) ───────────────────────
# The ChatGPT checkout-create gate needs the ``chatgpt_checkout`` token pair on
# every stage that can open a Checkout session.  The four call sites
# (``payment_capability``, ``paypal_extract._create_checkout``,
# ``wallet_transport`` and ``gcash_transport``) all reach the wire through
# ``paypal_extract._checkout_post``, so that one place attaches the pair.  This
# public helper is the authority they share; it mints through the same Node
# runner as registration and *never* degrades to the pure-Python legacy issuer
# (that issuer cannot produce ``chatgpt_checkout`` and is default-off anyway).
_CHECKOUT_MINT_CACHE: dict[tuple[str, ...], tuple[float, SentinelToken]] = {}
_CHECKOUT_MINT_CACHE_LOCK = threading.Lock()
#: Conservative TTL, under the server-side ~9 min token validity.
CHECKOUT_SENTINEL_CACHE_TTL_SECONDS = 480.0


def _checkout_mint_cache_key(
    device_id: str, proxy: str | None, cookie_header: str, profile: Mapping[str, Any] | None
) -> tuple[str, ...]:
    fp = profile if isinstance(profile, Mapping) else {}
    return (
        str(device_id or ""),
        str(proxy or ""),
        str(cookie_header or ""),
        str(fp.get("user_agent") or ""),
        str(fp.get("timezone") or ""),
        str(fp.get("lang") or fp.get("language") or fp.get("locale") or ""),
    )


def issue_checkout_sentinel(
    *,
    device_id: str,
    session: Any | None = None,
    proxy: str | None = None,
    profile: Mapping[str, Any] | None = None,
    cookie_header: str = "",
    timeout_seconds: int = 60,
) -> SentinelToken:
    """Issue the ``chatgpt_checkout`` token pair for the Checkout create gate.

    Only a successful mint is cached so a transient runner/network error does
    not poison the rest of the run.
    """
    key = _checkout_mint_cache_key(device_id, proxy, cookie_header, profile)
    now = time.monotonic()
    with _CHECKOUT_MINT_CACHE_LOCK:
        hit = _CHECKOUT_MINT_CACHE.get(key)
        if hit is not None and now - hit[0] < CHECKOUT_SENTINEL_CACHE_TTL_SECONDS:
            return hit[1]
    issued = issue_sentinel_flow(
        flow=CHECKOUT_SENTINEL_FLOW,
        device_id=device_id,
        session=session,
        proxy=proxy,
        profile=profile,
        cookie_header=cookie_header,
        timeout_seconds=timeout_seconds,
    )
    if issued.token:
        with _CHECKOUT_MINT_CACHE_LOCK:
            _CHECKOUT_MINT_CACHE[key] = (now, issued)
    return issued


def checkout_sentinel_headers(
    *,
    device_id: str,
    session: Any | None = None,
    proxy: str | None = None,
    profile: Mapping[str, Any] | None = None,
    cookie_header: str = "",
    timeout_seconds: int = 60,
) -> dict[str, str]:
    """Return the ``openai-sentinel-*`` header pair, or ``{}`` when unavailable.

    Advisory by design: a Checkout that is otherwise valid must not be aborted
    because the local runner is missing or slow.  Callers that need the failure
    to surface use :func:`issue_checkout_sentinel` directly.
    """
    issued = issue_checkout_sentinel(
        device_id=device_id,
        session=session,
        proxy=proxy,
        profile=profile,
        cookie_header=cookie_header,
        timeout_seconds=timeout_seconds,
    )
    headers: dict[str, str] = {}
    if issued.token:
        headers["OpenAI-Sentinel-Token"] = issued.token
    if issued.so_token:
        headers["OpenAI-Sentinel-SO-Token"] = issued.so_token
    return headers


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
    """Compatibility adapter returning the historical Sentinel bundle shape.

    All flows share **one** requirements proof (and the same fingerprint
    ``session_id``), reproducing the password-page iframe, which sends the same
    ``p`` for each flow instead of re-randomising it per flow.  The per-flow
    server challenge still differs, so each token stays flow-bound.
    """
    did = str(device_id or "").strip() or str(uuid.uuid4())
    owned_session = session is None
    active_session = session or curl_requests.Session()
    normalized_proxy = normalize_proxy_url(proxy or "")
    if normalized_proxy and owned_session:
        active_session.proxies = {"http": normalized_proxy, "https": normalized_proxy}
    active_profile = dict(profile or sentinel_fingerprint())
    active_profile.setdefault("session_id", str(uuid.uuid4()))
    issued: dict[str, SentinelToken] = {}
    cookie_str = ""
    try:
        shared_proof = _requirements_token(did, active_profile)
        for flow in flows:
            issued[flow] = issue_sentinel_token(
                flow=flow,
                device_id=did,
                session=active_session,
                profile=active_profile,
                timeout_seconds=timeout_seconds,
                requirements_proof=shared_proof,
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
    "CHECKOUT_SENTINEL_CACHE_TTL_SECONDS",
    "CHECKOUT_SENTINEL_FLOW",
    "FLOW_PAGE_URLS",
    "SentinelIssueError",
    "SentinelToken",
    "checkout_sentinel_headers",
    "issue_checkout_sentinel",
    "issue_sentinel_bundle",
    "issue_sentinel_flow",
    "issue_sentinel_token",
    "sentinel_backend",
]

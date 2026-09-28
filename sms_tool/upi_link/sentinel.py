from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT
except ImportError:
    from pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT  # type: ignore
from typing import Any
from collections.abc import Mapping
import secrets
import time
from ._extract import (
    _upi_find_submission_attempt,
    _upi_int_value,
)
from .constants import STRIPE_PAYMENT_PAGE_GET_URL_T, UPI_CHATGPT_CLIENT_BUILD_NUMBER, UPI_CHATGPT_CLIENT_VERSION, UPI_SENTINEL_APPROVAL_FLOW, UPI_SENTINEL_PING_URL, UPI_WARMUP_TIMEOUT
from .env import _emit
from .session import _upi_account_id_from_token, _upi_apply_fingerprint, _upi_attestation_deploy_id, _upi_env_attestation, _upi_fingerprint, _upi_scrape_page_identity, _upi_session_is_live, _upi_warmup_session
from .browser import _upi_browser_capture
from .stripe import _upi_elements_session_params


class _UpiRiskContext:
    """Protocol-only ChatGPT risk context (reference git5 rail).

    Holds the live deployment attestation, the current client build and one
    rotating signed-observation value plus a coherent ``oai-telemetry``
    sequence for the whole attempt. It is deliberately browser-free: the
    ChatGPT checkout rejection (HTTP 400 unusual activity) is answered with the
    same server-issued attestation and observation headers a real frontend
    sends, not with a headless browser.
    """

    def __init__(self) -> None:
        self.attestation = ""
        self.client_version = UPI_CHATGPT_CLIENT_VERSION
        self.client_build = UPI_CHATGPT_CLIENT_BUILD_NUMBER
        self.observation = ""
        self.browser_observation = False
        self.stripe_ids: dict[str, str] = {}
        self.sentinel_tokens: dict[str, str] = {}
        self._started_at = 0.0
        self._seq = 0
        self._counter_b = 0
        self._counter_c = 0

    def apply_capture(self, capture: Any) -> bool:
        """Merge a browser capture; True when it supplied an observation.

        A browser-issued observation is stage-specific and must be forwarded
        verbatim, so ``browser_observation`` makes :meth:`headers` reuse it
        instead of rotating a synthetic protocol value.
        """
        if not isinstance(capture, Mapping):
            return False
        attestation = str(capture.get("attestation") or "").strip()
        if attestation and "." in attestation:
            self.attestation = attestation
            deploy = _upi_attestation_deploy_id(attestation)
            if deploy:
                self.client_version = f"prod-{deploy}"
        observation = str(capture.get("observation") or "").strip()
        used = False
        if observation:
            self.observation = observation
            self.browser_observation = True
            used = True
        version = str(capture.get("client_version") or "").strip()
        if version:
            self.client_version = version
        build = str(capture.get("client_build") or "").strip()
        if build:
            self.client_build = build
        captured_ids = capture.get("stripe_ids")
        if isinstance(captured_ids, Mapping):
            for key in ("guid", "muid", "sid", "stripe_js_id"):
                value = str(captured_ids.get(key) or "").strip()
                if value:
                    self.stripe_ids[key] = value
        token = str(capture.get("sentinel_token") or "").strip()
        token_flow = str(capture.get("sentinel_flow") or "").strip()
        if token and token_flow:
            self.sentinel_tokens[token_flow] = token
        return used

    def observation_header(self) -> str:
        if self.browser_observation and self.observation:
            return self.observation
        return self.rotate_observation()

    def rotate_observation(self) -> str:
        self.observation = "v1.r.p." + secrets.token_urlsafe(12)[:16]
        return self.observation

    def telemetry(self) -> str:
        now = time.monotonic()
        if not self._started_at:
            self._started_at = now
        self._seq += 1
        if not self._counter_b or not self._counter_c:
            self._counter_b = secrets.randbelow(211) + 30
            self._counter_c = secrets.randbelow(81) + 10
        elapsed = round(max(120.0, (now - self._started_at) * 1000.0) + secrets.randbelow(41), 4)
        total = round(elapsed + secrets.randbelow(9), 1)
        return f"[1,{elapsed},{min(self._seq, 60)},{self._counter_b},{self._counter_c},2,0,{total}]"

    def headers(self, *, account_id: str = "") -> dict[str, str]:
        headers = {
            "oai-client-version": self.client_version,
            "oai-client-build-number": self.client_build,
            "oai-telemetry": self.telemetry(),
            "x-openai-web-frontend": "core_web",
            "x-openai-codex-window-type": "not_applicable",
            "x-oai-is-pending-updates": '{"v":3,"updates":[]}',
            "x-oai-is-client-observation": self.observation_header(),
        }
        if self.attestation:
            headers["oai-web-deployment-attestation"] = self.attestation
        if account_id:
            headers["Chatgpt-Account-Id"] = account_id
        return headers
def _upi_capture_risk_context(
    session: Any,
    *,
    access_token: Any,
    device_id: Any,
    page_url: Any,
    fingerprint: Any,
    proxy: Any = "",
    session_token: Any = "",
    use_browser: bool = False,
    sentinel_flow: Any = "",
) -> _UpiRiskContext:
    """Warm the session and capture the risk context.

    Prefers the headless browser rail when requested and available: only a
    real browser emits the signed ``x-oai-is-client-observation`` value the
    ChatGPT approval gate validates. Protocol scrapes remain the fallback so a
    missing Playwright or a blocked page never aborts the attempt.
    """
    risk = _UpiRiskContext()
    risk.attestation = _upi_env_attestation()
    if not _upi_session_is_live(session):
        return risk
    if use_browser:
        capture = _upi_browser_capture(
            access_token=access_token,
            session_token=session_token,
            device_id=device_id,
            proxy=proxy,
            fingerprint=fingerprint,
            page_url=page_url,
            sentinel_flow=sentinel_flow,
        )
        used = risk.apply_capture(capture)
        _emit(
            "risk",
            "browser rail: attestation=%s observation=%s error=%s"
            % (
                "yes" if risk.attestation else "no",
                "yes" if used else "no",
                str(capture.get("error") or "none")[:90],
            ),
        )
        if risk.attestation or used:
            return risk
        _emit("risk", "browser rail unavailable; falling back to protocol scrapes")
    html = _upi_warmup_session(session, device_id=device_id, page_url=page_url, fingerprint=fingerprint)
    version, build, attestation = _upi_scrape_page_identity(html)
    if version:
        risk.client_version = version
    if build:
        risk.client_build = build
    if attestation:
        risk.attestation = attestation
    deploy = _upi_attestation_deploy_id(risk.attestation)
    if deploy:
        risk.client_version = f"prod-{deploy}"
    risk.rotate_observation()
    _emit(
        "risk",
        "protocol risk context: attestation=%s build=%s" % ("yes" if risk.attestation else "no", risk.client_build),
    )
    return risk
def _upi_sentinel_ping(session: Any, *, proxy: Any, referer: Any) -> None:
    """Best-effort Sentinel connectivity ping (failure is never fatal)."""
    if not _upi_session_is_live(session) or not hasattr(session, "post"):
        return
    try:
        session.post(
            UPI_SENTINEL_PING_URL,
            json={},
            headers={
                "Referer": referer,
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=UPI_WARMUP_TIMEOUT,
        )
    except Exception:
        pass
def _upi_sentinel_headers(
    session: Any,
    device_id: Any,
    proxy: Any,
    *,
    flow: str = UPI_SENTINEL_APPROVAL_FLOW,
    supplied_token: str = "",
) -> dict[str, str]:
    """Mint one Sentinel token bound to this flow and session.

    Advisory only: a missing or malformed token must not abort an otherwise
    valid Checkout session, so every failure degrades to an empty header set
    and lets the upstream risk layer classify the response. Mirrors
    ``paypal_extract._fresh_approval_sentinel`` so checkout/approval requests
    carry the same risk attestation the browser flow does.
    """
    if not device_id or not _upi_session_is_live(session):
        return {}
    if str(supplied_token or "").strip():
        # Browser-issued token: never substitute a Node-minted one, because the
        # risk engine correlates it with the observed browser session.
        return {"OpenAI-Sentinel-Token": str(supplied_token).strip()}
    try:
        from ..sentinel import issue_sentinel_flow

        issued = issue_sentinel_flow(flow=flow, device_id=device_id, session=session, proxy=proxy)
    except Exception as exc:
        _emit("sentinel", f"Sentinel unavailable (non-fatal): {type(exc).__name__}: {str(exc)[:120]}")
        return {}
    headers: dict[str, str] = {}
    if issued.token:
        headers["OpenAI-Sentinel-Token"] = issued.token
    if issued.so_token:
        headers["OpenAI-Sentinel-SO-Token"] = issued.so_token
    if headers:
        _emit("sentinel", f"{flow} Sentinel ready (len={len(issued.token)})")
    return headers
def _upi_fetch_oaics_state(
    session: Any,
    access_token: Any,
    *,
    cs_id: Any,
    processor_entity: Any,
    proxy: Any,
) -> dict[str, Any]:
    """Refresh an ``oaics_`` Checkout state before building Elements.

    Aligned with the reference ``_fetch_upi_oaics_state``: an ``oaics_`` create
    response may not yet carry its wallet method or the CustomerSession secret,
    so read the authenticated Checkout state once through the same ChatGPT
    session (same cookies, device id and India egress).
    """
    if not str(cs_id or "").startswith("oaics_"):
        return {}
    if not _upi_session_is_live(session) or not hasattr(session, "get"):
        return {}
    route = f"/backend-api/payments/checkout/{processor_entity}/{cs_id}"
    try:
        response = session.get(
            "https://chatgpt.com" + route,
            headers={
                "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
                "x-openai-target-path": route,
                "x-openai-target-route": route,
            },
            timeout=CHATGPT_TIMEOUT,
        )
    except Exception as exc:
        _emit("oaics", f"state refresh failed (non-fatal): {type(exc).__name__}")
        return {}
    if _upi_int_value(getattr(response, "status_code", 0))[0] >= 400:
        return {}
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
def _upi_wait_paid(
    stripe: Any,
    *,
    cs_id: Any,
    stripe_pk: Any,
    ctx: Any,
    timeout: Any,
    poll_interval: float = 3.0,
) -> dict[str, Any]:
    """Keep polling the Checkout session until it is paid or the deadline passes.

    Aligned with the reference ``_wait_upi_paid`` intent: the generated QR is
    short-lived and the operator may scan it after the link is returned, so
    ``--wait-paid`` holds the same session and reports the terminal status
    instead of returning an unverified link.
    """
    if not hasattr(stripe, "get"):
        return {"paid": False, "payment_status": "unsupported"}
    deadline = time.monotonic() + max(30, _upi_int_value(timeout)[0] or 900)
    params = {
        key: value
        for key, value in _upi_elements_session_params(ctx).items()
        if key.startswith("elements_session_client")
    }
    params["key"] = stripe_pk
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            response = stripe.get(
                STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                params=params,
                timeout=DEFAULT_TIMEOUT,
            )
            last = response.json() or {}
        except Exception:
            time.sleep(poll_interval)
            continue
        submission = _upi_find_submission_attempt(last)
        state = str(submission.get("state") or "").strip().lower()
        setup_intent = last.get("setup_intent") if isinstance(last, dict) else None
        intent_status = (
            str(setup_intent.get("status") or "").strip().lower() if isinstance(setup_intent, Mapping) else ""
        )
        if state == "succeeded" or intent_status == "succeeded":
            return {"paid": True, "payment_status": state or intent_status}
        time.sleep(poll_interval)
    submission = _upi_find_submission_attempt(last)
    return {"paid": False, "payment_status": str(submission.get("state") or "timeout")}
def _upi_apply_approve_risk(
    session: Any,
    risk: Any,
    access_token: Any,
    device_id: Any,
    proxy: Any,
    fingerprint: Any,
    payment_country: Any,
) -> None:
    """Refresh approval-stage risk headers and Sentinel on the session.

    The ChatGPT ``checkout/approve`` endpoint answers ``result=blocked``
    unless the request carries a fresh signed observation, the deployment
    attestation and a newly minted ``checkout_session_approval`` Sentinel.
    Replaying the same context (only swapping the UA) therefore keeps failing,
    so every retry rotates the whole context instead.
    """
    if session is None:
        return
    try:
        session.headers.update(risk.headers(account_id=_upi_account_id_from_token(access_token)))
    except Exception:
        pass
    try:
        session.headers.update(
            _upi_sentinel_headers(
                session,
                device_id,
                proxy,
                flow=UPI_SENTINEL_APPROVAL_FLOW,
                supplied_token=risk.sentinel_tokens.get(UPI_SENTINEL_APPROVAL_FLOW, ""),
            )
        )
    except Exception:
        pass
    if payment_country:
        try:
            _upi_apply_fingerprint(session, _upi_fingerprint(country=payment_country))
        except Exception:
            pass

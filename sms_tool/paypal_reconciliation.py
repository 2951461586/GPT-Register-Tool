"""Reconcile the merchant return chain after PayPal authorization.

This module is deliberately separate from payment-link extraction.  It follows
only the observed merchant return path::

    pm-redirects.stripe.com -> pay.openai.com -> chatgpt.com/checkout/verify

The caller supplies an HTTP transport (for example, an authenticated
``requests.Session``).  Results contain only normalized hosts, stages, status
codes, and booleans; bearer-like query values and complete URLs are never
returned or persisted by this module.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qs, unquote, urljoin, urlsplit


class ReconciliationClassification(str, Enum):
    """Whether reconciliation produced an answer or could be completed."""

    CONCLUSIVE = "conclusive"
    UNKNOWN = "unknown"
    FAILED = "failed"


class PaymentOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ReturnStage(str, Enum):
    STRIPE_RETURN = "stripe_return"
    OPENAI_PAY = "openai_pay"
    CHECKOUT_VERIFY = "checkout_verify"
    CHATGPT_LANDING = "chatgpt_landing"


class RemoteStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ReconciliationTransport(Protocol):
    """Minimal transport accepted by :func:`reconcile_paypal_return`."""

    def get(
        self,
        url: str,
        *,
        timeout: float,
        allow_redirects: bool,
    ) -> Any:
        ...


class ReturnURLValidationError(ValueError):
    """A safe validation error which never includes the rejected URL."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedReturnState:
    """Secret-free state extracted from one allowlisted return URL."""

    stage: ReturnStage
    host: str
    redirect_status: RemoteStatus
    stripe_return_status: RemoteStatus
    has_setup_intent: bool
    has_client_secret: bool
    has_success_return_url: bool
    success_return_stage: Optional[ReturnStage]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "host": self.host,
            "redirect_status": self.redirect_status.value,
            "stripe_return_status": self.stripe_return_status.value,
            "has_setup_intent": self.has_setup_intent,
            "has_client_secret": self.has_client_secret,
            "has_success_return_url": self.has_success_return_url,
            "success_return_stage": (
                self.success_return_stage.value if self.success_return_stage else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationHop:
    """A secret-free observation of one HTTP request."""

    index: int
    stage: ReturnStage
    host: str
    status_code: Optional[int]
    response_state: RemoteStatus = RemoteStatus.UNKNOWN
    next_stage: Optional[ReturnStage] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "stage": self.stage.value,
            "host": self.host,
            "status_code": self.status_code,
            "response_state": self.response_state.value,
            "next_stage": self.next_stage.value if self.next_stage else None,
        }


@dataclass(frozen=True, slots=True)
class PayPalReconciliationResult:
    """Typed, secret-free result for a single merchant return chain."""

    classification: ReconciliationClassification
    outcome: PaymentOutcome
    retryable: bool
    error_stage: Optional[str]
    error_code: Optional[str]
    reason: str
    final_stage: Optional[ReturnStage]
    redirect_status: RemoteStatus
    stripe_return_status: RemoteStatus
    observed_setup_intent: bool
    observed_client_secret: bool
    observed_success_return_url: bool
    hops: tuple[ReconciliationHop, ...]

    @property
    def conclusive(self) -> bool:
        return self.classification is ReconciliationClassification.CONCLUSIVE

    @property
    def ok(self) -> bool:
        return self.conclusive and self.outcome is PaymentOutcome.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready object containing no full URL or token value."""

        return {
            "ok": self.ok,
            "conclusive": self.conclusive,
            "classification": self.classification.value,
            "outcome": self.outcome.value,
            "retryable": self.retryable,
            "error_stage": self.error_stage,
            "error_code": self.error_code,
            "reason": self.reason,
            "final_stage": self.final_stage.value if self.final_stage else None,
            "redirect_status": self.redirect_status.value,
            "stripe_return_status": self.stripe_return_status.value,
            "observed_setup_intent": self.observed_setup_intent,
            "observed_client_secret": self.observed_client_secret,
            "observed_success_return_url": self.observed_success_return_url,
            "hops": [hop.to_dict() for hop in self.hops],
        }


_ALLOWED_HOSTS = frozenset(
    {
        "pm-redirects.stripe.com",
        "pay.openai.com",
        "chatgpt.com",
    }
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_HTTP_CODES = frozenset({408, 425, 429})
_MAX_URL_LENGTH = 16_384
_MAX_BODY_LENGTH = 262_144

_SUCCESS_MARKERS = (
    "payment was successful",
    "payment successful",
    "plus is active",
    "subscription is active",
    "you're all set",
    "you are all set",
)
_FAILURE_MARKERS = (
    "payment failed",
    "payment was not successful",
    "card was declined",
    "setup intent failed",
    "setup_intent_failed",
)
_CANCEL_MARKERS = (
    "payment cancelled",
    "payment canceled",
    "checkout cancelled",
    "checkout canceled",
)
_PROCESSING_MARKERS = (
    "processing payment",
    "processing your payment",
    "payment is being processed",
    "please wait while we process",
)

_URL_VALUE_KEYS = frozenset(
    {
        "url",
        "href",
        "location",
        "redirect_url",
        "return_url",
        "success_return_url",
        "verification_url",
    }
)
_CONTAINER_KEYS = (
    "result",
    "data",
    "payload",
    "b_layer",
    "merchant",
    "billing",
    "authorize",
)
_INPUT_URL_KEYS = (
    "return_url",
    "returnURL",
    "returnUrl",
    "final_redirect_url",
    "redirect_url",
    "success_return_url",
    "verification_url",
)


class _CandidateHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.text_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() in {"a", "link"} and values.get("href"):
            self.urls.append(values["href"])
        if tag.lower() == "meta" and values.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)$", values.get("content", ""), re.I)
            if match:
                self.urls.append(match.group(1).strip().strip("'\""))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data and len(self.text_parts) < 2_000:
            self.text_parts.append(data)


@dataclass(slots=True)
class _Evidence:
    redirect_status: RemoteStatus = RemoteStatus.UNKNOWN
    stripe_return_status: RemoteStatus = RemoteStatus.UNKNOWN
    response_status: RemoteStatus = RemoteStatus.UNKNOWN
    observed_setup_intent: bool = False
    observed_client_secret: bool = False
    observed_success_return_url: bool = False
    reached_verify: bool = False
    left_verify: bool = False

    def observe_url(self, state: NormalizedReturnState) -> None:
        self.redirect_status = _merge_status(self.redirect_status, state.redirect_status)
        self.stripe_return_status = _merge_status(
            self.stripe_return_status, state.stripe_return_status
        )
        self.observed_setup_intent = self.observed_setup_intent or state.has_setup_intent
        self.observed_client_secret = self.observed_client_secret or state.has_client_secret
        self.observed_success_return_url = (
            self.observed_success_return_url or state.has_success_return_url
        )
        self.reached_verify = self.reached_verify or state.stage is ReturnStage.CHECKOUT_VERIFY

    def observe_response(self, status: RemoteStatus) -> None:
        self.response_status = _merge_status(self.response_status, status)

    def terminal_outcome(self) -> Optional[PaymentOutcome]:
        for status in (
            self.response_status,
            self.redirect_status,
            self.stripe_return_status,
        ):
            if status is RemoteStatus.FAILED:
                return PaymentOutcome.FAILED
            if status is RemoteStatus.CANCELLED:
                return PaymentOutcome.CANCELLED
        return None

    def has_success_evidence(self) -> bool:
        return (
            self.response_status is RemoteStatus.SUCCEEDED
            or self.redirect_status is RemoteStatus.SUCCEEDED
        )


def normalize_return_state(url: str) -> NormalizedReturnState:
    """Validate one return URL and expose only its normalized state.

    ``ReturnURLValidationError`` messages are intentionally generic so callers
    can log them without leaking query parameters.
    """

    parsed, stage = _validate_return_url(url)
    query = _query(parsed.query)
    redirect_status = _normalize_remote_status(_first(query, "redirect_status"))
    stripe_return_status = RemoteStatus.UNKNOWN
    if stage is ReturnStage.STRIPE_RETURN:
        stripe_return_status = _normalize_remote_status(_first(query, "status"))

    success_return_url = _first(query, "success_return_url")
    success_return_stage: Optional[ReturnStage] = None
    if success_return_url:
        _, success_return_stage = _validate_return_url(success_return_url)
        if success_return_stage not in {
            ReturnStage.CHECKOUT_VERIFY,
            ReturnStage.CHATGPT_LANDING,
        }:
            raise ReturnURLValidationError(
                "invalid_success_return_url",
                "success return URL does not target an allowed ChatGPT route",
            )

    return NormalizedReturnState(
        stage=stage,
        host=str(parsed.hostname or "").lower(),
        redirect_status=redirect_status,
        stripe_return_status=stripe_return_status,
        has_setup_intent=bool(_first(query, "setup_intent", "setup_intent_id")),
        has_client_secret=bool(
            _first(query, "setup_intent_client_secret", "client_secret")
        ),
        has_success_return_url=bool(success_return_url),
        success_return_stage=success_return_stage,
    )


def reconcile_paypal_return(
    source: str | Mapping[str, Any],
    *,
    transport: ReconciliationTransport,
    max_hops: int = 8,
    timeout: float = 20.0,
) -> PayPalReconciliationResult:
    """Follow and classify a PayPal merchant return chain.

    The transport must not auto-follow redirects; this function always passes
    ``allow_redirects=False`` and validates every hop before requesting it.
    """

    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 1:
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code="invalid_max_hops",
            reason="max_hops must be a positive integer",
        )
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code="invalid_timeout",
            reason="timeout must be positive",
        )
    if transport is None or not callable(getattr(transport, "get", None)):
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code="invalid_transport",
            reason="transport must provide get()",
        )

    evidence = _Evidence()
    _observe_mapping(source, evidence)
    start_url = _extract_start_url(source)
    if not start_url:
        terminal = evidence.terminal_outcome()
        if terminal is not None:
            return _make_result(
                ReconciliationClassification.CONCLUSIVE,
                terminal,
                retryable=False,
                error_stage="merchant_return",
                error_code=(
                    "remote_payment_cancelled"
                    if terminal is PaymentOutcome.CANCELLED
                    else "remote_payment_failed"
                ),
                reason="merchant return reported a terminal payment state",
                evidence=evidence,
            )
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code="missing_return_url",
            reason="no merchant return URL was provided",
            evidence=evidence,
        )

    try:
        current_state = normalize_return_state(start_url)
    except ReturnURLValidationError as exc:
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code=exc.code,
            reason=str(exc),
            evidence=evidence,
        )
    if current_state.stage is ReturnStage.CHATGPT_LANDING:
        return _make_result(
            ReconciliationClassification.FAILED,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage="input_validation",
            error_code="invalid_start_stage",
            reason="merchant reconciliation cannot start from a landing page",
            final_stage=current_state.stage,
            evidence=evidence,
        )

    current = start_url
    hops: list[ReconciliationHop] = []
    seen: set[bytes] = set()

    for index in range(1, max_hops + 1):
        try:
            current_state = normalize_return_state(current)
        except ReturnURLValidationError as exc:
            return _make_result(
                ReconciliationClassification.FAILED,
                PaymentOutcome.UNKNOWN,
                retryable=False,
                error_stage="redirect_validation",
                error_code=exc.code,
                reason=str(exc),
                final_stage=current_state.stage if current_state else None,
                hops=hops,
                evidence=evidence,
            )

        fingerprint = _url_fingerprint(current)
        if fingerprint in seen:
            return _make_result(
                ReconciliationClassification.FAILED,
                PaymentOutcome.UNKNOWN,
                retryable=False,
                error_stage="redirect_chain",
                error_code="redirect_loop",
                reason="merchant return chain contains a redirect loop",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )
        seen.add(fingerprint)
        evidence.observe_url(current_state)

        terminal = evidence.terminal_outcome()
        if terminal is not None:
            return _terminal_remote_result(terminal, current_state.stage, hops, evidence)

        try:
            response = transport.get(
                current,
                timeout=float(timeout),
                allow_redirects=False,
            )
        except Exception as exc:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=None,
                )
            )
            return _make_result(
                ReconciliationClassification.UNKNOWN,
                PaymentOutcome.UNKNOWN,
                retryable=True,
                error_stage=current_state.stage.value,
                error_code="transport_error",
                reason=f"transport raised {_safe_exception_name(exc)}",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )

        status_code = _status_code(response)
        if status_code is None:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=None,
                )
            )
            return _make_result(
                ReconciliationClassification.FAILED,
                PaymentOutcome.UNKNOWN,
                retryable=False,
                error_stage=current_state.stage.value,
                error_code="invalid_transport_response",
                reason="transport response has no valid HTTP status",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )

        body = _response_text(response)
        response_state = _status_from_body(body)
        evidence.observe_response(response_state)
        terminal = evidence.terminal_outcome()
        if terminal is not None:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=status_code,
                    response_state=response_state,
                )
            )
            return _terminal_remote_result(terminal, current_state.stage, hops, evidence)

        if status_code in _RETRYABLE_HTTP_CODES or status_code >= 500:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=status_code,
                    response_state=response_state,
                )
            )
            return _make_result(
                ReconciliationClassification.UNKNOWN,
                PaymentOutcome.UNKNOWN,
                retryable=True,
                error_stage=current_state.stage.value,
                error_code="transient_http_error",
                reason="merchant return endpoint reported a transient HTTP error",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )
        if status_code in {401, 403}:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=status_code,
                    response_state=response_state,
                )
            )
            return _make_result(
                ReconciliationClassification.UNKNOWN,
                PaymentOutcome.UNKNOWN,
                retryable=False,
                error_stage=current_state.stage.value,
                error_code="authentication_required",
                reason="merchant return endpoint requires an authenticated session",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )
        if 400 <= status_code:
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=status_code,
                    response_state=response_state,
                )
            )
            return _make_result(
                ReconciliationClassification.FAILED,
                PaymentOutcome.UNKNOWN,
                retryable=False,
                error_stage=current_state.stage.value,
                error_code="merchant_http_error",
                reason="merchant return endpoint reported a non-retryable HTTP error",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )

        location = _header(response, "location")
        next_url = ""
        if status_code in _REDIRECT_CODES:
            if not location:
                hops.append(
                    ReconciliationHop(
                        index=index,
                        stage=current_state.stage,
                        host=current_state.host,
                        status_code=status_code,
                        response_state=response_state,
                    )
                )
                return _make_result(
                    ReconciliationClassification.UNKNOWN,
                    PaymentOutcome.UNKNOWN,
                    retryable=True,
                    error_stage=current_state.stage.value,
                    error_code="redirect_location_missing",
                    reason="merchant redirect response did not include a location",
                    final_stage=current_state.stage,
                    hops=hops,
                    evidence=evidence,
                )
            next_url = urljoin(current, location)
            try:
                next_state = normalize_return_state(next_url)
                _validate_transition(current_state.stage, next_state.stage)
            except ReturnURLValidationError as exc:
                hops.append(
                    ReconciliationHop(
                        index=index,
                        stage=current_state.stage,
                        host=current_state.host,
                        status_code=status_code,
                        response_state=response_state,
                    )
                )
                return _make_result(
                    ReconciliationClassification.FAILED,
                    PaymentOutcome.UNKNOWN,
                    retryable=False,
                    error_stage="redirect_validation",
                    error_code=exc.code,
                    reason=str(exc),
                    final_stage=current_state.stage,
                    hops=hops,
                    evidence=evidence,
                )
        else:
            next_url, next_state = _pick_body_or_nested_hop(
                current,
                current_state,
                body,
            )

        if next_url:
            evidence.observe_url(next_state)
            if (
                current_state.stage is ReturnStage.CHECKOUT_VERIFY
                and next_state.stage is ReturnStage.CHATGPT_LANDING
            ):
                evidence.left_verify = True
            hops.append(
                ReconciliationHop(
                    index=index,
                    stage=current_state.stage,
                    host=current_state.host,
                    status_code=status_code,
                    response_state=response_state,
                    next_stage=next_state.stage,
                )
            )
            terminal = evidence.terminal_outcome()
            if terminal is not None:
                return _terminal_remote_result(terminal, next_state.stage, hops, evidence)
            if next_state.stage is ReturnStage.CHATGPT_LANDING:
                if evidence.has_success_evidence():
                    return _success_result(next_state.stage, hops, evidence)
                return _make_result(
                    ReconciliationClassification.UNKNOWN,
                    PaymentOutcome.UNKNOWN,
                    retryable=True,
                    error_stage="checkout_verify",
                    error_code="landing_without_success_evidence",
                    reason="checkout verification exited without a terminal payment status",
                    final_stage=next_state.stage,
                    hops=hops,
                    evidence=evidence,
                )
            if _url_fingerprint(next_url) in seen:
                return _make_result(
                    ReconciliationClassification.FAILED,
                    PaymentOutcome.UNKNOWN,
                    retryable=False,
                    error_stage="redirect_chain",
                    error_code="redirect_loop",
                    reason="merchant return chain contains a redirect loop",
                    final_stage=next_state.stage,
                    hops=hops,
                    evidence=evidence,
                )
            current = next_url
            current_state = next_state
            continue

        hops.append(
            ReconciliationHop(
                index=index,
                stage=current_state.stage,
                host=current_state.host,
                status_code=status_code,
                response_state=response_state,
            )
        )
        if (
            current_state.stage is ReturnStage.CHECKOUT_VERIFY
            and response_state is RemoteStatus.SUCCEEDED
        ):
            return _success_result(current_state.stage, hops, evidence)
        if response_state is RemoteStatus.PENDING or (
            current_state.stage in {ReturnStage.OPENAI_PAY, ReturnStage.CHECKOUT_VERIFY}
            and (
                evidence.redirect_status is RemoteStatus.PENDING
                or response_state is RemoteStatus.UNKNOWN
            )
        ):
            return _make_result(
                ReconciliationClassification.UNKNOWN,
                PaymentOutcome.UNKNOWN,
                retryable=True,
                error_stage=current_state.stage.value,
                error_code="payment_pending",
                reason="merchant return has not reached a terminal payment state",
                final_stage=current_state.stage,
                hops=hops,
                evidence=evidence,
            )
        return _make_result(
            ReconciliationClassification.UNKNOWN,
            PaymentOutcome.UNKNOWN,
            retryable=False,
            error_stage=current_state.stage.value,
            error_code="terminal_evidence_missing",
            reason="merchant return ended without terminal payment evidence",
            final_stage=current_state.stage,
            hops=hops,
            evidence=evidence,
        )

    return _make_result(
        ReconciliationClassification.UNKNOWN,
        PaymentOutcome.UNKNOWN,
        retryable=False,
        error_stage="redirect_chain",
        error_code="max_hops_exceeded",
        reason="merchant return chain exceeded the configured hop limit",
        final_stage=current_state.stage,
        hops=hops,
        evidence=evidence,
    )


def _validate_return_url(url: str) -> tuple[Any, ReturnStage]:
    value = str(url or "").strip()
    if not value:
        raise ReturnURLValidationError("empty_url", "merchant return URL is empty")
    if len(value) > _MAX_URL_LENGTH:
        raise ReturnURLValidationError("url_too_long", "merchant return URL is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ReturnURLValidationError("invalid_url", "merchant return URL is malformed")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ReturnURLValidationError("invalid_url", "merchant return URL is malformed") from None
    if parsed.scheme.lower() != "https":
        raise ReturnURLValidationError("https_required", "merchant return URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ReturnURLValidationError("userinfo_forbidden", "merchant return URL cannot contain userinfo")
    if port not in {None, 443}:
        raise ReturnURLValidationError("port_forbidden", "merchant return URL uses a disallowed port")
    host = str(parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ReturnURLValidationError("host_not_allowed", "merchant return URL host is not allowed")
    path = unquote(parsed.path or "/")
    if "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ReturnURLValidationError("invalid_path", "merchant return URL path is malformed")

    if host == "pm-redirects.stripe.com":
        if not (
            path in {"/return", "/authorize"}
            or path.startswith("/return/")
            or path.startswith("/authorize/")
        ):
            raise ReturnURLValidationError(
                "path_not_allowed", "Stripe return URL path is not allowed"
            )
        return parsed, ReturnStage.STRIPE_RETURN
    if host == "pay.openai.com":
        pay_id = path.removeprefix("/c/pay/") if path.startswith("/c/pay/") else ""
        if not pay_id or "/" in pay_id or pay_id in {".", ".."}:
            raise ReturnURLValidationError(
                "path_not_allowed", "OpenAI Pay return URL path is not allowed"
            )
        return parsed, ReturnStage.OPENAI_PAY
    normalized_path = path.rstrip("/") or "/"
    if normalized_path == "/checkout/verify":
        return parsed, ReturnStage.CHECKOUT_VERIFY
    if normalized_path == "/":
        return parsed, ReturnStage.CHATGPT_LANDING
    raise ReturnURLValidationError(
        "path_not_allowed", "ChatGPT return URL path is not allowed"
    )


def _validate_transition(current: ReturnStage, target: ReturnStage) -> None:
    allowed = {
        ReturnStage.STRIPE_RETURN: {
            ReturnStage.STRIPE_RETURN,
            ReturnStage.OPENAI_PAY,
            ReturnStage.CHECKOUT_VERIFY,
        },
        ReturnStage.OPENAI_PAY: {
            ReturnStage.OPENAI_PAY,
            ReturnStage.CHECKOUT_VERIFY,
        },
        ReturnStage.CHECKOUT_VERIFY: {
            ReturnStage.CHECKOUT_VERIFY,
            ReturnStage.CHATGPT_LANDING,
        },
        ReturnStage.CHATGPT_LANDING: {ReturnStage.CHATGPT_LANDING},
    }
    if target not in allowed[current]:
        raise ReturnURLValidationError(
            "invalid_stage_transition",
            "merchant return URL attempted an invalid stage transition",
        )


def _query(raw_query: str) -> dict[str, list[str]]:
    try:
        return parse_qs(raw_query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise ReturnURLValidationError(
            "invalid_query", "merchant return URL query is malformed"
        ) from None


def _first(query: Mapping[str, Sequence[str]], *keys: str) -> str:
    for key in keys:
        values = query.get(key)
        if values:
            return str(values[0] or "").strip()
    return ""


def _normalize_remote_status(value: Any) -> RemoteStatus:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"success", "succeeded", "complete", "completed", "paid"}:
        return RemoteStatus.SUCCEEDED
    if normalized in {"failed", "failure", "error", "declined", "requires_payment_method"}:
        return RemoteStatus.FAILED
    if normalized in {"cancelled", "canceled", "cancel", "aborted"}:
        return RemoteStatus.CANCELLED
    if normalized in {"pending", "processing", "requires_action", "in_progress"}:
        return RemoteStatus.PENDING
    return RemoteStatus.UNKNOWN


def _merge_status(current: RemoteStatus, observed: RemoteStatus) -> RemoteStatus:
    if observed in {RemoteStatus.FAILED, RemoteStatus.CANCELLED}:
        return observed
    if current in {RemoteStatus.FAILED, RemoteStatus.CANCELLED}:
        return current
    if observed is RemoteStatus.SUCCEEDED:
        return observed
    if current is RemoteStatus.SUCCEEDED:
        return current
    if observed is RemoteStatus.PENDING:
        return observed
    return current


def _known_mappings(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    queue: list[tuple[Mapping[str, Any], int]] = [(source, 0)]
    seen: set[int] = set()
    while queue:
        current, depth = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(current)
        if depth >= 4:
            continue
        for key in _CONTAINER_KEYS:
            nested = current.get(key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
    return output


def _url_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("href", "url", "value"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _extract_start_url(source: str | Mapping[str, Any]) -> str:
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, Mapping):
        return ""
    mappings = _known_mappings(source)
    for key in _INPUT_URL_KEYS:
        for current in mappings:
            value = _url_value(current.get(key))
            if value:
                return value
    return ""


def _observe_mapping(source: str | Mapping[str, Any], evidence: _Evidence) -> None:
    if not isinstance(source, Mapping):
        return
    for current in _known_mappings(source):
        evidence.redirect_status = _merge_status(
            evidence.redirect_status,
            _normalize_remote_status(current.get("redirect_status")),
        )
        evidence.stripe_return_status = _merge_status(
            evidence.stripe_return_status,
            _normalize_remote_status(current.get("stripe_return_status")),
        )
        evidence.observed_setup_intent = evidence.observed_setup_intent or bool(
            current.get("setup_intent") or current.get("setup_intent_id")
        )
        evidence.observed_client_secret = evidence.observed_client_secret or bool(
            current.get("setup_intent_client_secret") or current.get("client_secret")
        )
        evidence.observed_success_return_url = (
            evidence.observed_success_return_url
            or bool(current.get("success_return_url") or current.get("verification_url"))
        )
        for key in _INPUT_URL_KEYS:
            candidate = _url_value(current.get(key))
            if not candidate:
                continue
            try:
                evidence.observe_url(normalize_return_state(candidate))
            except ReturnURLValidationError:
                continue


def _status_code(response: Any) -> Optional[int]:
    try:
        value = int(getattr(response, "status_code"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if 100 <= value <= 599 else None


def _response_text(response: Any) -> str:
    try:
        value = str(getattr(response, "text", "") or "")
    except Exception:
        return ""
    return value[:_MAX_BODY_LENGTH]


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value or "").strip()
    return ""


def _status_from_body(body: str) -> RemoteStatus:
    text = str(body or "")[:_MAX_BODY_LENGTH]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        data = None
    if data is not None:
        statuses: list[RemoteStatus] = []
        _collect_json_statuses(data, statuses, depth=0)
        merged = RemoteStatus.UNKNOWN
        for status in statuses:
            merged = _merge_status(merged, status)
        if merged is not RemoteStatus.UNKNOWN:
            return merged

    parser = _CandidateHTMLParser()
    try:
        parser.feed(text)
        visible_text = " ".join(parser.text_parts)
    except Exception:
        visible_text = text
    lowered = visible_text.lower()
    for marker in _FAILURE_MARKERS:
        if marker in lowered:
            return RemoteStatus.FAILED
    for marker in _CANCEL_MARKERS:
        if marker in lowered:
            return RemoteStatus.CANCELLED
    for marker in _PROCESSING_MARKERS:
        if marker in lowered:
            return RemoteStatus.PENDING
    for marker in _SUCCESS_MARKERS:
        if marker in lowered:
            return RemoteStatus.SUCCEEDED
    return RemoteStatus.UNKNOWN


def _collect_json_statuses(value: Any, output: list[RemoteStatus], *, depth: int) -> None:
    if depth > 8 or len(output) > 100:
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in {"status", "redirect_status", "payment_status"}:
                output.append(_normalize_remote_status(nested))
            else:
                _collect_json_statuses(nested, output, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value[:100]:
            _collect_json_statuses(nested, output, depth=depth + 1)


def _body_candidates(body: str) -> list[str]:
    text = str(body or "")[:_MAX_BODY_LENGTH]
    candidates: list[str] = []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        data = None
    if data is not None:
        _collect_json_urls(data, candidates, depth=0)

    parser = _CandidateHTMLParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    candidates.extend(parser.urls)
    return candidates


def _collect_json_urls(value: Any, output: list[str], *, depth: int) -> None:
    if depth > 8 or len(output) > 100:
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _URL_VALUE_KEYS and isinstance(nested, str):
                output.append(nested)
            else:
                _collect_json_urls(nested, output, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value[:100]:
            _collect_json_urls(nested, output, depth=depth + 1)


def _nested_success_return_url(url: str) -> str:
    try:
        parsed, _ = _validate_return_url(url)
        value = _first(_query(parsed.query), "success_return_url")
    except ReturnURLValidationError:
        return ""
    return value


def _clean_candidate(value: Any) -> str:
    candidate = html.unescape(str(value or "")).strip().strip("'\"")
    if candidate.lower().startswith(("https%3a%2f%2f", "http%3a%2f%2f")):
        candidate = unquote(candidate)
    return candidate.replace("\\/", "/")


def _pick_body_or_nested_hop(
    current: str,
    current_state: NormalizedReturnState,
    body: str,
) -> tuple[str, NormalizedReturnState]:
    raw_candidates = _body_candidates(body)
    nested = _nested_success_return_url(current)
    if nested:
        raw_candidates.append(nested)

    ranked: list[tuple[int, int, str, NormalizedReturnState]] = []
    stage_rank = {
        ReturnStage.STRIPE_RETURN: 0,
        ReturnStage.OPENAI_PAY: 1,
        ReturnStage.CHECKOUT_VERIFY: 2,
        ReturnStage.CHATGPT_LANDING: 3,
    }
    for order, raw in enumerate(raw_candidates):
        candidate = urljoin(current, _clean_candidate(raw))
        try:
            state = normalize_return_state(candidate)
            _validate_transition(current_state.stage, state.stage)
        except ReturnURLValidationError:
            continue
        if _url_fingerprint(candidate) == _url_fingerprint(current):
            continue
        ranked.append((-stage_rank[state.stage], order, candidate, state))
    if not ranked:
        return "", current_state
    ranked.sort(key=lambda item: (item[0], item[1]))
    _, _, candidate, state = ranked[0]
    return candidate, state


def _url_fingerprint(url: str) -> bytes:
    return hashlib.sha256(str(url or "").encode("utf-8", errors="replace")).digest()


def _safe_exception_name(exc: Exception) -> str:
    name = type(exc).__name__
    safe = re.sub(r"[^A-Za-z0-9_]", "", name)[:80]
    return safe or "Exception"


def _terminal_remote_result(
    outcome: PaymentOutcome,
    stage: ReturnStage,
    hops: Sequence[ReconciliationHop],
    evidence: _Evidence,
) -> PayPalReconciliationResult:
    cancelled = outcome is PaymentOutcome.CANCELLED
    return _make_result(
        ReconciliationClassification.CONCLUSIVE,
        outcome,
        retryable=False,
        error_stage=stage.value,
        error_code="remote_payment_cancelled" if cancelled else "remote_payment_failed",
        reason="merchant return reported a terminal payment state",
        final_stage=stage,
        hops=hops,
        evidence=evidence,
    )


def _success_result(
    stage: ReturnStage,
    hops: Sequence[ReconciliationHop],
    evidence: _Evidence,
) -> PayPalReconciliationResult:
    return _make_result(
        ReconciliationClassification.CONCLUSIVE,
        PaymentOutcome.SUCCEEDED,
        retryable=False,
        error_stage=None,
        error_code=None,
        reason="merchant checkout verification reached a successful terminal state",
        final_stage=stage,
        hops=hops,
        evidence=evidence,
    )


def _make_result(
    classification: ReconciliationClassification,
    outcome: PaymentOutcome,
    *,
    retryable: bool,
    error_stage: Optional[str],
    error_code: Optional[str],
    reason: str,
    final_stage: Optional[ReturnStage] = None,
    hops: Sequence[ReconciliationHop] = (),
    evidence: Optional[_Evidence] = None,
) -> PayPalReconciliationResult:
    observed = evidence or _Evidence()
    return PayPalReconciliationResult(
        classification=classification,
        outcome=outcome,
        retryable=bool(retryable),
        error_stage=error_stage,
        error_code=error_code,
        reason=str(reason),
        final_stage=final_stage,
        redirect_status=observed.redirect_status,
        stripe_return_status=observed.stripe_return_status,
        observed_setup_intent=observed.observed_setup_intent,
        observed_client_secret=observed.observed_client_secret,
        observed_success_return_url=observed.observed_success_return_url,
        hops=tuple(hops),
    )


__all__ = [
    "NormalizedReturnState",
    "PayPalReconciliationResult",
    "PaymentOutcome",
    "ReconciliationClassification",
    "ReconciliationHop",
    "ReconciliationTransport",
    "RemoteStatus",
    "ReturnStage",
    "ReturnURLValidationError",
    "normalize_return_state",
    "reconcile_paypal_return",
]

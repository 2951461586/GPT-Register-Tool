import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Mapping

from .config import CFG
from .backoff import bounded_cooldown, transport_backoff
from .error_classification import is_terminal_registration_error
from .proxy_edge_probe import EDGE_CHALLENGE, EDGE_UNKNOWN, edge_challenge_verdict
from .sanitizer import sanitize_text


class SessionCircuitOpen(RuntimeError):
    """The current account/session is cooling down after an auth 403/429.

    ``edge_challenge`` is **observation only**: it records that the reply that
    opened the circuit looked like an exit-level Cloudflare challenge, and it
    shows up as a trailing ``:edge_challenge`` segment in the message.  The
    leading ``session_circuit_open`` token never moves, so
    ``failure_registry.classify_error`` keeps classifying this exactly as it
    did before (invariant 3 of
    ``plan-2026-10-05-inflow-challenge-handoff.md`` §3.5: the challenge path
    adds no new failure class, only a suffix).
    """

    def __init__(self, status_code: int, retry_after: float, *, edge_challenge: bool = False):
        self.status_code = int(status_code or 0)
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.edge_challenge = bool(edge_challenge)
        suffix = ":edge_challenge" if self.edge_challenge else ""
        super().__init__(f"session_circuit_open:http_{self.status_code}:retry_after={self.retry_after:.0f}s{suffix}")


def _retry_after_seconds(response, default=300.0):
    value = ""
    try:
        value = response.headers.get("Retry-After", "")
    except Exception:
        pass
    try:
        seconds = float(str(value).strip())
        return bounded_cooldown(seconds, default)
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(1.0, min((when - datetime.now(timezone.utc)).total_seconds(), 3600.0))
    except Exception:
        return float(default)


def _session_circuit(session):
    state = getattr(session, "_openai_registration_circuit", None)
    if not isinstance(state, dict):
        state = {"blocked_until": 0.0, "status_code": 0, "retry_after": 0.0}
        try:
            setattr(session, "_openai_registration_circuit", state)
        except Exception:
            pass
    return state


def clear_session_circuit(session):
    state = _session_circuit(session)
    state.update({"blocked_until": 0.0, "status_code": 0, "retry_after": 0.0, "edge_challenge": False})


def edge_challenge_discrimination_enabled():
    """``registration.edge_challenge_discrimination`` (default **True**).

    Read-only: it decides only whether a 403/429 *name* carries the
    ``:edge_challenge`` suffix.  No control flow reads it -- rotation and
    handoff are separate, default-**off** switches
    (``plan-2026-10-05-inflow-challenge-handoff.md`` §3.1).

    Default on because the error name is the only data source the offline A/B
    (``p0-2b-inflow-challenge-handoff``) has for "did a challenge happen at
    all": an observation that ships off measures nothing, and a wrong suffix
    costs a mislabelled log line, not a lost mailbox.
    """
    registration = CFG.get("registration") if isinstance(CFG, Mapping) else None
    if not isinstance(registration, Mapping):
        return True
    value = registration.get("edge_challenge_discrimination", True)
    return value not in (False, 0, "0", "false", "False", "no", "No", "off")


def _is_edge_challenge(response):
    """Read-only label for one 403/429; never changes what happens next."""
    if not edge_challenge_discrimination_enabled():
        return False
    try:
        return edge_challenge_verdict(response) == EDGE_CHALLENGE
    except Exception:
        # An observation must never turn a transport reply into a crash.
        return False


#: Session attribute through which the registration handler installs its
#: challenge hook: ``hook(session, verdict, rotate_allowed) -> bool``, called
#: once per observed challenge/unknown reply, returning True when it rotated this
#: run's exit (so the transport retries the same request exactly once).
#:
#: A callback on the session -- not a module-level hook and not transport-owned
#: policy -- because concurrent runs each own a session, and because the
#: *handler* owns the exit: the proxy string, the pool cursor, the audit
#: counters and the ``clear_session_circuit`` invariant.  The transport owns
#: only the once-per-request cap.  A session without the attribute (every
#: non-registration caller) simply observes.
#:
#: The verdict is passed through rather than a bare boolean because §7-5 of
#: ``plan-2026-10-05-inflow-challenge-handoff.md`` requires ``unknown`` rotations
#: to be counted **separately** from ``challenge`` ones -- the A/B's manipulation
#: check reads the challenge count, and merging uncertainty into it would dilute
#: the very number the comparison rests on.
EDGE_CHALLENGE_HOOK_ATTR = "_openai_edge_challenge_hook"


def _report_edge_challenge(session, verdict: str, rotate_allowed: bool) -> bool:
    """Ask the run's hook about an observed challenge; True = rotated."""
    hook = getattr(session, EDGE_CHALLENGE_HOOK_ATTR, None)
    if not callable(hook):
        return False
    try:
        return bool(hook(session, verdict, bool(rotate_allowed)))
    except Exception:
        # A hook failure must degrade to today's behaviour, never to a crash.
        return False


def _apply_edge_challenge_policy(session, response, caller, url, kwargs, *, rotate_allowed):
    """Observe one 403/429 challenge; optionally rotate the exit and retry once.

    Returns ``(final_response, is_challenge, rotated)``.  It deliberately does
    **not** write the circuit: the caller does that afterwards, which is what
    keeps §3.5's ordering true -- a rotation that succeeds leaves no circuit
    behind, and a rotation that fails falls through to exactly the old
    behaviour.  The retry is verbatim (same method, URL, headers and body),
    because a challenge means "this exit was refused", not "the request was
    wrong".

    §7-5: ``unknown`` rotates too.  The asymmetry is the whole point of this
    feature -- a false negative burns a mailbox and its OTP forever, a false
    positive costs one request -- so an unreadable verdict is spent on the cheap
    side.  It is counted separately by the hook.  (From ``request_with_retry``
    ``unknown`` is currently unreachable: this function is only entered on
    403/429, and the verdict answers ``unknown`` only off those statuses.  The
    branch is here so the contract is complete and testable the moment a wider
    caller consults the judgement.)
    """
    try:
        verdict = edge_challenge_verdict(response)
    except Exception:
        verdict = EDGE_UNKNOWN
    if verdict not in (EDGE_CHALLENGE, EDGE_UNKNOWN):
        return response, False, False
    if not _report_edge_challenge(session, verdict, rotate_allowed):
        return response, verdict == EDGE_CHALLENGE, False
    retried = caller(url, **kwargs)
    try:
        retried_verdict = edge_challenge_verdict(retried)
    except Exception:
        retried_verdict = EDGE_UNKNOWN
    return retried, retried_verdict == EDGE_CHALLENGE, True


def _raise_if_circuit_open(session):
    state = _session_circuit(session)
    remaining = float(state.get("blocked_until") or 0.0) - time.time()
    if remaining > 0:
        raise SessionCircuitOpen(
            state.get("status_code") or 0, remaining, edge_challenge=bool(state.get("edge_challenge"))
        )
    if state.get("blocked_until"):
        clear_session_circuit(session)


TRANSIENT_MARKERS = (
    "tls connect error",
    "openssl_internal",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "failed to connect",
    "proxy",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
    # Firefox/Camoufox reports an interrupted navigation as NS_ERROR_ABORT;
    # this is a transport-level retry, not an account or mailbox failure.
    "ns_error_abort",
    "navigation aborted",
)


def _timeout_cfg():
    return CFG.get("timeouts") or {}


def request_timeout():
    try:
        return max(1, int(_timeout_cfg().get("request", 30) or 30))
    except Exception:
        return 30


def request_attempts():
    try:
        return max(1, int(_timeout_cfg().get("http_retries", 3) or 3))
    except Exception:
        return 3


def request_retry_delay():
    try:
        return max(0.0, float(_timeout_cfg().get("retry_delay", 2) or 2))
    except Exception:
        return 2.0


def is_transient_transport_error(error):
    text = str(error or "").lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


def request_with_retry(session, method, url, *, label="", attempts=None, retry_delay=None, **kwargs):
    base_attempts = request_attempts() if attempts is None else max(1, int(attempts or 1))
    base_delay = request_retry_delay() if retry_delay is None else max(0.0, float(retry_delay or 0))
    kwargs.setdefault("timeout", request_timeout())

    caller = getattr(session, method.lower())
    last_error = None
    # 尊重配置的 http_retries，不再用硬编码 5 覆盖。
    # 之前 `max(base_attempts, 5)` 导致配 3 实际跑 5，与配置语义不符。
    # 给一个合理上限（10）防止异常配置把重试拉到天上去。
    max_attempts = max(1, min(base_attempts, 10))
    # §3.3: at most **one** exit rotation per request.  SunnyRegister retries
    # three times, but that resets a proof provider; rotating the exit swaps the
    # whole session's egress, so a second rotation would make "did it help?"
    # unattributable.
    rotations = 0
    for attempt in range(1, max_attempts + 1):
        try:
            _raise_if_circuit_open(session)
            attempt_kwargs = dict(kwargs)
            if attempt > 1:
                # OTP waits frequently leave an upstream keep-alive socket stale.
                # Preserve the session cookie jar but force a fresh connection.
                headers = dict(attempt_kwargs.get("headers") or {})
                headers["Connection"] = "close"
                attempt_kwargs["headers"] = headers
            response = caller(url, **attempt_kwargs)
            status_code = int(getattr(response, "status_code", 0) or 0)
            edge_challenge = False
            if status_code in {403, 429}:
                response, edge_challenge, rotated = _apply_edge_challenge_policy(
                    session,
                    response,
                    caller,
                    url,
                    attempt_kwargs,
                    rotate_allowed=rotations < 1,
                )
                rotations += int(rotated)
                status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {403, 429}:
                retry_after = _retry_after_seconds(response, default=900.0 if status_code == 403 else 300.0)
                state = _session_circuit(session)
                state.update(
                    {
                        "blocked_until": time.time() + retry_after,
                        "status_code": status_code,
                        "retry_after": retry_after,
                        # Recorded, never acted on by the circuit: the challenge
                        # path has already had its chance above, so writing the
                        # breaker here is §3.5's "only after the challenge path
                        # failed".
                        "edge_challenge": bool(edge_challenge and edge_challenge_discrimination_enabled()),
                    }
                )
            return response
        except Exception as error:
            last_error = error
            # Terminal markers are pure error classification, not registration
            # policy. This used to call registration_retry_decision(error,
            # failure_class="network"), which is exactly equivalent: "network"
            # is in RETRYABLE_CLASSES, so .retryable collapsed to `not terminal`.
            # Asking classification directly keeps the transport layer free of
            # any import back edge into the policy layer. Do not "restore" the
            # policy call here -- that reintroduces the layering violation.
            if not is_transient_transport_error(error) or is_terminal_registration_error(error):
                raise
            if attempt >= max_attempts:
                raise
            # Exponential backoff: base_delay * 2^(attempt-1), capped at 15s
            delay = transport_backoff(attempt, base_delay)
            prefix = f"  {label} " if label else "  "
            print(f"{prefix}transport retry {attempt}/{max_attempts}: {sanitize_text(error)}")
            if delay:
                time.sleep(delay)
    # Unreachable in practice: every exit from the loop above either returns the
    # response or re-raises.  Spelled out so the ``last_error`` binding is not a
    # bare ``None`` at the raise site.
    if last_error is None:
        raise RuntimeError(f"request_with_retry exhausted without an error: {method} {url}")
    raise last_error

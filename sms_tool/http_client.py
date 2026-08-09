import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from .config import CFG


class SessionCircuitOpen(RuntimeError):
    """The current account/session is cooling down after an auth 403/429."""

    def __init__(self, status_code: int, retry_after: float):
        self.status_code = int(status_code or 0)
        self.retry_after = max(0.0, float(retry_after or 0.0))
        super().__init__(f"session_circuit_open:http_{self.status_code}:retry_after={self.retry_after:.0f}s")


def _retry_after_seconds(response, default=300.0):
    value = ""
    try:
        value = response.headers.get("Retry-After", "")
    except Exception:
        pass
    try:
        seconds = float(str(value).strip())
        return max(1.0, min(seconds, 3600.0))
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
    state.update({"blocked_until": 0.0, "status_code": 0, "retry_after": 0.0})


def _raise_if_circuit_open(session):
    state = _session_circuit(session)
    remaining = float(state.get("blocked_until") or 0.0) - time.time()
    if remaining > 0:
        raise SessionCircuitOpen(state.get("status_code") or 0, remaining)
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
            if status_code in {403, 429}:
                retry_after = _retry_after_seconds(response, default=900.0 if status_code == 403 else 300.0)
                state = _session_circuit(session)
                state.update({
                    "blocked_until": time.time() + retry_after,
                    "status_code": status_code,
                    "retry_after": retry_after,
                })
            return response
        except Exception as error:
            last_error = error
            if not is_transient_transport_error(error):
                raise
            if attempt >= max_attempts:
                raise
            # Exponential backoff: base_delay * 2^(attempt-1), capped at 15s
            delay = min(base_delay * (2 ** (attempt - 1)), 15.0)
            prefix = f"  {label} " if label else "  "
            print(f"{prefix}transport retry {attempt}/{max_attempts}: {error}")
            if delay:
                time.sleep(delay)
    raise last_error

import time

from .config import CFG


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
    # Use more retries for TLS/proxy errors (curl: (35) etc.)
    max_attempts = max(base_attempts, 5)
    for attempt in range(1, max_attempts + 1):
        try:
            return caller(url, **kwargs)
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

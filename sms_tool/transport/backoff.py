"""Transport-level backoff and cooldown clamping.

Deliberately dependency-free (stdlib only). Both the HTTP transport layer and
the registration policy layer need these two helpers, and the transport layer
must not import policy -- that back edge is exactly why this module exists.

Nothing here knows what a "registration" is. If a helper starts needing
registration vocabulary, it belongs in ``registration_policy`` instead.
"""


def transport_backoff(attempt: int, base_delay: float) -> float:
    """Exponential backoff for transport retries: ``base_delay * 2^(attempt-1)``.

    Capped at 15s so a misconfigured ``retry_delay`` cannot stall a batch.
    """
    return min(max(0.0, base_delay) * 2 ** max(0, min(attempt - 1, 10)), 15.0)


def bounded_cooldown(seconds: float, default: float = 300.0) -> float:
    """Clamp a Retry-After style cooldown into ``[1.0, 3600.0]``.

    ``seconds is None`` falls back to ``default``; the lower bound of 1.0 keeps
    a hostile ``Retry-After: 0`` from turning into a hot retry loop.
    """
    return max(1.0, min(float(default if seconds is None else seconds), 3600.0))

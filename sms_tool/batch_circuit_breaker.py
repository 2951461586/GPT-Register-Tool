"""Batch-level circuit breaker for environment failures (P2-2).

The per-account machinery already knows when a *single* registration should not
be retried (:class:`~sms_tool.registration_retry_guard.RegistrationRetryGuard`)
and when a *single* proxy is unhealthy
(:class:`~sms_tool.proxy_health.ProxyHealthTracker`). Neither one can see the
pattern that actually destroys a batch: when the environment dies -- the proxy
pool goes down, or the signup page changes shape -- every account fails the same
way, and the batch walks through the entire mailbox list before anyone notices.

This is the missing whole-batch stop: N consecutive environment failures trip
the breaker and the remaining accounts are parked instead of consumed.

What counts as an environment failure is not a new judgement call --
``RETRYABLE_CLASSES`` already answers it. Those are the classes the retry policy
considers "the environment failed, a retry might help"; if retrying does not
help for ``threshold`` accounts in a row, the environment is broken.
``account`` / ``mailbox`` / ``rate_limit`` are per-account verdicts and must
never trip a batch-level breaker.
"""

from __future__ import annotations

import threading

from .registration_policy import RETRYABLE_CLASSES

# Consecutive environment failures tolerated before the batch parks itself.
# Matches the reference implementation's "3 consecutive network/env errors".
DEFAULT_THRESHOLD = 3

# network / auth_state -- see the module docstring for why this is exactly
# RETRYABLE_CLASSES and not a hand-maintained duplicate list.
ENVIRONMENT_FAILURE_CLASSES = frozenset(RETRYABLE_CLASSES)


class BatchCircuitBreaker:
    """Trip after ``threshold`` consecutive environment failures in one batch.

    In-memory and per-batch on purpose: a breaker that persisted across batches
    would park a batch because of something that happened yesterday. Workers
    share one instance, so ``record`` is lock-guarded.
    """

    def __init__(self, threshold: int = DEFAULT_THRESHOLD, *, enabled: bool = True) -> None:
        try:
            parsed = int(threshold)
        except (TypeError, ValueError):
            # A junk value must not disable the breaker; fall back to the default.
            parsed = DEFAULT_THRESHOLD
        self.threshold = max(1, parsed)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._consecutive = 0
        self._tripped = False
        self._tripped_class = ""
        self._tripped_count = 0

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    @property
    def consecutive(self) -> int:
        with self._lock:
            return self._consecutive

    @property
    def tripped_class(self) -> str:
        """Failure class that tripped the breaker ("" while it is closed)."""
        with self._lock:
            return self._tripped_class if self._tripped else ""

    def record_success(self) -> bool:
        """One account finished cleanly; the environment is fine. Never trips."""
        with self._lock:
            self._consecutive = 0
            return self._tripped

    def record(self, failure_class: object) -> bool:
        """Record one terminal account outcome.

        Returns ``True`` when the breaker is open *after* this outcome, so the
        caller can report the transition exactly once. An already-open breaker
        stays open and keeps reporting ``True`` -- it never heals mid-batch.
        """
        cls = str(failure_class or "").strip().lower()
        with self._lock:
            if self._tripped:
                return True
            if not self.enabled:
                return False
            if cls in ENVIRONMENT_FAILURE_CLASSES:
                self._consecutive += 1
                if self._consecutive >= self.threshold:
                    self._tripped = True
                    self._tripped_class = cls
                    self._tripped_count = self._consecutive
            else:
                # A per-account verdict (account/mailbox/rate_limit/unknown)
                # proves the environment is still able to reach the target.
                self._consecutive = 0
            return self._tripped


__all__ = [
    "ENVIRONMENT_FAILURE_CLASSES",
    "DEFAULT_THRESHOLD",
    "BatchCircuitBreaker",
]

"""Browser process pool for concurrent Camoufox registrations.

Manages a pool of browser processes that can be reused across multiple
registration tasks.  Each process tracks a health state and a generation
counter so stale or degraded browsers are recycled automatically.

The pool is designed for the synchronous registration flow: callers acquire
a browser session, run their registration logic, and release the session back
to the pool.  A ``threading.Semaphore`` bounds concurrency so the pool size
is respected without external coordination.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .registration_drivers.base import BrowserRegistrationError
from .registration_drivers.external_sessions import create_browser_session

logger = logging.getLogger(__name__)


class BrowserHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class BrowserSlot:
    """Metadata for one browser process in the pool."""

    slot_id: int
    generation: int = 0
    health: BrowserHealth = BrowserHealth.HEALTHY
    uses: int = 0
    last_used: float = 0.0
    last_error: str = ""

    def needs_recycle(self, max_uses: int) -> bool:
        if self.health == BrowserHealth.FAILED:
            return True
        if self.uses >= max_uses:
            return True
        return False


@dataclass
class PoolConfig:
    max_concurrent: int = 4
    contexts_per_process: int = 6
    max_uses_per_process: int = 10
    recycle_on_error: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PoolConfig":
        pool_cfg = config.get("registration", {})
        pool_cfg = pool_cfg.get("browser_pool", {}) if isinstance(pool_cfg, Mapping) else {}
        if not isinstance(pool_cfg, Mapping):
            pool_cfg = {}
        return cls(
            max_concurrent=int(pool_cfg.get("max_concurrent") or 4),
            contexts_per_process=int(pool_cfg.get("contexts_per_process") or 6),
            max_uses_per_process=int(pool_cfg.get("max_uses_per_process") or 10),
            recycle_on_error=bool(pool_cfg.get("recycle_on_error", True)),
        )


class BrowserProcessPool:
    """Pool of browser processes for concurrent registration.

    Usage::

        pool = BrowserProcessPool(config, driver="camoufox")
        with pool.session(proxy=..., headless=...) as (browser, slot):
            # run registration using browser.page, browser.context, etc.
            ...
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        driver: str = "camoufox",
        proxy: str | None = None,
        headless: bool = True,
        timeout_ms: int = 90_000,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
    ) -> None:
        self.pool_config = PoolConfig.from_config(config)
        self.driver = driver
        self.config = config
        self.proxy = proxy
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.locale = locale
        self.timezone_id = timezone_id

        self._semaphore = threading.Semaphore(self.pool_config.max_concurrent)
        self._slots: list[BrowserSlot] = [
            BrowserSlot(slot_id=i) for i in range(self.pool_config.max_concurrent)
        ]
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def session(self, **session_overrides: Any):
        """Acquire a browser session from the pool.

        Yields ``(browser, slot)`` where ``browser`` is a connected browser
        session and ``slot`` is the ``BrowserSlot`` tracking this process.
        The browser is closed on context exit if it needs recycling.
        """
        if self._closed:
            raise BrowserRegistrationError("browser_pool_closed")

        acquired = self._semaphore.acquire(timeout=120)
        if not acquired:
            raise BrowserRegistrationError("browser_pool_timeout", "no slot available within 120s")

        slot = self._acquire_slot()
        browser = None
        try:
            kwargs = {
                "proxy": session_overrides.get("proxy", self.proxy),
                "headless": session_overrides.get("headless", self.headless),
                "timeout_ms": session_overrides.get("timeout_ms", self.timeout_ms),
                "locale": session_overrides.get("locale", self.locale),
                "timezone_id": session_overrides.get("timezone_id", self.timezone_id),
            }
            browser = create_browser_session(self.driver, config=self.config, **kwargs)
            browser.__enter__()
            yield browser, slot
            slot.health = BrowserHealth.HEALTHY
        except BrowserRegistrationError as exc:
            slot.health = BrowserHealth.FAILED
            slot.last_error = exc.code
            raise
        except Exception as exc:
            slot.health = BrowserHealth.DEGRADED
            slot.last_error = str(exc)[:200]
            raise
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            slot.uses += 1
            slot.last_used = time.time()
            self._release_slot(slot)
            self._semaphore.release()

    def _acquire_slot(self) -> BrowserSlot:
        with self._lock:
            # Prefer the healthiest, least-recently-used slot.
            candidates = sorted(
                self._slots,
                key=lambda s: (
                    s.health != BrowserHealth.HEALTHY,
                    s.needs_recycle(self.pool_config.max_uses_per_process),
                    s.last_used,
                ),
            )
            slot = candidates[0]
            if slot.needs_recycle(self.pool_config.max_uses_per_process):
                slot.generation += 1
                slot.uses = 0
                slot.health = BrowserHealth.HEALTHY
                slot.last_error = ""
            return slot

    def _release_slot(self, slot: BrowserSlot) -> None:
        with self._lock:
            if self.pool_config.recycle_on_error and slot.health == BrowserHealth.FAILED:
                slot.generation += 1
                slot.uses = 0
                slot.health = BrowserHealth.HEALTHY
                slot.last_error = ""

    @property
    def stats(self) -> dict[str, Any]:
        """Return a snapshot of pool health for diagnostics."""
        with self._lock:
            return {
                "max_concurrent": self.pool_config.max_concurrent,
                "slots": [
                    {
                        "slot_id": s.slot_id,
                        "generation": s.generation,
                        "health": s.health.value,
                        "uses": s.uses,
                        "last_error": s.last_error,
                    }
                    for s in self._slots
                ],
            }

    def close(self) -> None:
        """Mark the pool as closed; no new sessions can be acquired."""
        self._closed = True


__all__ = [
    "BrowserHealth",
    "BrowserProcessPool",
    "BrowserSlot",
    "PoolConfig",
]

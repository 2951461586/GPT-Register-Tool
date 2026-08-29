"""Pulse-wave batch scheduling with IP-ban detection.

Replaces continuous concurrent batch submission with discrete waves:
each wave submits a sub-batch of registrations, waits for completion,
then analyses results before launching the next wave.  When OTP
delivery failures cluster across multiple accounts in a wave, the
pulse scheduler flags a likely IP-ban and pauses before the next
wave to allow proxy rotation.

Integration is opt-in via ``registration.pulse.enabled`` in config.
When disabled, ``run_batch_impl`` proceeds with its original
all-at-once concurrent strategy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import CFG


# Failure signatures that indicate OTP delivery was blocked, most likely
# by an IP-level ban rather than per-account issues.
_OTP_BAN_MARKERS = (
    "otp_not_received", "otp_timeout", "email_otp_timeout",
    "mailbox_otp_not_received", "no_otp", "otp_poll_timeout",
)


class PulseConfig:
    """Configuration for pulse-wave scheduling."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        wave_size: int = 4,
        wave_delay_seconds: float = 5.0,
        ban_threshold: int = 2,
        ban_pause_seconds: float = 60.0,
        max_waves: int = 0,
    ) -> None:
        self.enabled = enabled
        self.wave_size = max(1, int(wave_size))
        self.wave_delay_seconds = max(0.0, float(wave_delay_seconds))
        self.ban_threshold = max(1, int(ban_threshold))
        self.ban_pause_seconds = max(0.0, float(ban_pause_seconds))
        self.max_waves = max(0, int(max_waves))

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "PulseConfig":
        if not isinstance(config, Mapping):
            return cls()
        registration = config.get("registration", {})
        if not isinstance(registration, Mapping):
            return cls()
        pulse = registration.get("pulse", {})
        if not isinstance(pulse, Mapping):
            return cls()
        return cls(
            enabled=bool(pulse.get("enabled", False)),
            # ``or`` would swallow legitimate zeros: wave_size=0 must clamp to
            # 1 (PulseConfig's floor), not silently fall back to the default.
            wave_size=_coerce_number(pulse.get("wave_size"), 4, int),
            wave_delay_seconds=_coerce_number(pulse.get("wave_delay_seconds"), 5.0, float),
            ban_threshold=_coerce_number(pulse.get("ban_threshold"), 2, int),
            ban_pause_seconds=_coerce_number(pulse.get("ban_pause_seconds"), 60.0, float),
            max_waves=_coerce_number(pulse.get("max_waves"), 0, int),
        )


def _coerce_number(value: Any, default: Any, cast: Callable[[Any], Any]) -> Any:
    """Cast a config value, falling back to ``default`` when unusable.

    Missing values (``None``), booleans and non-numeric junk all yield the
    default so a typo in config.json degrades to stock behaviour instead of
    crashing the whole batch before it starts.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _is_otp_ban_signal(result: dict[str, Any]) -> bool:
    """Check if a result's failure looks like an IP-ban OTP block."""
    if result.get("success"):
        return False
    error = str(result.get("error") or "").lower()
    failure_class = str(result.get("failure_class") or "").lower()
    if failure_class in {"rate_limit", "account"}:
        return False
    return any(marker in error for marker in _OTP_BAN_MARKERS)


def _detect_ip_ban(wave_results: list[dict[str, Any]], threshold: int) -> bool:
    """Return True if enough OTP failures in a wave suggest an IP ban."""
    ban_signals = sum(1 for r in wave_results if _is_otp_ban_signal(r))
    return ban_signals >= threshold


def run_pulse_batch(
    count: int,
    *,
    run_one_fn: Callable[[int], tuple[int, dict[str, Any]]],
    on_result: Callable[[int, dict[str, Any]], None] | None = None,
    workers: int = 4,
    pulse_config: PulseConfig | None = None,
) -> list[dict[str, Any]]:
    """Run registrations in pulse waves with IP-ban detection.

    ``run_one_fn`` is the per-account function (matching ``_run_one`` in
    ``batch_runner``).  Returns ordered results for all accounts.
    """
    if pulse_config is None:
        pulse_config = PulseConfig.from_config(CFG if hasattr(CFG, "data") else {})

    wave_size = pulse_config.wave_size
    wave_delay = pulse_config.wave_delay_seconds
    max_waves = pulse_config.max_waves

    results: list[dict[str, Any] | None] = [None] * count
    remaining = list(range(count))
    wave_number = 0

    while remaining:
        wave_number += 1
        if max_waves > 0 and wave_number > max_waves:
            # Emit a terminal result for every skipped account instead of
            # silently dropping it: callers size their bookkeeping from the
            # returned list, so a short list would desync account indices and
            # make the run look like it never attempted those accounts.
            print(f"[Pulse] Max waves ({max_waves}) reached; stopping with {len(remaining)} accounts unprocessed")
            for idx in remaining:
                skipped = {
                    "success": False,
                    "error": "pulse_max_waves_reached",
                    "failure_class": "skipped",
                    "dropped": False,
                    "registration_attempts": 0,
                }
                results[idx] = skipped
                if on_result:
                    on_result(idx, skipped)
            remaining = []
            break

        wave_indices = remaining[:wave_size]
        remaining = remaining[wave_size:]

        print(f"\n[Pulse] Wave {wave_number}: {len(wave_indices)} account(s)")

        wave_results: list[dict[str, Any]] = []
        if workers <= 1 or len(wave_indices) <= 1:
            for idx in wave_indices:
                _, result = run_one_fn(idx)
                results[idx] = result
                wave_results.append(result)
                if on_result:
                    on_result(idx, result)
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(wave_indices))) as executor:
                futures = {executor.submit(run_one_fn, idx): idx for idx in wave_indices}
                for future in as_completed(futures):
                    idx, result = future.result()
                    results[idx] = result
                    wave_results.append(result)
                    if on_result:
                        on_result(idx, result)

        # IP-ban detection
        if _detect_ip_ban(wave_results, pulse_config.ban_threshold):
            ban_count = sum(1 for r in wave_results if _is_otp_ban_signal(r))
            print(
                f"[Pulse] ⚠ IP-ban suspected: {ban_count} OTP failures in "
                f"wave {wave_number} (threshold={pulse_config.ban_threshold})"
            )
            if remaining and pulse_config.ban_pause_seconds > 0:
                print(f"[Pulse] Pausing {pulse_config.ban_pause_seconds}s before next wave for proxy rotation")
                time.sleep(pulse_config.ban_pause_seconds)

        # Inter-wave delay
        if remaining and wave_delay > 0:
            time.sleep(wave_delay)

    return [r for r in results if r is not None]


__all__ = [
    "PulseConfig",
    "run_pulse_batch",
]

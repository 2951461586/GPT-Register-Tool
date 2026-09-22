"""Pulse-wave batch scheduling with IP-ban detection.

Replaces continuous concurrent batch submission with discrete waves:
each wave submits a sub-batch of registrations, waits for completion,
then analyses results before launching the next wave.  When OTP
delivery failures cluster across multiple accounts in a wave, the
pulse scheduler flags a likely IP-ban and pauses before the next
wave to allow proxy rotation.

「OTP 失败」必须读成**服务端没完成派发**，不是「码没到邮箱」——
后者是邮箱侧问题，停 60s 换出口一点用都没有。而且每个账号钉在池里各自的
出口上，所以只有**整轮一致**的派发侧失败才算出口级证据（2026-09-16 P0-2，
见 ``_is_otp_ban_signal`` / ``_detect_ip_ban``）。

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
from .failure_registry import (
    OTP_BAN_MARKERS as _OTP_BAN_MARKERS,
    OTP_MAILBOX_SIDE_MARKER as _OTP_MAILBOX_SIDE_MARKER,
    OTP_UNDISPATCHED_MARKER as _OTP_UNDISPATCHED_MARKER,
)
from .registration_cancel import cancellable_sleep

# Failure signatures that indicate OTP delivery was blocked, most likely
# by an IP-level ban rather than per-account issues. 词汇在 failure_registry
#（单一注册表）。


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
        canary_enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.wave_size = max(1, int(wave_size))
        self.wave_delay_seconds = max(0.0, float(wave_delay_seconds))
        self.ban_threshold = max(1, int(ban_threshold))
        self.ban_pause_seconds = max(0.0, float(ban_pause_seconds))
        self.max_waves = max(0, int(max_waves))
        self.canary_enabled = bool(canary_enabled)

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
            canary_enabled=bool(pulse.get("canary_enabled", True)),
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
    """Check if a result's failure looks like an IP-ban OTP block.

    2026-09-16（P0-2）：``OTP_BAN_MARKERS`` 是**子串**匹配，而
    ``otp_poll_timeout`` 恰好命中 ``email_otp_poll_timeout`` —— 那是我们自己的
    邮箱轮询超时（邮箱侧），不是服务端封禁。实测批次 25288：6 个
    ``email_otp_poll_timeout`` 全部发生在服务端**已派发**（事务键里没有挂起标记）
    的 run 上，却被计成封禁信号，导致每个 wave 白停 60s 且换出口无效。

    所以先看 ``registration_handlers._otp_timeout_error`` 生成的显式后缀：
    ``mailbox_side_no_code`` = 邮箱侧，**不是**封禁信号；``otp_send_stuck`` =
    服务端没完成派发（**派发侧**，出口只是可能的原因之一），**是**候选信号。
    两者互斥，都来自同一份 ``client_auth_session_dump``。没有后缀（dump 不可用）
    时才回落到旧的子串判据 —— 拿不到证据时宁可多停 60s。

    ⚠ 第二个后缀可能**再带**一个能力后缀
    （``...:mailbox_side_no_code:no_resend_for_channel``，该渠道没有重发能力）。
    这里仍然先在 ``_OTP_MAILBOX_SIDE_MARKER`` 上短路 ⇒ 组合形式照样**不是**
    封禁信号。🔴 短路必须排在 ``_OTP_BAN_MARKERS`` **之前** —— 组合串里仍然
    含 ``otp_poll_timeout`` 子串，顺序反了就会把它重新读成封禁。

    ⚠ 本函数只回答「这条失败是不是**派发侧**的」。够不够格叫「IP 封禁」还要看
    ``_detect_ip_ban`` 的整轮一致性 —— 单账号的派发侧失败在钉定了各自出口的
    池子里不是出口证据。
    """
    if result.get("success"):
        return False
    error = str(result.get("error") or "").lower()
    failure_class = str(result.get("failure_class") or "").lower()
    if failure_class in {"rate_limit", "account"}:
        return False
    if _OTP_MAILBOX_SIDE_MARKER in error:
        return False
    if _OTP_UNDISPATCHED_MARKER in error:
        return True
    return any(marker in error for marker in _OTP_BAN_MARKERS)


def _detect_ip_ban(wave_results: list[dict[str, Any]], threshold: int) -> bool:
    """Return True if a wave's OTP failures are consistent with an IP ban.

    2026-09-16（P0-2）：除了「够多」，还必须「**整轮都在同一侧失败**」。

    ``batch_runner._run_one`` 把每个账号钉在池里各自的出口上
    （``account_proxy_index = i % len(proxy_pool)``，重试也只刷新 session id、
    不换出口）。所以一轮里只要**有任何一个账号拿到了码**，就同时证明了池子、
    目标域名和发码链路都是通的 —— 剩下的失败是账号级的，不是出口级的。

    实测批次 25288（wave_size=4）：wave 2 = 3 超时 + 1 拿到码，wave 3 = 2 超时
    + 2 拿到码，两次都被判成封禁、白停 60s×2；而 wave 4/5/6 同一出口、同样配置
    却 0 超时。旧判据只看「≥ threshold 个」，把「N 个互不相关的账号级停顿」
    读成了「出口被封」。
    """
    ban_signals = sum(1 for r in wave_results if _is_otp_ban_signal(r))
    if ban_signals < threshold:
        return False
    # 整轮一致才是出口级证据；混合结局说明出口是通的。
    return ban_signals == len(wave_results)


def run_pulse_batch(
    count: int,
    *,
    run_one_fn: Callable[[int], tuple[int, dict[str, Any]]],
    on_result: Callable[[int, dict[str, Any]], None] | None = None,
    workers: int = 4,
    pulse_config: PulseConfig | None = None,
    cancel_event=None,
    on_dispatch_block: Callable[[], bool] | None = None,
    on_wave_complete: Callable[[list[int], list[dict[str, Any]]], None] | None = None,
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
    next_wave_is_canary = pulse_config.canary_enabled

    def cancelled() -> bool:
        from .registration_cancel import registration_cancel_requested

        return (cancel_event is not None and cancel_event.is_set()) or registration_cancel_requested()

    while remaining:
        if cancelled():
            for idx in remaining:
                skipped = {
                    "success": False,
                    "error": "registration_cancelled",
                    "failure_class": "cancelled",
                    "retryable": False,
                    "dropped": False,
                    "registration_state": "cancelled",
                    "registration_attempts": 0,
                }
                results[idx] = skipped
                if on_result:
                    on_result(idx, skipped)
            remaining = []
            break
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

        current_wave_size = 1 if next_wave_is_canary else wave_size
        wave_indices = remaining[:current_wave_size]
        remaining = remaining[current_wave_size:]
        next_wave_is_canary = False

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

        if on_wave_complete:
            on_wave_complete(list(wave_indices), list(wave_results))

        # A one-account canary exists precisely to stop a blocked route before
        # a full wave spends more mailboxes, so its single dispatch-side
        # failure is sufficient. Normal waves retain the threshold+unanimity
        # requirement.
        dispatch_blocked = (
            len(wave_results) == 1 and _is_otp_ban_signal(wave_results[0])
        ) or _detect_ip_ban(wave_results, pulse_config.ban_threshold)
        if dispatch_blocked:
            ban_count = sum(1 for r in wave_results if _is_otp_ban_signal(r))
            print(
                f"[Pulse] ⚠ OTP dispatch route blocked: all {ban_count} account(s) in "
                f"wave {wave_number} failed on the OTP-dispatch side "
                f"(threshold={pulse_config.ban_threshold})"
            )
            if remaining:
                rotated = bool(on_dispatch_block and on_dispatch_block())
                if rotated:
                    print("[Pulse] Proxy pool cursor rotated before the next wave")
                else:
                    print("[Pulse] No alternate proxy slot; applying cooldown only")
            if remaining and pulse_config.ban_pause_seconds > 0:
                print(f"[Pulse] Cooling down for {pulse_config.ban_pause_seconds}s before the next canary")
                # Wake early on cancellation; the loop head then emits terminal
                # cancelled results for every remaining account.
                if cancellable_sleep(pulse_config.ban_pause_seconds, requested=cancelled):
                    continue
            if remaining:
                next_wave_is_canary = True

        # Inter-wave delay
        if remaining and wave_delay > 0:
            if cancellable_sleep(wave_delay, requested=cancelled):
                continue

    return [r for r in results if r is not None]


__all__ = [
    "PulseConfig",
    "run_pulse_batch",
]

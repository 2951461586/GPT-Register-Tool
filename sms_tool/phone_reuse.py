"""Phone verification pool for registration.

Numbers are rented from a provider declared in :mod:`sms_providers`. The
provider is selected by ``phone_reuse.source`` and its settings live under
``phone_reuse.<provider>``. Activations are kept open across timeouts so a late
code can still be retried. Once the configured reuse count is reached, the next
SMS round resets the exhausted activation and buys a new number.

The static phone-pool mode was removed on 2026-09-22. It read a hand-maintained
list of ``{phone, sms_api_url}`` entries and polled each URL for a code, which
meant the operator owned the number inventory and the pool had **no activation
lifecycle**: ``complete``/``cancel`` were no-ops, so nothing ever told the vendor
the code had arrived. Every slot is now a rental with an activation id.

This module is the facade and the OpenAI-OTP orchestration. The pieces live in:

* :mod:`sms_tool.phone_config` -- provider key resolution and diagnostics;
* :mod:`sms_tool.phone_pool` -- :class:`PhoneSlot` / :class:`PhonePool` and the
  atomic cross-process state file;
* :mod:`sms_tool.phone_provider_lifecycle` -- vendor acquire / prepare /
  complete / cancel.

Every name those modules expose is re-exported here, so existing importers keep
working unchanged.
"""

from __future__ import annotations

import time

from . import sms_providers
from .auth_headers import openai_auth_headers_lower
from .config import CFG  # noqa: F401 - re-exported for tests that patch phone_reuse.CFG
from .phone_config import (
    KEY_ORIGIN_CONFIG,  # noqa: F401
    KEY_ORIGIN_ENV,  # noqa: F401
    KEY_ORIGIN_MISSING,  # noqa: F401
    _int_value,
    _key_lookup,  # noqa: F401
    _key_origin,  # noqa: F401
    _number_attempts,
    _phone_reuse_cfg,
    _phone_source,  # noqa: F401 - re-exported for phone_registration
    _provider_api_key,
    _provider_cfg,
    _resolve_secret,  # noqa: F401
    _send_cooldown_seconds,
    _send_retry_attempts,
    _send_retry_delay_seconds,
    has_phone_reuse_config,  # noqa: F401
    missing_key_hint,  # noqa: F401
    phone_reuse_source_error,
    provider_key_collisions,  # noqa: F401
    provider_key_status,  # noqa: F401
)
from .phone_pool import (
    PhonePool,
    PhoneSlot,
    _atomic_write_text,  # noqa: F401
    _saved_state_matches_slot,  # noqa: F401
    _state_for_phone,  # noqa: F401
    rental_handle_for,
)
from .phone_provider_lifecycle import (
    RENTAL_NO_NUMBERS_MAX_ATTEMPTS,  # noqa: F401
    _acquire_nexsms_number,  # noqa: F401
    _acquire_rental_number,  # noqa: F401
    _acquire_smsbower_number,  # noqa: F401
    _cancel_provider_activation,
    _cancel_smsbower_activation,  # noqa: F401
    _complete_provider_activation,
    _complete_smsbower_activation,  # noqa: F401
    _country_candidates,  # noqa: F401
    _is_rental_slot,
    _nexsms_client,  # noqa: F401
    _prepare_provider_for_send,
    _prepare_smsbower_for_send,  # noqa: F401
    _price_matches,  # noqa: F401
    _refresh_smsbower_provider_ids,  # noqa: F401
    _rental_client,  # noqa: F401
    _reset_provider_slot,  # noqa: F401
    _reset_smsbower_slot,  # noqa: F401
    _RentalSmsProviderAdapter,  # noqa: F401
    _sms_provider_adapter,
    _smsbower_client,  # noqa: F401
    _wait_smsbower_code,  # noqa: F401
    rental_client,  # noqa: F401
    rental_protocol,  # noqa: F401
)
from .smsbower import (
    DEFAULT_ENDPOINT,
    GHANA_COUNTRY_CODE,
    OPENAI_SERVICE_CODE,
    normalize_phone,
    normalize_service,
)

#: Shared by both protocol families. Renamed off the vendor name when the second
#: family landed: they are per-rental, not per-vendor.
RENTAL_PHONE_IN_USE_MAX_ATTEMPTS = 10


def _wait_for_send_cooldown(slot: PhoneSlot):
    cooldown = max(0, int(slot.send_cooldown_seconds or 0))
    if cooldown <= 0 or slot.last_send_at <= 0:
        return
    wait = cooldown - (int(time.time()) - int(slot.last_send_at))
    if wait <= 0:
        return
    print(f"[*] Phone send cooldown: waiting {wait}s before reusing {normalize_phone(slot.phone)}")
    time.sleep(wait)


def _should_keep_activation_after_send_failure(result: dict) -> bool:
    code = str(result.get("error_code") or "").strip().lower()
    status = int(result.get("status_code") or 0)
    return code in {"rate_limit_exceeded", "too_many_requests"} or status == 429


def _is_terminal_send_rejection(result: dict) -> bool:
    code = str(result.get("error_code") or "").strip().lower()
    return code in {"fraud_guard", "unsupported_phone_number", "invalid_phone_number"}


def _is_terminal_validate_rejection(result: dict) -> bool:
    status = int(result.get("status_code") or 0)
    body = str(result.get("body") or "").lower()
    message = str(result.get("message") or "").lower()
    text = f"{body} {message}"
    return (
        status == 429
        or "phone_recently_used" in text
        or "this phone number was recently used" in text
        or _phone_number_already_in_use(result)
    )


def _retire_phone_slot_for_batch(phone_pool: PhonePool, phone_slot: PhoneSlot, reason: str):
    if _is_rental_slot(phone_slot):
        _cancel_provider_activation(phone_slot)
    phone_slot.reuse_count = max(1, int(phone_slot.max_reuse_count or 1))
    print(f"[!] Phone slot retired for this batch: {reason}")
    phone_pool.save_state()


def _send_phone_otp_with_retries(session, did, current_url, phone_slot: PhoneSlot, phone: str, sentinel=None, proxy=None) -> dict:
    attempts = max(1, int(phone_slot.send_retry_attempts or 1))
    retry_delay = max(0, int(phone_slot.send_retry_delay_seconds or 0))
    last_result = {}
    for attempt in range(1, attempts + 1):
        _wait_for_send_cooldown(phone_slot)
        try:
            result = send_phone_otp(session, did, current_url, phone, sentinel=sentinel, proxy=proxy)
        except Exception as exc:
            try:
                from .phone_proxy import looks_like_proxy_error
            except Exception:
                looks_like_proxy_error = lambda value: False
            if looks_like_proxy_error(exc):
                result = {"ok": False, "error": "phone_proxy_unavailable", "message": str(exc), "phone": phone}
            else:
                raise
        phone_slot.last_send_at = int(time.time())
        last_result = result
        if result.get("ok"):
            return result
        if result.get("error") == "phone_proxy_unavailable":
            return result
        if attempt >= attempts or not _should_keep_activation_after_send_failure(result):
            return result
        wait = max(retry_delay, int(phone_slot.send_cooldown_seconds or 0))
        if wait > 0:
            detail = result.get("error_code") or result.get("status_code", 0)
            print(f"[*] Phone OTP send retry {attempt}/{attempts} after {wait}s ({detail})")
            time.sleep(wait)
    return last_result


def send_phone_otp(session, did, current_url, phone: str, sentinel=None, proxy=None) -> dict:
    from .codex_sentinel import load_cached_sentinel

    if sentinel is None:
        sentinel = load_cached_sentinel()
    headers = openai_auth_headers_lower(
        did,
        referer=current_url,
        sentinel=sentinel,
        extra={"content-type": "application/json"},
    )
    response = session.post(
        "https://auth.openai.com/api/accounts/add-phone/send",
        headers=headers,
        json={"phone_number": normalize_phone(phone)},
        timeout=30,
        impersonate="chrome110",
    )
    if response.status_code == 200:
        return {"ok": True, "status_code": response.status_code}
    error_code = ""
    error_message = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        error_code = str(error.get("code") or "").strip()
        error_message = str(error.get("message") or "").strip()
    except Exception:
        pass
    return {
        "ok": False,
        "status_code": response.status_code,
        "error_code": error_code,
        "message": error_message,
        "body": response.text[:500],
    }


def validate_phone_otp(session, did, code: str, sentinel=None, proxy=None) -> dict:
    from .codex_sentinel import load_cached_sentinel

    if sentinel is None:
        sentinel = load_cached_sentinel()
    headers = openai_auth_headers_lower(
        did,
        referer="https://auth.openai.com/phone-verification",
        sentinel=sentinel,
        extra={"content-type": "application/json"},
    )
    response = session.post(
        "https://auth.openai.com/api/accounts/phone-otp/validate",
        headers=headers,
        json={"code": code},
        timeout=30,
        impersonate="chrome110",
    )
    if response.status_code == 200:
        try:
            body = response.json()
        except Exception:
            body = {}
        return {
            "ok": True,
            "continue_url": body.get("continue_url") or response.headers.get("Location") or "",
            "body": body,
        }
    return {"ok": False, "status_code": response.status_code, "body": response.text[:300]}


def complete_phone_verification_with_reuse(
    session,
    did,
    current_url,
    phone_pool: PhonePool,
    sentinel=None,
    proxy=None,
    sms_timeout: int = 0,
    sms_poll_interval: int = 0,
) -> dict:
    with phone_pool.lock:
        return _complete_phone_verification_locked(
            session=session,
            did=did,
            current_url=current_url,
            phone_pool=phone_pool,
            sentinel=sentinel,
            proxy=proxy,
            sms_timeout=sms_timeout,
            sms_poll_interval=sms_poll_interval,
        )


def _complete_phone_verification_locked(
    session,
    did,
    current_url,
    phone_pool: PhonePool,
    sentinel=None,
    proxy=None,
    sms_timeout: int = 0,
    sms_poll_interval: int = 0,
) -> dict:
    attempts = max(
        1,
        max((int(phone.number_attempts or 1) for phone in phone_pool.phones if _is_rental_slot(phone)), default=1),
    )
    last_result: dict = {}
    attempt = 1
    while attempt <= attempts:
        result = _complete_phone_verification_once_locked(
            session=session,
            did=did,
            current_url=current_url,
            phone_pool=phone_pool,
            sentinel=sentinel,
            proxy=proxy,
            sms_timeout=sms_timeout,
            sms_poll_interval=sms_poll_interval,
        )
        if result.get("ok"):
            return result
        last_result = result
        should_retry = _should_retry_with_new_provider_number(phone_pool, result)
        if _phone_number_already_in_use(result):
            attempts = RENTAL_PHONE_IN_USE_MAX_ATTEMPTS
        if should_retry and attempt >= attempts:
            if _should_retry_until_success_with_new_provider_number(result):
                attempts = attempt + 1
            else:
                attempts = max(attempts, _minimum_provider_attempts_for_retry(result))
        if attempt >= attempts or not should_retry:
            return result
        print(
            "[*] Phone verification retry with new provider number "
            f"{attempt + 1}/{attempts}: {result.get('error', 'unknown')}"
        )
        phone_pool.reset_exhausted_slots()
        attempt += 1
    return last_result or {"ok": False, "error": "phone_verification_failed"}


def _minimum_provider_attempts_for_retry(result: dict) -> int:
    error = str(result.get("error") or "").strip().lower()
    if error == "phone_sms_timeout" or error.startswith("phone_send_failed:"):
        return 2
    return 1


def _should_retry_until_success_with_new_provider_number(result: dict) -> bool:
    error = str(result.get("error") or "").strip().lower()
    body = str(result.get("body") or "").lower()
    message = str(result.get("message") or "").lower()
    text = f"{error} {body} {message}"
    return error.startswith("phone_send_failed:") and "fraud_guard" in text


def _phone_number_already_in_use(result: dict) -> bool:
    error = str(result.get("error") or "").strip().lower()
    body = str(result.get("body") or "").lower()
    message = str(result.get("message") or "").lower()
    text = f"{error} {body} {message}"
    return any(
        marker in text
        for marker in (
            "phone number already in use",
            "phone_number_already_in_use",
            "phone_already_in_use",
            "use a different phone number",
        )
    )


def _should_retry_with_new_provider_number(phone_pool: PhonePool, result: dict) -> bool:
    if not any(_is_rental_slot(phone) for phone in phone_pool.phones):
        return False
    error = str(result.get("error") or "").strip().lower()
    body = str(result.get("body") or "").lower()
    if error == "phone_sms_timeout":
        return True
    # The prefix is dynamic: ``_complete_phone_verification_once_locked`` raises
    # ``f"{provider}_prepare_failed"``. Matching the literal "smsbower" here meant
    # a HeroSMS/Grizzly failure stopped only by falling through to the final
    # ``return False`` -- so any error later added to this set would have
    # silently not applied to them.
    if error.endswith("_prepare_failed") or error in {"phone_pool_exhausted", "phone_proxy_unavailable"}:
        return False
    if error.startswith("phone_send_failed:"):
        return (
            any(
                marker in error or marker in body
                for marker in ("fraud_guard", "unsupported_phone_number", "invalid_phone_number")
            )
            or _phone_number_already_in_use(result)
        )
    if error.startswith("phone_validate_failed:"):
        return (
            "429" in error
            or "phone_recently_used" in body
            or "recently used" in body
            or _phone_number_already_in_use(result)
        )
    return False


def _complete_phone_verification_once_locked(
    session,
    did,
    current_url,
    phone_pool: PhonePool,
    sentinel=None,
    proxy=None,
    sms_timeout: int = 0,
    sms_poll_interval: int = 0,
) -> dict:
    phone_pool.reset_exhausted_slots()
    phone_slot = phone_pool.get_next_available()
    if not phone_slot:
        return {
            "ok": False,
            "error": "phone_pool_exhausted",
            "message": f"all phones exhausted; total remaining capacity={phone_pool.total_capacity}",
        }

    if sms_timeout:
        phone_slot.sms_timeout = sms_timeout
    if sms_poll_interval:
        phone_slot.sms_poll_interval = sms_poll_interval

    selected_proxy = proxy
    if _is_rental_slot(phone_slot):
        try:
            from .phone_proxy import apply_proxy_to_session, phone_proxy_cfg, select_phone_proxy
            proxy_cfg = phone_proxy_cfg()
        except Exception:
            apply_proxy_to_session = None
            select_phone_proxy = None
            proxy_cfg = {}
        should_probe_proxy = session is not None and (bool(proxy) or any(proxy_cfg.get(key) for key in ("proxy", "proxy_template", "proxies", "proxy_api_url", "white_api_url", "api_url")))
        if should_probe_proxy and select_phone_proxy is not None:
            try:
                proxy_result = select_phone_proxy(
                    proxy,
                    country=phone_slot.country,
                    provider=phone_slot.provider,
                    country_cfg={"country": phone_slot.country},
                )
            except Exception as exc:
                proxy_result = {"ok": False, "error": f"phone_proxy_select_failed:{exc}"}
            if not proxy_result.get("ok"):
                return {
                    "ok": False,
                    "error": "phone_proxy_unavailable",
                    "message": proxy_result.get("error", "phone_proxy_unavailable"),
                    "phone": phone_slot.phone,
                }
            selected_proxy = proxy_result.get("proxy") or ""
            if apply_proxy_to_session is not None:
                apply_proxy_to_session(session, selected_proxy)
            if selected_proxy:
                print(f"[*] Phone proxy ready: region={proxy_result.get('region', '')} ip={proxy_result.get('ip', '')}")

    if _is_rental_slot(phone_slot) and not _prepare_provider_for_send(phone_slot):
        return {"ok": False, "error": f"{phone_slot.provider}_prepare_failed", "phone": phone_slot.phone}

    phone = normalize_phone(phone_slot.phone)
    print(f"[*] Phone verification: {phone} (reuse {phone_slot.reuse_count + 1}/{phone_slot.max_reuse_count})")

    send_result = _send_phone_otp_with_retries(session, did, current_url, phone_slot, phone, sentinel=sentinel, proxy=selected_proxy)
    phone_pool.save_state()
    if not send_result.get("ok"):
        if _is_rental_slot(phone_slot) and _phone_number_already_in_use(send_result):
            print(f"  [{phone_slot.provider}] phone already in use; cancelling rental {phone_slot.rental_handle} before retry")
            _cancel_provider_activation(phone_slot)
            phone_pool.save_state()
        elif _is_terminal_send_rejection(send_result):
            detail = send_result.get("error_code") or send_result.get("status_code", 0)
            _retire_phone_slot_for_batch(phone_pool, phone_slot, f"phone_send_failed:{detail}")
        elif _is_rental_slot(phone_slot) and not _should_keep_activation_after_send_failure(send_result):
            _cancel_provider_activation(phone_slot)
            phone_pool.save_state()
        detail = send_result.get("error_code") or send_result.get("status_code", 0)
        return {
            "ok": False,
            "error": f"phone_send_failed:{detail}",
            "body": send_result.get("body", ""),
            "message": send_result.get("message", ""),
            "phone": phone,
        }

    print(f"[*] Phone OTP sent to {phone}, polling for code...")
    code = _sms_provider_adapter(phone_slot).wait_code()

    if not code:
        if _is_rental_slot(phone_slot):
            print(f"  [{phone_slot.provider}] SMS timeout; cancelling rental {phone_slot.rental_handle} so this run can buy a new number")
            _cancel_provider_activation(phone_slot)
            phone_pool.save_state()
        return {
            "ok": False,
            "error": "phone_sms_timeout",
            "phone": phone,
            "message": f"SMS code not received within {phone_slot.sms_timeout}s",
        }

    print(f"[*] SMS code received: {code}")
    validate_result = validate_phone_otp(session, did, code, sentinel=sentinel, proxy=selected_proxy)
    if not validate_result.get("ok"):
        if _is_rental_slot(phone_slot):
            if _is_terminal_validate_rejection(validate_result):
                print(f"  [{phone_slot.provider}] phone rejected by OpenAI; cancelling rental {phone_slot.rental_handle} so next round buys a new number")
            _cancel_provider_activation(phone_slot)
            phone_pool.save_state()
        return {
            "ok": False,
            "error": f"phone_validate_failed:{validate_result.get('status_code', 0)}",
            "body": validate_result.get("body", ""),
            "phone": phone,
        }

    phone_slot.phone = phone
    phone_slot.last_sms_code = str(code)
    phone_pool.mark_used(phone_slot)
    activation_id = phone_slot.activation_id
    reuse_count = phone_slot.reuse_count
    max_reuse_count = phone_slot.max_reuse_count
    remaining = phone_slot.remaining
    if _is_rental_slot(phone_slot) and phone_slot.is_exhausted:
        print(f"  [{phone_slot.provider}] rental {phone_slot.rental_handle} reached reuse limit; completing now")
        _complete_provider_activation(phone_slot)
        phone_pool.save_state()

    return {
        "ok": True,
        "phone": phone,
        "provider": phone_slot.provider,
        "activation_id": activation_id,
        "reuse_count": reuse_count,
        "max_reuse_count": max_reuse_count,
        "remaining": remaining,
        "next_url": validate_result.get("continue_url", ""),
    }


def create_phone_pool(
    max_reuse_count: int = 0,
    send_cooldown_seconds: int | None = None,
    source_override: str | None = None,
) -> PhonePool:
    cfg = _phone_reuse_cfg()
    # Validate the *effective* source: an explicit override is what the caller
    # asked for, so a stale config value must not block it -- but a stale
    # override still fails, because coercion alone cannot tell "unset" from
    # "removed" and would silently rent from the default provider.
    raw_source = source_override if source_override else (cfg.get("source") or cfg.get("mode"))
    error = phone_reuse_source_error(raw_source)
    if error:
        raise ValueError(error)
    source = sms_providers.resolve_provider(raw_source)
    max_reuse = max_reuse_count or _int_value(cfg.get("max_reuse_count"), 1)
    send_cooldown = (
        max(0, int(send_cooldown_seconds))
        if send_cooldown_seconds is not None
        else _send_cooldown_seconds(cfg)
    )
    send_retries = _send_retry_attempts(cfg)
    send_retry_delay = _send_retry_delay_seconds(cfg)
    number_attempts = _number_attempts(cfg, source)
    phones: list[PhoneSlot] = []

    provider_cfg = _provider_cfg(cfg, source)
    api_key = _provider_api_key(cfg, source)
    if api_key:
        endpoint = (
            str(provider_cfg.get("endpoint") or "").strip()
            or sms_providers.default_endpoint(source, DEFAULT_ENDPOINT)
        )
        pool_size = max(1, _int_value(provider_cfg.get("pool_size"), 1))
        service = normalize_service(provider_cfg.get("service") or OPENAI_SERVICE_CODE)
        country = provider_cfg.get("country") or GHANA_COUNTRY_CODE
        for index in range(pool_size):
            phones.append(PhoneSlot(
                phone="",
                provider=source,
                api_key=api_key,
                endpoint=endpoint,
                service=service,
                country=country,
                min_price=str(provider_cfg.get("min_price") or "").strip(),
                max_price=str(provider_cfg.get("max_price") or "0.06").strip(),
                target_price=str(provider_cfg.get("target_price") or "0.054").strip(),
                provider_ids=str(provider_cfg.get("provider_ids") or "").strip(),
                max_reuse_count=max_reuse,
                slot_id=f"{source}:{index}",
                sms_timeout=_int_value(provider_cfg.get("sms_timeout"), 120),
                sms_poll_interval=_int_value(provider_cfg.get("sms_poll_interval"), 5),
                send_cooldown_seconds=_int_value(provider_cfg.get("send_cooldown_seconds"), send_cooldown),
                send_retry_attempts=_int_value(provider_cfg.get("send_retry_attempts"), send_retries),
                send_retry_delay_seconds=_int_value(provider_cfg.get("send_retry_delay_seconds"), send_retry_delay),
                number_attempts=_int_value(provider_cfg.get("number_attempts"), number_attempts),
            ))

    pool = PhonePool(phones=phones, state_file=str(cfg.get("state_file") or "runtime/phone_reuse_state.json"))
    pool.load_state()
    return pool


def print_phone_pool_status(pool: PhonePool):
    print(f"\n{'=' * 50}")
    print("  Phone Pool Status")
    print(f"{'=' * 50}")
    print(f"  Total phones: {len(pool.phones)}")
    print(f"  Available: {pool.available_count}")
    print(f"  Total capacity: {pool.total_capacity}")
    print()
    for index, phone in enumerate(pool.phones):
        status = "EXHAUSTED" if phone.is_exhausted else "available"
        current = " <-- CURRENT" if index == pool.current_index else ""
        provider = f" [{phone.provider}]" if phone.provider != "legacy" else ""
        display = phone.phone or "(pending acquire)"
        service = f" service={phone.service}" if _is_rental_slot(phone) else ""
        country = f" country={phone.country}" if _is_rental_slot(phone) else ""
        print(
            f"  [{index}] {display}{provider}{service}{country} | "
            f"reuse: {phone.reuse_count}/{phone.max_reuse_count} | {status}{current}"
        )
    print(f"{'=' * 50}\n")


__all__ = [
    "PhonePool",
    "PhoneSlot",
    "complete_phone_verification_with_reuse",
    "create_phone_pool",
    "has_phone_reuse_config",
    "missing_key_hint",
    "print_phone_pool_status",
    "provider_key_collisions",
    "provider_key_status",
    "rental_client",
    "rental_handle_for",
    "rental_protocol",
    "send_phone_otp",
    "validate_phone_otp",
]

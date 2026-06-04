"""Phone verification pool for registration.

SMSBower activations are kept open across timeouts so a late code can still be
retried. Once the configured reuse count is reached, the next SMS round resets
the exhausted activation and buys a new number.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import CFG
from .smsbower import (
    DEFAULT_ENDPOINT,
    GHANA_COUNTRY_CODE,
    OPENAI_SERVICE_CODE,
    SmsBowerClient,
    normalize_country,
    normalize_phone,
    normalize_service,
)
from .nextsms import (
    DEFAULT_ENDPOINT as NEXTSMS_DEFAULT_ENDPOINT,
    DEFAULT_COUNTRY_CODE as NEXTSMS_DEFAULT_COUNTRY_CODE,
    OPENAI_SERVICE_CODE as NEXTSMS_OPENAI_SERVICE_CODE,
    NexSmsClient,
    normalize_country as normalize_nextsms_country,
    normalize_service as normalize_nextsms_service,
)


PLACEHOLDER_KEYS = {"", "YOUR_SMSBOWER_API_KEY", "$SMSBOWER_API_KEY", "YOUR_NEXTSMS_API_KEY", "$NEXTSMS_API_KEY"}


@dataclass
class PhoneSlot:
    phone: str
    sms_api_url: str = ""
    provider: str = "legacy"
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    service: str = OPENAI_SERVICE_CODE
    country: object = GHANA_COUNTRY_CODE
    min_price: str = ""
    max_price: str = "0.06"
    target_price: str = "0.054"
    activation_id: str = ""
    reuse_count: int = 0
    max_reuse_count: int = 1
    last_used_at: int = 0
    last_send_at: int = 0
    total_verified: int = 0
    slot_id: str = ""
    sms_timeout: int = 120
    sms_poll_interval: int = 5
    send_cooldown_seconds: int = 0
    send_retry_attempts: int = 1
    send_retry_delay_seconds: int = 0
    number_attempts: int = 3
    last_sms_code: str = ""
    pricing_option: int = 0

    @property
    def is_exhausted(self) -> bool:
        return self.reuse_count >= self.max_reuse_count

    @property
    def remaining(self) -> int:
        return max(0, self.max_reuse_count - self.reuse_count)

    def mark_used(self):
        self.reuse_count += 1
        self.total_verified += 1
        self.last_used_at = int(time.time())


@dataclass
class PhonePool:
    phones: list[PhoneSlot] = field(default_factory=list)
    current_index: int = 0
    state_file: str = ""
    lock: object = field(default_factory=threading.RLock, repr=False)

    @property
    def current(self) -> Optional[PhoneSlot]:
        if not self.phones:
            return None
        return self.phones[self.current_index % len(self.phones)]

    @property
    def available_count(self) -> int:
        return sum(1 for phone in self.phones if not phone.is_exhausted)

    @property
    def total_capacity(self) -> int:
        return sum(phone.remaining for phone in self.phones)

    def get_next_available(self) -> Optional[PhoneSlot]:
        if not self.phones:
            return None
        for _ in range(len(self.phones)):
            phone = self.phones[self.current_index % len(self.phones)]
            if not phone.is_exhausted:
                return phone
            self.current_index = (self.current_index + 1) % len(self.phones)
        return None

    def mark_used(self, phone: PhoneSlot):
        phone.mark_used()
        if phone.is_exhausted and self.phones:
            self.current_index = (self.current_index + 1) % len(self.phones)
        self.save_state()

    def save_state(self):
        if not self.state_file:
            return
        state = {
            "current_index": self.current_index,
            "phones": [_state_for_phone(phone) for phone in self.phones],
            "updated_at": int(time.time()),
        }
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self):
        if not self.state_file:
            return
        path = Path(self.state_file)
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[!] Failed to load phone pool state: {exc}")
            return
        self.current_index = int(state.get("current_index") or 0)
        saved_by_slot = {
            str(item.get("slot_id") or ""): item
            for item in state.get("phones", [])
            if isinstance(item, dict) and item.get("slot_id")
        }
        saved_by_phone = {
            str(item.get("phone") or ""): item
            for item in state.get("phones", [])
            if isinstance(item, dict) and item.get("phone")
        }
        for index, phone in enumerate(self.phones):
            saved = saved_by_slot.get(phone.slot_id) or saved_by_phone.get(phone.phone)
            if not saved or not _saved_state_matches_slot(phone, saved):
                continue
            if saved.get("phone"):
                phone.phone = str(saved.get("phone") or "")
            phone.activation_id = str(saved.get("activation_id") or "")
            phone.reuse_count = int(saved.get("reuse_count") or 0)
            phone.last_used_at = int(saved.get("last_used_at") or 0)
            phone.last_send_at = int(saved.get("last_send_at") or 0)
            phone.total_verified = int(saved.get("total_verified") or 0)
            phone.last_sms_code = str(saved.get("last_sms_code") or "")
            phone.slot_id = phone.slot_id or str(saved.get("slot_id") or f"slot:{index}")
            if phone.provider == "smsbower" and (not phone.phone or not phone.activation_id):
                phone.reuse_count = 0
                phone.last_sms_code = ""

    def reset_exhausted_smsbower_slots(self) -> int:
        reset_count = 0
        for phone in self.phones:
            if phone.provider not in {"smsbower", "nextsms"} or not phone.is_exhausted:
                continue
            old_phone = phone.phone
            old_activation_id = phone.activation_id
            if old_activation_id:
                print(
                    f"  [{phone.provider}] activation "
                    f"{old_activation_id} reached reuse limit; completing before next purchase"
                )
                _complete_provider_activation(phone)
            else:
                _reset_provider_slot(phone)
            print(
                f"  [{phone.provider}] reset exhausted phone "
                f"{old_phone or '(pending)'}; next send will acquire a new number"
            )
            reset_count += 1
        if reset_count:
            self.save_state()
        return reset_count


def _state_for_phone(phone: PhoneSlot) -> dict:
    return {
        "slot_id": phone.slot_id,
        "phone": phone.phone,
        "sms_api_url": phone.sms_api_url,
        "provider": phone.provider,
        "endpoint": phone.endpoint,
        "service": phone.service,
        "country": phone.country,
        "min_price": phone.min_price,
        "max_price": phone.max_price,
        "target_price": phone.target_price,
        "activation_id": phone.activation_id,
        "reuse_count": phone.reuse_count,
        "max_reuse_count": phone.max_reuse_count,
        "last_used_at": phone.last_used_at,
        "last_send_at": phone.last_send_at,
        "total_verified": phone.total_verified,
        "send_cooldown_seconds": phone.send_cooldown_seconds,
        "send_retry_attempts": phone.send_retry_attempts,
        "send_retry_delay_seconds": phone.send_retry_delay_seconds,
        "number_attempts": phone.number_attempts,
        "last_sms_code": phone.last_sms_code,
        "pricing_option": phone.pricing_option,
    }


def _saved_state_matches_slot(phone: PhoneSlot, saved: dict) -> bool:
    saved_provider = str(saved.get("provider") or "").strip()
    if saved_provider and saved_provider != phone.provider:
        return False
    if phone.provider in {"smsbower", "nextsms"}:
        return True
    saved_phone = normalize_phone(saved.get("phone") or "")
    if saved_phone and saved_phone != normalize_phone(phone.phone):
        return False
    saved_url = str(saved.get("sms_api_url") or "").strip()
    if saved_url and saved_url != str(phone.sms_api_url or "").strip():
        return False
    return True


def _phone_reuse_cfg():
    cfg = CFG.get("phone_reuse") if isinstance(CFG.get("phone_reuse"), dict) else {}
    return cfg


def _send_cooldown_seconds(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return _int_value(cfg.get("send_cooldown_seconds"), 45)


def _send_retry_attempts(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return max(1, _int_value(cfg.get("send_retry_attempts"), 3))


def _send_retry_delay_seconds(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return max(0, _int_value(cfg.get("send_retry_delay_seconds"), 45))


def _number_attempts(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    smsbower_cfg = cfg.get("smsbower") if isinstance(cfg.get("smsbower"), dict) else {}
    return max(1, _int_value(smsbower_cfg.get("number_attempts") or cfg.get("number_attempts"), 3))


def _resolve_secret(value: str, env_name: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("$") and len(raw) > 1:
        return os.environ.get(raw[1:], "").strip()
    if raw in PLACEHOLDER_KEYS:
        return os.environ.get(env_name, "").strip()
    return raw


def _int_value(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _smsbower_api_key(cfg: dict | None = None) -> str:
    cfg = cfg if isinstance(cfg, dict) else (_phone_reuse_cfg().get("smsbower") or {})
    return _resolve_secret(str(cfg.get("api_key") or ""), "SMSBOWER_API_KEY")


def _nextsms_api_key(cfg: dict | None = None) -> str:
    cfg = cfg if isinstance(cfg, dict) else (_phone_reuse_cfg().get("nextsms") or {})
    return _resolve_secret(str(cfg.get("api_key") or ""), "NEXTSMS_API_KEY")


def has_phone_reuse_config() -> bool:
    cfg = _phone_reuse_cfg()
    source = _phone_source(cfg)
    if source == "phone_pool":
        return bool(cfg.get("phone_pool"))
    if source == "smsbower":
        smsbower_cfg = cfg.get("smsbower") if isinstance(cfg.get("smsbower"), dict) else {}
        return bool(_smsbower_api_key(smsbower_cfg))
    if source == "nextsms":
        nextsms_cfg = cfg.get("nextsms") if isinstance(cfg.get("nextsms"), dict) else {}
        return bool(_nextsms_api_key(nextsms_cfg))
    smsbower_cfg = cfg.get("smsbower") if isinstance(cfg.get("smsbower"), dict) else {}
    if _smsbower_api_key(smsbower_cfg):
        return True
    nextsms_cfg = cfg.get("nextsms") if isinstance(cfg.get("nextsms"), dict) else {}
    if _nextsms_api_key(nextsms_cfg):
        return True
    if cfg.get("phone_pool"):
        return True
    paypal_cfg = CFG.get("paypal_auto") if isinstance(CFG.get("paypal_auto"), dict) else {}
    if paypal_cfg.get("phone_numbers"):
        return True
    return bool(paypal_cfg.get("phone_number") and paypal_cfg.get("sms_api_url"))


def _phone_source(cfg: dict | None = None) -> str:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    source = str(cfg.get("source") or cfg.get("mode") or "auto").strip().lower()
    if source in {"smsbower", "sms_bower", "platform", "provider"}:
        return "smsbower"
    if source in {"nextsms", "nexsms", "next_sms", "nextactionplus"}:
        return "nextsms"
    if source in {"phone_pool", "static", "legacy", "sms_link", "link"}:
        return "phone_pool"
    return "auto"


def create_phone_pool(max_reuse_count: int = 0, send_cooldown_seconds: int | None = None) -> PhonePool:
    cfg = _phone_reuse_cfg()
    source = _phone_source(cfg)
    max_reuse = max_reuse_count or _int_value(cfg.get("max_reuse_count"), 1)
    send_cooldown = (
        max(0, int(send_cooldown_seconds))
        if send_cooldown_seconds is not None
        else _send_cooldown_seconds(cfg)
    )
    send_retries = _send_retry_attempts(cfg)
    send_retry_delay = _send_retry_delay_seconds(cfg)
    number_attempts = _number_attempts(cfg)
    phones: list[PhoneSlot] = []

    smsbower_cfg = cfg.get("smsbower") if isinstance(cfg.get("smsbower"), dict) else {}
    api_key = _smsbower_api_key(smsbower_cfg)
    if api_key and source not in {"phone_pool", "nextsms"}:
        pool_size = max(1, _int_value(smsbower_cfg.get("pool_size"), 1))
        service = normalize_service(smsbower_cfg.get("service") or OPENAI_SERVICE_CODE)
        country = smsbower_cfg.get("country") or GHANA_COUNTRY_CODE
        for index in range(pool_size):
            phones.append(PhoneSlot(
                phone="",
                provider="smsbower",
                api_key=api_key,
                endpoint=str(smsbower_cfg.get("endpoint") or DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT,
                service=service,
                country=country,
                min_price=str(smsbower_cfg.get("min_price") or "").strip(),
                max_price=str(smsbower_cfg.get("max_price") or "0.06").strip(),
                target_price=str(smsbower_cfg.get("target_price") or "0.054").strip(),
                max_reuse_count=max_reuse,
                slot_id=f"smsbower:{index}",
                sms_timeout=_int_value(smsbower_cfg.get("sms_timeout"), 120),
                sms_poll_interval=_int_value(smsbower_cfg.get("sms_poll_interval"), 5),
                send_cooldown_seconds=_int_value(smsbower_cfg.get("send_cooldown_seconds"), send_cooldown),
                send_retry_attempts=_int_value(smsbower_cfg.get("send_retry_attempts"), send_retries),
                send_retry_delay_seconds=_int_value(smsbower_cfg.get("send_retry_delay_seconds"), send_retry_delay),
                number_attempts=_int_value(smsbower_cfg.get("number_attempts"), number_attempts),
            ))

    nextsms_cfg = cfg.get("nextsms") if isinstance(cfg.get("nextsms"), dict) else {}
    nextsms_api_key = _nextsms_api_key(nextsms_cfg)
    if nextsms_api_key and source == "nextsms":
        pool_size = max(1, _int_value(nextsms_cfg.get("pool_size"), 1))
        service = normalize_nextsms_service(nextsms_cfg.get("service") or NEXTSMS_OPENAI_SERVICE_CODE)
        country = nextsms_cfg.get("country") or NEXTSMS_DEFAULT_COUNTRY_CODE
        pricing_option = _int_value(nextsms_cfg.get("pricing_option"), 0)
        for index in range(pool_size):
            phones.append(PhoneSlot(
                phone="",
                provider="nextsms",
                api_key=nextsms_api_key,
                endpoint=str(nextsms_cfg.get("endpoint") or NEXTSMS_DEFAULT_ENDPOINT).strip() or NEXTSMS_DEFAULT_ENDPOINT,
                service=service,
                country=country,
                max_reuse_count=max_reuse,
                slot_id=f"nextsms:{index}",
                sms_timeout=_int_value(nextsms_cfg.get("sms_timeout"), 120),
                sms_poll_interval=_int_value(nextsms_cfg.get("sms_poll_interval"), 5),
                send_cooldown_seconds=_int_value(nextsms_cfg.get("send_cooldown_seconds"), send_cooldown),
                send_retry_attempts=_int_value(nextsms_cfg.get("send_retry_attempts"), send_retries),
                send_retry_delay_seconds=_int_value(nextsms_cfg.get("send_retry_delay_seconds"), send_retry_delay),
                number_attempts=_int_value(nextsms_cfg.get("number_attempts"), number_attempts),
                pricing_option=pricing_option,
            ))

    if not phones and source not in {"smsbower", "nextsms"}:
        for index, entry in enumerate(cfg.get("phone_pool") or []):
            slot = _slot_from_static_entry(entry, max_reuse, f"phone_pool:{index}")
            if slot:
                phones.append(slot)

    if not phones and source not in {"smsbower", "nextsms"}:
        paypal_cfg = CFG.get("paypal_auto") if isinstance(CFG.get("paypal_auto"), dict) else {}
        phone_numbers = paypal_cfg.get("phone_numbers") or []
        for index, entry in enumerate(phone_numbers):
            slot = _slot_from_static_entry(entry, max_reuse, f"paypal_auto:{index}")
            if slot:
                phones.append(slot)
        if not phones:
            phone = str(paypal_cfg.get("phone_number") or "").strip()
            sms_api_url = str(paypal_cfg.get("sms_api_url") or "").strip()
            if phone and sms_api_url:
                phones.append(PhoneSlot(
                    phone=normalize_phone(phone),
                    sms_api_url=sms_api_url,
                    provider="legacy",
                    max_reuse_count=max_reuse,
                    slot_id="paypal_auto:0",
                    sms_timeout=_int_value(paypal_cfg.get("sms_timeout"), 120),
                    sms_poll_interval=_int_value(paypal_cfg.get("sms_poll_interval"), 5),
                    send_cooldown_seconds=send_cooldown,
                    send_retry_attempts=send_retries,
                    send_retry_delay_seconds=send_retry_delay,
                ))

    pool = PhonePool(phones=phones, state_file=str(cfg.get("state_file") or "runtime/phone_reuse_state.json"))
    pool.load_state()
    return pool


def _slot_from_static_entry(entry: dict, max_reuse: int, slot_id: str) -> Optional[PhoneSlot]:
    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or "legacy").strip()
    if provider == "smsbower":
        api_key = _resolve_secret(str(entry.get("api_key") or ""), "SMSBOWER_API_KEY")
        if not api_key:
            return None
        return PhoneSlot(
            phone=normalize_phone(entry.get("phone") or ""),
            provider="smsbower",
            api_key=api_key,
            endpoint=str(entry.get("endpoint") or DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT,
            service=normalize_service(entry.get("service") or OPENAI_SERVICE_CODE),
            country=entry.get("country") or GHANA_COUNTRY_CODE,
            min_price=str(entry.get("min_price") or "").strip(),
            max_price=str(entry.get("max_price") or "0.06").strip(),
            target_price=str(entry.get("target_price") or "0.054").strip(),
            max_reuse_count=max_reuse,
            slot_id=slot_id,
            sms_timeout=_int_value(entry.get("sms_timeout"), 120),
            sms_poll_interval=_int_value(entry.get("sms_poll_interval"), 5),
            send_cooldown_seconds=_int_value(entry.get("send_cooldown_seconds"), _send_cooldown_seconds()),
            send_retry_attempts=_int_value(entry.get("send_retry_attempts"), _send_retry_attempts()),
            send_retry_delay_seconds=_int_value(entry.get("send_retry_delay_seconds"), _send_retry_delay_seconds()),
        )
    phone = normalize_phone(entry.get("phone") or "")
    sms_api_url = str(entry.get("sms_api_url") or "").strip()
    if not phone or not sms_api_url:
        return None
    return PhoneSlot(
        phone=phone,
        sms_api_url=sms_api_url,
        provider="legacy",
        max_reuse_count=max_reuse,
        slot_id=slot_id,
        sms_timeout=_int_value(entry.get("sms_timeout"), 120),
        sms_poll_interval=_int_value(entry.get("sms_poll_interval"), 5),
        send_cooldown_seconds=_int_value(entry.get("send_cooldown_seconds"), _send_cooldown_seconds()),
        send_retry_attempts=_int_value(entry.get("send_retry_attempts"), _send_retry_attempts()),
        send_retry_delay_seconds=_int_value(entry.get("send_retry_delay_seconds"), _send_retry_delay_seconds()),
    )


def _country_candidates(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        candidates = [normalize_country(item) for item in value]
    else:
        candidates = [normalize_country(value)]
    return [item for item in candidates if item]


def _smsbower_client(slot: PhoneSlot) -> SmsBowerClient:
    return SmsBowerClient(api_key=slot.api_key, endpoint=slot.endpoint)


def _nextsms_client(slot: PhoneSlot) -> NexSmsClient:
    return NexSmsClient(api_key=slot.api_key, endpoint=slot.endpoint)


def _acquire_smsbower_number(slot: PhoneSlot) -> bool:
    client = _smsbower_client(slot)
    countries = _country_candidates(slot.country)
    for country in countries:
        try:
            activation = client.get_number(
                service=slot.service,
                country=country,
                min_price=slot.min_price,
                max_price=slot.max_price,
            )
        except Exception as exc:
            error = str(exc)
            print(f"  [smsbower] country={country} acquire failed: {error}")
            if "NO_BALANCE" in error or "BAD_KEY" in error:
                return False
            continue
        previous_phone = slot.phone
        slot.phone = normalize_phone(activation.phone)
        slot.activation_id = activation.activation_id
        slot.service = activation.service
        slot.country = activation.country
        if not previous_phone or previous_phone != slot.phone:
            slot.reuse_count = 0
            slot.last_sms_code = ""
        print(
            "  [smsbower] acquired "
            f"{slot.phone} (id={slot.activation_id}, country={country}, price={activation.price})"
        )
        return True
    return False


def _nextsms_country_candidates(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        candidates = [normalize_nextsms_country(item) for item in value]
    else:
        candidates = [normalize_nextsms_country(value)]
    return [item for item in candidates if item]


def _acquire_nextsms_number(slot: PhoneSlot) -> bool:
    client = _nextsms_client(slot)
    countries = _nextsms_country_candidates(slot.country)
    for country in countries:
        try:
            activation = client.get_number(
                service=slot.service or NEXTSMS_OPENAI_SERVICE_CODE,
                country=country,
                pricing_option=slot.pricing_option,
            )
        except Exception as exc:
            print(f"  [nextsms] country={country} acquire failed: {exc}")
            continue
        previous_phone = slot.phone
        slot.phone = normalize_phone(activation.phone)
        slot.activation_id = activation.activation_id
        slot.service = activation.service
        slot.country = activation.country
        if not previous_phone or previous_phone != slot.phone:
            slot.reuse_count = 0
            slot.last_sms_code = ""
        print(
            "  [nextsms] acquired "
            f"{slot.phone} (id={slot.activation_id}, country={country}, price={activation.price})"
        )
        return True
    return False


def _prepare_smsbower_for_send(slot: PhoneSlot) -> bool:
    if not slot.activation_id or not slot.phone:
        return _acquire_smsbower_number(slot)
    if slot.reuse_count <= 0:
        return True
    if _smsbower_client(slot).request_additional(slot.activation_id):
        print(f"  [smsbower] activation {slot.activation_id} ready for another code")
        return True
    print(f"  [smsbower] activation {slot.activation_id} could not request another code; cancelling and acquiring a new number")
    _cancel_smsbower_activation(slot)
    return _acquire_smsbower_number(slot)


def _prepare_nextsms_for_send(slot: PhoneSlot) -> bool:
    if not slot.activation_id or not slot.phone:
        return _acquire_nextsms_number(slot)
    if slot.reuse_count <= 0:
        return True
    if _nextsms_client(slot).request_additional(slot.activation_id):
        print(f"  [nextsms] order {slot.activation_id} ready for another code")
        return True
    print(f"  [nextsms] order {slot.activation_id} could not request another code; acquiring a new number")
    _reset_provider_slot(slot)
    return _acquire_nextsms_number(slot)


def _prepare_provider_for_send(slot: PhoneSlot) -> bool:
    if slot.provider == "smsbower":
        return _prepare_smsbower_for_send(slot)
    if slot.provider == "nextsms":
        return _prepare_nextsms_for_send(slot)
    return True


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
    return status == 429 or "phone_recently_used" in text or "this phone number was recently used" in text


def _retire_phone_slot_for_batch(phone_pool: PhonePool, phone_slot: PhoneSlot, reason: str):
    if phone_slot.provider in {"smsbower", "nextsms"}:
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
        result = send_phone_otp(session, did, current_url, phone, sentinel=sentinel, proxy=proxy)
        phone_slot.last_send_at = int(time.time())
        last_result = result
        if result.get("ok"):
            return result
        if attempt >= attempts or not _should_keep_activation_after_send_failure(result):
            return result
        wait = max(retry_delay, int(phone_slot.send_cooldown_seconds or 0))
        if wait > 0:
            detail = result.get("error_code") or result.get("status_code", 0)
            print(f"[*] Phone OTP send retry {attempt}/{attempts} after {wait}s ({detail})")
            time.sleep(wait)
    return last_result


def _wait_smsbower_code(slot: PhoneSlot) -> Optional[str]:
    return _smsbower_client(slot).wait_for_code(
        slot.activation_id,
        timeout=slot.sms_timeout,
        poll_interval=slot.sms_poll_interval,
        previous_code=slot.last_sms_code,
    )


def _wait_nextsms_code(slot: PhoneSlot) -> Optional[str]:
    return _nextsms_client(slot).wait_for_code(
        slot.activation_id,
        timeout=slot.sms_timeout,
        poll_interval=slot.sms_poll_interval,
        previous_code=slot.last_sms_code,
    )


def _reset_provider_slot(slot: PhoneSlot):
    slot.phone = ""
    slot.activation_id = ""
    slot.reuse_count = 0
    slot.last_send_at = 0
    slot.last_sms_code = ""


def _reset_smsbower_slot(slot: PhoneSlot):
    _reset_provider_slot(slot)


def _complete_smsbower_activation(slot: PhoneSlot):
    if slot.activation_id:
        _smsbower_client(slot).complete(slot.activation_id)
    _reset_smsbower_slot(slot)


def _cancel_smsbower_activation(slot: PhoneSlot):
    if slot.activation_id:
        _smsbower_client(slot).cancel(slot.activation_id)
    _reset_smsbower_slot(slot)


def _complete_provider_activation(slot: PhoneSlot):
    if slot.provider == "smsbower":
        _complete_smsbower_activation(slot)
        return
    if slot.provider == "nextsms":
        if slot.activation_id:
            _nextsms_client(slot).complete(slot.activation_id)
        _reset_provider_slot(slot)
        return
    _reset_provider_slot(slot)


def _cancel_provider_activation(slot: PhoneSlot):
    if slot.provider == "smsbower":
        _cancel_smsbower_activation(slot)
        return
    if slot.provider == "nextsms":
        if slot.activation_id:
            _nextsms_client(slot).cancel(slot.activation_id)
        _reset_provider_slot(slot)
        return
    _reset_provider_slot(slot)


def send_phone_otp(session, did, current_url, phone: str, sentinel=None, proxy=None) -> dict:
    from .codex_sentinel import load_cached_sentinel, with_sentinel

    if sentinel is None:
        sentinel = load_cached_sentinel()
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36",
        "oai-device-id": did,
        "Referer": current_url,
        "content-type": "application/json",
    }
    response = session.post(
        "https://auth.openai.com/api/accounts/add-phone/send",
        headers=with_sentinel(headers, sentinel),
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


def get_sms_baseline(sms_api_url: str) -> dict:
    from .paypal_auto import _sms_baseline
    return _sms_baseline(sms_api_url)


def poll_sms_code(sms_api_url: str, baseline: dict, timeout: int = 120, poll_interval: int = 5) -> Optional[str]:
    from .paypal_auto import _poll_sms_code
    return _poll_sms_code(sms_api_url, baseline, timeout=timeout, poll_interval=poll_interval)


def validate_phone_otp(session, did, code: str, sentinel=None, proxy=None) -> dict:
    from .codex_sentinel import load_cached_sentinel, with_sentinel

    if sentinel is None:
        sentinel = load_cached_sentinel()
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36",
        "oai-device-id": did,
        "Referer": "https://auth.openai.com/phone-verification",
        "content-type": "application/json",
    }
    response = session.post(
        "https://auth.openai.com/api/accounts/phone-otp/validate",
        headers=with_sentinel(headers, sentinel),
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
        max((int(phone.number_attempts or 1) for phone in phone_pool.phones if phone.provider in {"smsbower", "nextsms"}), default=1),
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
        should_retry = _should_retry_with_new_smsbower_number(phone_pool, result)
        if should_retry and attempt >= attempts:
            if _should_retry_until_success_with_new_smsbower_number(result):
                attempts = attempt + 1
            else:
                attempts = max(attempts, _minimum_smsbower_attempts_for_retry(result))
        if attempt >= attempts or not should_retry:
            return result
        print(
            "[*] Phone verification retry with new provider number "
            f"{attempt + 1}/{attempts}: {result.get('error', 'unknown')}"
        )
        phone_pool.reset_exhausted_smsbower_slots()
        attempt += 1
    return last_result or {"ok": False, "error": "phone_verification_failed"}


def _minimum_smsbower_attempts_for_retry(result: dict) -> int:
    error = str(result.get("error") or "").strip().lower()
    if error == "phone_sms_timeout" or error.startswith("phone_send_failed:"):
        return 2
    return 1


def _should_retry_until_success_with_new_smsbower_number(result: dict) -> bool:
    error = str(result.get("error") or "").strip().lower()
    body = str(result.get("body") or "").lower()
    message = str(result.get("message") or "").lower()
    text = f"{error} {body} {message}"
    return error.startswith("phone_send_failed:") and "fraud_guard" in text


def _should_retry_with_new_smsbower_number(phone_pool: PhonePool, result: dict) -> bool:
    if not any(phone.provider in {"smsbower", "nextsms"} for phone in phone_pool.phones):
        return False
    error = str(result.get("error") or "").strip().lower()
    body = str(result.get("body") or "").lower()
    if error == "phone_sms_timeout":
        return True
    if error in {"smsbower_prepare_failed", "nextsms_prepare_failed", "phone_pool_exhausted"}:
        return False
    if error.startswith("phone_send_failed:"):
        return any(
            marker in error or marker in body
            for marker in ("fraud_guard", "unsupported_phone_number", "invalid_phone_number")
        )
    if error.startswith("phone_validate_failed:"):
        return "429" in error or "phone_recently_used" in body or "recently used" in body
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
    phone_pool.reset_exhausted_smsbower_slots()
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

    if phone_slot.provider in {"smsbower", "nextsms"} and not _prepare_provider_for_send(phone_slot):
        return {"ok": False, "error": f"{phone_slot.provider}_prepare_failed", "phone": phone_slot.phone}

    phone = normalize_phone(phone_slot.phone)
    print(f"[*] Phone verification: {phone} (reuse {phone_slot.reuse_count + 1}/{phone_slot.max_reuse_count})")

    send_result = _send_phone_otp_with_retries(session, did, current_url, phone_slot, phone, sentinel=sentinel, proxy=proxy)
    phone_pool.save_state()
    if not send_result.get("ok"):
        if _is_terminal_send_rejection(send_result):
            detail = send_result.get("error_code") or send_result.get("status_code", 0)
            _retire_phone_slot_for_batch(phone_pool, phone_slot, f"phone_send_failed:{detail}")
        elif phone_slot.provider in {"smsbower", "nextsms"} and not _should_keep_activation_after_send_failure(send_result):
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
    if phone_slot.provider == "smsbower":
        code = _wait_smsbower_code(phone_slot)
    elif phone_slot.provider == "nextsms":
        code = _wait_nextsms_code(phone_slot)
    else:
        baseline = get_sms_baseline(phone_slot.sms_api_url)
        code = poll_sms_code(
            phone_slot.sms_api_url,
            baseline,
            timeout=phone_slot.sms_timeout,
            poll_interval=phone_slot.sms_poll_interval,
        )

    if not code:
        if phone_slot.provider in {"smsbower", "nextsms"}:
            print(f"  [{phone_slot.provider}] SMS timeout; cancelling activation {phone_slot.activation_id} so this run can buy a new number")
            _cancel_provider_activation(phone_slot)
            phone_pool.save_state()
        return {
            "ok": False,
            "error": "phone_sms_timeout",
            "phone": phone,
            "message": f"SMS code not received within {phone_slot.sms_timeout}s",
        }

    print(f"[*] SMS code received: {code}")
    validate_result = validate_phone_otp(session, did, code, sentinel=sentinel, proxy=proxy)
    if not validate_result.get("ok"):
        if phone_slot.provider in {"smsbower", "nextsms"}:
            if _is_terminal_validate_rejection(validate_result):
                print(f"  [{phone_slot.provider}] phone rejected by OpenAI; cancelling activation {phone_slot.activation_id} so next round buys a new number")
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
    if phone_slot.provider in {"smsbower", "nextsms"} and phone_slot.is_exhausted:
        print(f"  [{phone_slot.provider}] activation {phone_slot.activation_id} reached reuse limit; completing now")
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
        service = f" service={phone.service}" if phone.provider in {"smsbower", "nextsms"} else ""
        country = f" country={phone.country}" if phone.provider in {"smsbower", "nextsms"} else ""
        print(
            f"  [{index}] {display}{provider}{service}{country} | "
            f"reuse: {phone.reuse_count}/{phone.max_reuse_count} | {status}{current}"
        )
    print(f"{'=' * 50}\n")

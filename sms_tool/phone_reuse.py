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
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from . import sms_providers
from .config import CFG
from .cross_process_gate import cross_process_write_lock
from .nexsms import NexSmsClient
from .operator_output import emit
from .smsbower import (
    DEFAULT_ENDPOINT,
    GHANA_COUNTRY_CODE,
    OPENAI_SERVICE_CODE,
    SmsBowerClient,
    normalize_country,
    normalize_phone,
    normalize_service,
)
from .auth_headers import openai_auth_headers_lower
from .sms_provider_adapter import SmsProviderAdapter, provider_name

#: Retry budgets shared by both protocol families. Renamed off the vendor name
#: when the second family landed: they are per-rental, not per-vendor, and a
#: vendor-named constant applied to another vendor is how "this only ever
#: matched one provider" bugs start.
RENTAL_NO_NUMBERS_MAX_ATTEMPTS = 10
RENTAL_PHONE_IN_USE_MAX_ATTEMPTS = 10

_LOGGER = logging.getLogger(__name__)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace).

    A crash mid-write leaves the previous file intact, so a concurrent reader
    (or the next process) never sees a torn JSON state. The temp file lives in
    the same directory as ``path`` so os.replace is a rename on the same volume.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class PhoneSlot:
    phone: str
    provider: str = sms_providers.DEFAULT_PROVIDER
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    service: str = OPENAI_SERVICE_CODE
    country: object = GHANA_COUNTRY_CODE
    min_price: str = ""
    max_price: str = "0.06"
    target_price: str = "0.054"
    provider_ids: str = ""
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

    @property
    def is_exhausted(self) -> bool:
        return self.reuse_count >= self.max_reuse_count

    @property
    def remaining(self) -> int:
        return max(0, self.max_reuse_count - self.reuse_count)

    @property
    def rental_handle(self) -> str:
        """The key this slot's vendor addresses the rental by.

        The two protocol families disagree on what that key *is*: an
        sms-activate vendor issues an activation id, while NexSMS issues none and
        is addressed by the phone number itself. Client calls take this value, so
        no call site has to know which family it is talking to, and a slot that
        has not rented yet reports ``""`` either way.
        """
        return rental_handle_for(self.provider, self)

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
        # Serialise across processes: two backend processes must not interleave
        # writes, or the same phone number can be handed to two accounts.
        # _atomic_write_text guarantees a reader never sees a half-written file.
        lock_path = path.with_name(f"{path.name}.lock")
        with cross_process_write_lock(lock_path):
            _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))

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
            # A saved slot with no rental has no handle either, so its reuse
            # counter is meaningless -- restart it rather than letting a stale
            # count exhaust a slot that never rented anything. The handle is the
            # right test, not ``activation_id``: a NexSMS slot keeps its number
            # and *never* has an activation id, so testing that field would reset
            # its reuse counter on every load and silently make reuse impossible
            # for that vendor.
            if not phone.rental_handle:
                phone.reuse_count = 0
                phone.last_sms_code = ""

    def reset_exhausted_slots(self) -> int:
        reset_count = 0
        for phone in self.phones:
            if not phone.is_exhausted:
                continue
            old_phone = phone.phone
            handle = phone.rental_handle
            # Branch on the handle, not on ``activation_id``: a NexSMS slot has a
            # live rental and *no* activation id, so testing that field sent it
            # down the "nothing to complete" path. Harmless today only because
            # this vendor's ``complete`` is a no-op -- i.e. it worked by accident.
            if handle:
                print(
                    f"  [{phone.provider}] rental "
                    f"{handle} reached reuse limit; completing before next purchase"
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
        "provider": phone.provider,
        "endpoint": phone.endpoint,
        "service": phone.service,
        "country": phone.country,
        "min_price": phone.min_price,
        "max_price": phone.max_price,
        "target_price": phone.target_price,
        "provider_ids": phone.provider_ids,
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
    }


def _saved_state_matches_slot(phone: PhoneSlot, saved: dict) -> bool:
    """True when ``saved`` describes the same rentable slot as ``phone``.

    Provider and tier are both compared: a saved activation bought under a
    different country or price tier was rented on different terms, so its code
    must not be reused for the current run. A saved slot from the removed static
    phone-pool mode carries ``provider="legacy"`` and therefore never matches,
    which is how old state files are retired without a migration step.
    """
    saved_provider = str(saved.get("provider") or "").strip()
    if saved_provider and saved_provider != phone.provider:
        return False
    saved_service = str(saved.get("service") or "").strip()
    saved_country = str(saved.get("country") or "").strip()
    saved_min_price = str(saved.get("min_price") or "").strip()
    saved_max_price = str(saved.get("max_price") or "").strip()
    saved_provider_ids = str(saved.get("provider_ids") or "").strip()
    return (
        (not saved_service or normalize_service(saved_service) == normalize_service(phone.service))
        and (not saved_country or normalize_country(saved_country) == normalize_country(phone.country))
        and (not saved_min_price or saved_min_price == str(phone.min_price or "").strip())
        and (not saved_max_price or saved_max_price == str(phone.max_price or "").strip())
        and (not saved_provider_ids or saved_provider_ids == str(phone.provider_ids or "").strip())
    )


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


def _number_attempts(cfg: dict | None = None, provider: str | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    provider_cfg = _provider_cfg(cfg, provider or _phone_source(cfg))
    return max(1, _int_value(provider_cfg.get("number_attempts") or cfg.get("number_attempts"), 3))


def _resolve_secret(value: str, provider: str) -> str:
    """Resolve a configured provider key, honouring indirection.

    ``api_key`` may be a literal, ``$ENV_NAME``, or a vendor placeholder such as
    ``YOUR_SMSBOWER_API_KEY``. The latter two read the provider's environment
    variable, so a committed config never has to carry a real key.
    """
    env_name = sms_providers.api_key_env(provider)
    raw = str(value or "").strip()
    if raw.startswith("$") and len(raw) > 1:
        return os.environ.get(raw[1:], "").strip()
    if not raw or raw == f"YOUR_{env_name}":
        return os.environ.get(env_name, "").strip()
    return raw


def _int_value(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _provider_cfg(cfg: dict, provider: str) -> dict:
    """The ``phone_reuse.<provider>`` sub-section, or an empty dict."""
    section = cfg.get(provider)
    return section if isinstance(section, dict) else {}


def _provider_api_key(cfg: dict, provider: str) -> str:
    """Resolve the selected provider's own key."""
    return _resolve_secret(_provider_cfg(cfg, provider).get("api_key") or "", provider)


def phone_reuse_source_error(raw: object) -> str:
    """Operator-facing error for a removed or unknown ``phone_reuse.source``.

    Returns ``""`` when the value is usable. Callers that must fail loudly ask
    this *before* coercing with :func:`_phone_source`, because coercion alone
    cannot tell "unset" from "stale" -- both land on the default provider.
    """
    reason = sms_providers.removed_source_reason(raw)
    if reason:
        return reason
    if str(raw or "").strip() and not sms_providers.normalize_provider(raw):
        known = ", ".join(sms_providers.available_provider_keys())
        return f"unknown phone_reuse.source {str(raw).strip()!r}; expected one of: {known}"
    return ""


def has_phone_reuse_config() -> bool:
    """True when the selected provider has a usable key configured."""
    cfg = _phone_reuse_cfg()
    return bool(_provider_api_key(cfg, _phone_source(cfg)))


def _phone_source(cfg: dict | None = None) -> str:
    """Canonical provider key selected by ``phone_reuse.source``.

    Unset, unknown, and removed-static-pool values all resolve to
    :data:`sms_providers.DEFAULT_PROVIDER`; :func:`phone_reuse_source_error` is
    what turns a removed or unknown spelling into a loud failure.
    """
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return sms_providers.resolve_provider(cfg.get("source") or cfg.get("mode"))


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


def _country_candidates(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        candidates = [normalize_country(item) for item in value]
    else:
        candidates = [normalize_country(value)]
    return [item for item in candidates if item]


def rental_protocol(provider: str) -> str:
    """The protocol family that drives ``provider`` -- the one place it is decided.

    Both client entry points below consult this instead of each re-deriving the
    rule, and an unrecognised provider raises rather than defaulting to the
    sms-activate client: that default would send a NexSMS endpoint sms-activate
    query parameters and surface an opaque vendor error, hiding the real defect.
    """
    spec = sms_providers.provider_spec(provider)
    if spec is not None and spec.has_client:
        return spec.protocol
    raise ValueError(f"unsupported SMS provider {provider!r}")


def rental_client(provider: str, api_key: str, endpoint: str):
    """Protocol-appropriate client for an explicit ``(provider, key, endpoint)``.

    The two families share a call surface -- ``get_number``/``wait_for_code``/
    ``complete``/``cancel`` -- but not a wire format, so the choice is made once
    here rather than at each call site.

    Takes the triple rather than a slot so callers that own no
    :class:`PhoneSlot` -- the standalone ``--phone-register`` entry point --
    share this rule instead of re-deriving it.
    """
    client_class = (
        NexSmsClient
        if rental_protocol(provider) == sms_providers.PROTOCOL_NEXSMS
        else SmsBowerClient
    )
    return client_class(api_key=api_key, endpoint=endpoint)


def rental_handle_for(provider: str, activation) -> str:
    """The key ``provider`` addresses this activation by.

    sms-activate vendors issue an activation id; NexSMS issues none and is
    addressed by the phone number itself. :class:`PhoneSlot` and the standalone
    phone-registration flow both go through here, so the rule cannot drift
    between the two entry points. An activation that has not rented yet reports
    ``""`` either way.

    Deliberately lenient where :func:`rental_protocol` raises: this is a *read*,
    and an unrecognised provider still has an ``activation_id`` worth returning.
    """
    spec = sms_providers.provider_spec(provider)
    if spec is not None and spec.speaks_nexsms:
        return str(getattr(activation, "phone", "") or "").strip()
    return str(getattr(activation, "activation_id", "") or "").strip()


def _smsbower_client(slot: PhoneSlot) -> SmsBowerClient:
    return SmsBowerClient(api_key=slot.api_key, endpoint=slot.endpoint)


def _nexsms_client(slot: PhoneSlot) -> NexSmsClient:
    return NexSmsClient(api_key=slot.api_key, endpoint=slot.endpoint)


def _rental_client(slot: PhoneSlot):
    """Protocol-appropriate client for ``slot`` -- see :func:`rental_protocol`.

    Goes through the slot-shaped ``_smsbower_client``/``_nexsms_client`` seams
    rather than constructing the class directly, because those are the patch
    targets the pool tests substitute. Same rule, same seam.
    """
    if rental_protocol(provider_name(slot)) == sms_providers.PROTOCOL_NEXSMS:
        return _nexsms_client(slot)
    return _smsbower_client(slot)


def _price_matches(left, right) -> bool:
    try:
        return Decimal(str(left or "").strip()) == Decimal(str(right or "").strip())
    except (InvalidOperation, ValueError):
        return False


def _refresh_smsbower_provider_ids(client: SmsBowerClient, slot: PhoneSlot, country: str) -> None:
    min_price = str(slot.min_price or "").strip()
    max_price = str(slot.max_price or "").strip()
    selected_price = max_price if min_price and max_price and _price_matches(min_price, max_price) else str(
        slot.target_price or max_price or min_price or ""
    ).strip()
    if not selected_price:
        return
    offers = client.get_prices(service=slot.service, country=country)
    provider_ids = []
    for offer in offers:
        if normalize_country(offer.get("country_id")) != normalize_country(country):
            continue
        if normalize_service(offer.get("service")) != normalize_service(slot.service):
            continue
        if not _price_matches(offer.get("price"), selected_price):
            continue
        if _int_value(offer.get("count"), 0) <= 0:
            continue
        provider_id = str(offer.get("provider_id") or "").strip()
        if provider_id and provider_id not in provider_ids:
            provider_ids.append(provider_id)
    if provider_ids:
        slot.provider_ids = ",".join(provider_ids)


def _acquire_smsbower_number(slot: PhoneSlot) -> bool:
    client = _smsbower_client(slot)
    countries = _country_candidates(slot.country)
    for country in countries:
        for attempt in range(1, RENTAL_NO_NUMBERS_MAX_ATTEMPTS + 1):
            try:
                _refresh_smsbower_provider_ids(client, slot, country)
            except Exception as exc:
                print(f"  [{slot.provider}] country={country} provider refresh failed: {exc}")
            try:
                activation = client.get_number(
                    service=slot.service,
                    country=country,
                    min_price=slot.min_price,
                    max_price=slot.max_price,
                    provider_ids=slot.provider_ids,
                )
            except Exception as exc:
                error = str(exc)
                print(f"  [{slot.provider}] country={country} acquire failed ({attempt}/{RENTAL_NO_NUMBERS_MAX_ATTEMPTS}): {error}")
                if "NO_BALANCE" in error or "BAD_KEY" in error:
                    return False
                if "NO_NUMBERS" not in error:
                    break
                if slot.activation_id:
                    client.cancel(slot.activation_id)
                    _reset_smsbower_slot(slot)
                if attempt < RENTAL_NO_NUMBERS_MAX_ATTEMPTS:
                    time.sleep(1)
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
                f"  [{slot.provider}] acquired "
                f"{slot.phone} (id={slot.activation_id}, country={country}, price={activation.price})"
            )
            return True
    return False


def _acquire_nexsms_number(slot: PhoneSlot) -> bool:
    """Acquire through the JSON REST family: no activation id, no price params.

    The retry budget matches the sms-activate path, but the classification does
    not: this vendor answers ``{code, message}`` rather than ``NO_NUMBERS``-style
    tokens, so "stop now" comes from the client's ``retryable`` flag instead of
    substring matching. A vendor whose failures cannot be classified would burn
    the whole budget on a rejected key.
    """
    client = _nexsms_client(slot)
    countries = _country_candidates(slot.country)
    for country in countries:
        for attempt in range(1, RENTAL_NO_NUMBERS_MAX_ATTEMPTS + 1):
            try:
                activation = client.get_number(
                    service=slot.service,
                    country=country,
                    min_price=slot.min_price,
                    max_price=slot.max_price,
                )
            except Exception as exc:
                emit(
                    _LOGGER,
                    "  [%s] country=%s acquire failed (%s/%s): %s",
                    slot.provider, country, attempt, RENTAL_NO_NUMBERS_MAX_ATTEMPTS, exc,
                )
                if not getattr(exc, "retryable", True):
                    return False
                if attempt < RENTAL_NO_NUMBERS_MAX_ATTEMPTS:
                    time.sleep(1)
                continue
            previous_phone = slot.phone
            slot.phone = normalize_phone(activation.phone)
            slot.activation_id = activation.activation_id
            slot.service = activation.service
            slot.country = activation.country
            if not previous_phone or previous_phone != slot.phone:
                slot.reuse_count = 0
                slot.last_sms_code = ""
            emit(
                _LOGGER,
                "  [%s] acquired %s (country=%s, price=%s)",
                slot.provider, slot.phone, country, activation.price,
            )
            return True
    return False


def _acquire_rental_number(slot: PhoneSlot) -> bool:
    """Acquire a number through the slot's own protocol family."""
    spec = sms_providers.provider_spec(getattr(slot, "provider", ""))
    if spec is not None and spec.speaks_nexsms:
        return _acquire_nexsms_number(slot)
    return _acquire_smsbower_number(slot)


def _prepare_smsbower_for_send(slot: PhoneSlot) -> bool:
    if not slot.rental_handle or not slot.phone:
        return _acquire_rental_number(slot)
    if slot.reuse_count <= 0:
        return True
    if _rental_client(slot).request_additional(slot.rental_handle):
        emit(
            _LOGGER,
            "  [%s] rental %s ready for another code",
            slot.provider, slot.rental_handle,
        )
        return True
    emit(
        _LOGGER,
        "  [%s] rental %s could not request another code; cancelling and acquiring a new number",
        slot.provider, slot.rental_handle,
    )
    _cancel_smsbower_activation(slot)
    return _acquire_rental_number(slot)


def _prepare_provider_for_send(slot: PhoneSlot) -> bool:
    return _sms_provider_adapter(slot).prepare()


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


def _wait_smsbower_code(slot: PhoneSlot) -> Optional[str]:
    return _rental_client(slot).wait_for_code(
        slot.rental_handle,
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
    handle = slot.rental_handle
    if handle:
        client = _rental_client(slot)
        if client.complete(handle):
            emit(_LOGGER, "  [%s] rental %s completed", slot.provider, handle)
        else:
            client.cancel(handle)
            emit(
                _LOGGER,
                "  [%s] rental %s completion failed; cancelled",
                slot.provider, handle,
            )
    _reset_smsbower_slot(slot)


def _cancel_smsbower_activation(slot: PhoneSlot):
    handle = slot.rental_handle
    if handle:
        _rental_client(slot).cancel(handle)
    _reset_smsbower_slot(slot)


class _RentalSmsProviderAdapter(SmsProviderAdapter):
    """Adapter for any provider whose rental lifecycle this repo can drive.

    Named for the lifecycle rather than the vendor. Both protocol families rent
    a number, wait for a code, then complete or cancel it -- they differ in how
    the rental is *addressed* (activation id vs phone number) and in the wire
    format, both of which live behind ``_rental_client`` and
    ``PhoneSlot.rental_handle``. The adapter itself is protocol-agnostic.
    """

    provider_key = sms_providers.DEFAULT_PROVIDER

    def prepare(self) -> bool:
        return _prepare_smsbower_for_send(self.slot)

    def wait_code(self) -> Optional[str]:
        return _wait_smsbower_code(self.slot)

    def complete(self) -> None:
        _complete_smsbower_activation(self.slot)

    def cancel(self) -> None:
        _cancel_smsbower_activation(self.slot)


def _sms_provider_adapter(slot: PhoneSlot) -> SmsProviderAdapter:
    """Rental adapter for ``slot``.

    An unrecognised provider name raises rather than falling back. The removed
    static adapter's ``complete``/``cancel`` were no-ops, so silently routing a
    rental to it left the activation open and billing while the registration had
    already moved on -- a wrong provider name is a bug, and the loudest
    available failure is the correct one.
    """
    name = provider_name(slot)
    spec = sms_providers.provider_spec(name)
    if spec is None or not spec.is_rental:
        supported = ", ".join(sms_providers.available_provider_keys())
        raise ValueError(f"unsupported SMS provider {name!r}; expected one of: {supported}")
    return _RentalSmsProviderAdapter(slot)


def _complete_provider_activation(slot: PhoneSlot):
    _sms_provider_adapter(slot).complete()


def _cancel_provider_activation(slot: PhoneSlot):
    _sms_provider_adapter(slot).cancel()


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


def _is_rental_slot(slot: PhoneSlot) -> bool:
    """True when ``slot`` rents its number through a protocol we can drive."""
    spec = sms_providers.provider_spec(getattr(slot, "provider", ""))
    return bool(spec and spec.is_rental)


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

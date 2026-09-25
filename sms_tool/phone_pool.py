"""Phone-reuse pool state and persistence.

Split out of :mod:`sms_tool.phone_reuse`: :class:`PhoneSlot` (one rentable
number), :class:`PhonePool` (the rotation cursor over them) and the atomic,
cross-process-safe state file. The vendor calls live in
:mod:`sms_tool.phone_provider_lifecycle`; the OpenAI OTP exchange and retry
orchestration stay in :mod:`sms_tool.phone_reuse`.

``rental_handle_for`` lives here because it is a property of the *slot* (how a
vendor addresses its rental) rather than of any one vendor call, and because
both the pool and the standalone phone-registration flow read it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import sms_providers
from .cross_process_gate import cross_process_write_lock
from .operator_output import emit
from .phone_provider_lifecycle import _complete_provider_activation, _reset_provider_slot
from .smsbower import DEFAULT_ENDPOINT, GHANA_COUNTRY_CODE, OPENAI_SERVICE_CODE, normalize_country, normalize_service

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


def rental_handle_for(provider: str, activation) -> str:
    """The key ``provider`` addresses this activation by.

    sms-activate vendors issue an activation id; NexSMS issues none and is
    addressed by the phone number itself. :class:`PhoneSlot` and the standalone
    phone-registration flow both go through here, so the rule cannot drift
    between the two entry points. An activation that has not rented yet reports
    ``""`` either way.

    Deliberately lenient where :func:`phone_provider_lifecycle.rental_protocol`
    raises: this is a *read*, and an unrecognised provider still has an
    ``activation_id`` worth returning.
    """
    spec = sms_providers.provider_spec(provider)
    if spec is not None and spec.speaks_nexsms:
        return str(getattr(activation, "phone", "") or "").strip()
    return str(getattr(activation, "activation_id", "") or "").strip()


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
            emit(_LOGGER, "[!] Failed to load phone pool state: %s", exc)
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
                emit(
                    _LOGGER,
                    "  [%s] rental %s reached reuse limit; completing before next purchase",
                    phone.provider,
                    handle,
                )
                _complete_provider_activation(phone)
            else:
                _reset_provider_slot(phone)
            emit(
                _LOGGER,
                "  [%s] reset exhausted phone %s; next send will acquire a new number",
                phone.provider,
                old_phone or "(pending)",
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


__all__ = ["PhonePool", "PhoneSlot", "rental_handle_for"]

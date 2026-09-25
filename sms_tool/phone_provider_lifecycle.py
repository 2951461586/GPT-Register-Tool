"""Rental lifecycle for phone-reuse providers (acquire / prepare / complete / cancel).

Split out of :mod:`sms_tool.phone_reuse`. Everything here speaks to a vendor:
choosing the protocol-appropriate client, buying a number, preparing a rental
for another code, and completing or cancelling it. The pool state lives in
:mod:`sms_tool.phone_pool`; the OpenAI OTP exchange and the retry orchestration
stay in :mod:`sms_tool.phone_reuse`.

Two protocol families share this surface. ``smsbower``/``herosms``/``grizzly``
speak the sms-activate handler family and address a rental by activation id;
``nexsms`` speaks a JSON REST family and addresses it by phone number. The
family is decided once by :func:`rental_protocol` and once by
:func:`sms_tool.phone_pool.rental_handle_for`; no call site re-derives it.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Optional

from . import sms_providers
from .nexsms import NexSmsClient
from .operator_output import emit
from .phone_config import _int_value
from .sms_provider_adapter import SmsProviderAdapter, provider_name
from .smsbower import (
    SmsBowerClient,
    normalize_country,
    normalize_phone,
    normalize_service,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from .phone_pool import PhoneSlot

#: Retry budgets shared by both protocol families. Renamed off the vendor name
#: when the second family landed: they are per-rental, not per-vendor, and a
#: vendor-named constant applied to another vendor is how "this only ever
#: matched one provider" bugs start.
RENTAL_NO_NUMBERS_MAX_ATTEMPTS = 10

_LOGGER = logging.getLogger(__name__)


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
                emit(
                    _LOGGER,
                    "  [%s] country=%s provider refresh failed: %s",
                    slot.provider,
                    country,
                    exc,
                )
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
                emit(
                    _LOGGER,
                    "  [%s] country=%s acquire failed (%s/%s): %s",
                    slot.provider,
                    country,
                    attempt,
                    RENTAL_NO_NUMBERS_MAX_ATTEMPTS,
                    error,
                )
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
            emit(
                _LOGGER,
                "  [%s] acquired %s (id=%s, country=%s, price=%s)",
                slot.provider,
                slot.phone,
                slot.activation_id,
                country,
                activation.price,
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


def _is_rental_slot(slot: PhoneSlot) -> bool:
    """True when ``slot`` rents its number through a protocol we can drive."""
    spec = sms_providers.provider_spec(getattr(slot, "provider", ""))
    return bool(spec and spec.is_rental)


__all__ = [
    "RENTAL_NO_NUMBERS_MAX_ATTEMPTS",
    "rental_client",
    "rental_protocol",
]

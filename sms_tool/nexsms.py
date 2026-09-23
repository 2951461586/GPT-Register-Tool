"""NexSMS REST client -- the ``nexsms_json`` protocol family.

A different family from :mod:`sms_tool.smsbower`, not a re-hosted copy of it.
Three differences drive every call site:

1. **Transport.** Plain REST under ``/api/`` with ``apiKey`` in the *query
   string* for both GET and POST; a POST carries its business parameters as a
   JSON body.
2. **Envelope.** Every reply is ``{code, message, data}`` and ``code == 0`` is
   the only success. There is no ``STATUS_OK:`` / ``ACCESS_NUMBER`` text
   protocol, so none of ``smsbower``'s parsing has an analogue here.
3. **Lifecycle key.** There is **no activation id**. ``POST /api/order/purchase``
   returns a phone number, and every later call -- poll, cancel -- is addressed
   by that number. There is likewise no ``setStatus``, so "ready for another
   code" and "activation complete" have no wire representation at all.

What that forces on callers:

* :attr:`NexSmsActivation.activation_id` is always ``""``. Persisting or logging
  an activation id is meaningless for this vendor -- use
  ``phone_reuse.PhoneSlot.rental_handle``, which resolves to the right key per
  protocol.
* :meth:`NexSmsClient.complete` reports success without contacting the vendor.
  The rental settles when the number expires; reporting failure would make the
  caller cancel a number that had already been paid for and successfully used.
* Cancelling refunds only once the number is two minutes old. The vendor refuses
  earlier attempts, and that refusal is logged with its raw payload rather than
  swallowed -- the same lesson ``smsbower.cancel`` records.

Two slot settings have no request-parameter equivalent on this vendor and are
therefore enforced **client-side** instead of being dropped: ``max_price`` and
``min_price`` become a price window checked against the cheapest quote. Ignoring
them would mean a configured ceiling silently stops applying, which is how an
operator ends up paying more than they allowed.

Scope note: no credentials live here. ``DEFAULT_ENDPOINT`` is the vendor's
public API host; the operator supplies their own key through ``NEXSMS_API_KEY``
or ``phone_reuse.nexsms.api_key``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests as _requests

from .operator_output import emit
from .sms_providers import PROVIDERS as _PROVIDERS
# Service codes and country ids follow the sms-activate numbering, so the
# normalisers are shared with that family rather than duplicated. The module
# they live in carries a vendor name, but the helpers themselves are
# protocol-level: ``dr`` and ``38`` mean the same thing to both vendors.
from .smsbower import (
    GHANA_COUNTRY_CODE,
    OPENAI_SERVICE_CODE,
    normalize_country,
    normalize_phone,
    normalize_service,
)

_LOGGER = logging.getLogger(__name__)

#: Base host only -- this family appends its own ``/api/...`` path per call, so
#: unlike the sms-activate vendors the endpoint must NOT carry a handler path.
DEFAULT_ENDPOINT = _PROVIDERS["nexsms"].default_endpoint

PATH_BALANCE = "/api/balance"
PATH_COUNTRIES = "/api/countries"
PATH_SERVICES = "/api/services"
PATH_COUNTRY_BY_SERVICE = "/api/getCountryByService"
PATH_PURCHASE = "/api/order/purchase"
PATH_MESSAGES = "/api/sms/messages"
PATH_CLOSE_ACTIVATION = "/api/close/activation"

#: Reply format that yields the newest single message rather than a list.
MESSAGES_FORMAT_LATEST = "json_latest"

#: Status vocabulary, deliberately the same two words ``smsbower`` uses for the
#: states this vendor can actually reach, so one polling loop serves both.
#: ``CANCEL`` and ``WAIT_RETRY`` are unreachable here: the first has no
#: representation and the second needs ``setStatus``.
STATUS_OK = "OK"
STATUS_WAIT_CODE = "WAIT_CODE"

#: Substrings that mean "retrying cannot help". The vendor's numeric codes are
#: not publicly enumerated, so this is a heuristic over the human-readable
#: ``message`` -- the same shape the reference implementation uses. It only ever
#: decides whether to stop retrying early; a miss costs attempts, not money.
_FATAL_MESSAGE_MARKERS = (
    "api key",
    "apikey",
    "api_key",
    "密钥",
    "令牌",
    "余额不足",
    "insufficient",
    "balance",
)

#: HTTP statuses that will not improve by asking again.
_FATAL_HTTP_STATUSES = (401, 403)


class NexSmsError(RuntimeError):
    """Vendor or transport failure.

    ``retryable`` is the only classification callers need: ``False`` means a
    rejected key, an empty balance, or a malformed request, so the acquisition
    loop should stop rather than spend its remaining attempts.
    """

    def __init__(self, message: str, *, code: object = None, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)


@dataclass
class NexSmsActivation:
    """A rented number.

    Shape-compatible with :class:`sms_tool.smsbower.SmsBowerActivation` so the
    acquisition path can handle both, with one deliberate difference:
    ``activation_id`` is always empty because this vendor does not issue one.
    """

    phone: str
    service: str
    country: str
    price: str = ""
    activation_id: str = ""
    acquired_at: float = field(default_factory=time.time)


def api_phone_number(phone: object) -> str:
    """``phone`` in the form this API wants: digits only, no ``+``.

    The vendor returns numbers without the prefix and expects them back that
    way, while the rest of the tool stores them ``+``-prefixed (the browser flow
    needs the prefixed form), so the conversion belongs here.
    """
    value = str(phone or "").strip()
    if value.startswith("+"):
        value = value[1:]
    if value.startswith("00"):
        value = value[2:]
    return "".join(ch for ch in value if ch.isdigit())


def _as_float(value) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _format_price(value) -> str:
    number = _as_float(value)
    if number is None:
        return ""
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _looks_fatal(message: str) -> bool:
    text = str(message or "").lower()
    return any(marker in text for marker in _FATAL_MESSAGE_MARKERS)


@dataclass
class NexSmsClient:
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    timeout: int = 30

    # -- transport ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.endpoint.rstrip('/')}{path}"

    def _query(self, params: dict | None = None) -> dict:
        query = {"apiKey": self.api_key}
        if params:
            query.update(params)
        return query

    def _get(self, path: str, params: dict | None = None) -> object:
        return self._send("get", self._url(path), params=self._query(params))

    def _post(self, path: str, body: dict | None = None) -> object:
        return self._send(
            "post",
            self._url(path),
            params=self._query(),
            json=body if body is not None else {},
        )

    def _send(self, method: str, url: str, **kwargs) -> object:
        """One HTTP call, with every failure normalised to :class:`NexSmsError`.

        Wrapping is what lets the acquisition loop classify a reply without
        importing ``requests``: an unhandled ``HTTPError`` would arrive as a bare
        exception and be retried like a transient network blip even when it was a
        rejected key.
        """
        try:
            response = getattr(_requests, method)(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            retryable = status not in _FATAL_HTTP_STATUSES
            raise NexSmsError(
                f"nexsms transport failed ({status or type(error).__name__}): {error}",
                code=status,
                retryable=retryable,
            ) from error

    def _unwrap(self, payload: object, fallback: str) -> object:
        """Return ``data`` from the ``{code, message, data}`` envelope.

        ``code`` is compared numerically so a vendor that sends ``"0"`` instead
        of ``0`` is not mistaken for a failure.
        """
        if not isinstance(payload, dict):
            raise NexSmsError(f"{fallback}: unexpected reply {str(payload)[:160]!r}")
        code = payload.get("code")
        try:
            ok = int(code) == 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            message = str(payload.get("message") or "").strip() or fallback
            raise NexSmsError(
                f"nexsms error (code={code}): {message}",
                code=code,
                retryable=not _looks_fatal(message),
            )
        return payload.get("data")

    # -- read-only endpoints ----------------------------------------------

    def get_balance(self) -> dict:
        data = self._unwrap(self._get(PATH_BALANCE), "balance lookup failed")
        data = data if isinstance(data, dict) else {}
        return {
            "user_id": data.get("userId"),
            "username": str(data.get("username") or ""),
            "balance": _as_float(data.get("balance")),
        }

    def get_countries(self) -> list[dict]:
        data = self._unwrap(self._get(PATH_COUNTRIES), "country lookup failed")
        rows: list[dict] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("id") or "").strip()
            if not country_id:
                continue
            name = str(item.get("name") or "").strip()
            rows.append({"id": country_id, "name": name})
        return rows

    def get_services(self) -> list[dict]:
        data = self._unwrap(self._get(PATH_SERVICES), "service lookup failed")
        rows: list[dict] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            rows.append({"code": code, "name": str(item.get("name") or "").strip()})
        return rows

    def get_country_quote(self, service: str = "", country: str = "") -> Optional[dict]:
        """Cheapest offer for one country, or ``None`` when there is none.

        The vendor answers an object when ``countryId`` is supplied and an array
        of every country when it is not; both shapes are accepted here so a
        caller cannot be broken by which form it happens to get.
        """
        params = {"serviceCode": normalize_service(service)}
        country_code = normalize_country(country)
        if country_code:
            params["countryId"] = _as_country_id(country_code)
        data = self._unwrap(
            self._get(PATH_COUNTRY_BY_SERVICE, params), "quote lookup failed"
        )
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict):
            return None
        return {
            "country_id": str(item.get("countryId") or country_code).strip(),
            "country_name": str(item.get("countryName") or "").strip(),
            "phone_code": str(item.get("phoneCode") or "").lstrip("+").strip(),
            "min_price": _as_float(item.get("minPrice")),
            "median_price": _as_float(item.get("medianPrice")),
            "max_price": _as_float(item.get("maxPrice")),
            "price_map": item.get("priceMap") if isinstance(item.get("priceMap"), dict) else {},
        }

    # -- lifecycle ---------------------------------------------------------

    def get_number(
        self,
        service: str = OPENAI_SERVICE_CODE,
        country: str = GHANA_COUNTRY_CODE,
        max_price: str = "",
        min_price: str = "",
        provider_ids: str = "",
    ) -> NexSmsActivation:
        """Buy exactly one number at the cheapest listed price.

        One attempt only. The retry loop lives in the caller, which is where it
        already lives for the sms-activate vendors; duplicating it here would
        give the two families different retry budgets for no reason.

        ``provider_ids`` has no meaning for this vendor (there is no operator
        dimension) and is reported rather than silently dropped. ``min_price``
        and ``max_price`` are enforced as a window over the quote, because this
        API takes no price parameter at all -- the operator's ceiling would
        otherwise stop applying the moment they switched vendor.
        """
        service_code = normalize_service(service)
        country_code = normalize_country(country)
        if str(provider_ids or "").strip():
            emit(
                _LOGGER,
                "  [nexsms] provider_ids=%s ignored: this vendor has no operator dimension",
                str(provider_ids).strip(),
            )
        quote = self.get_country_quote(service_code, country_code)
        if not quote or quote.get("min_price") is None:
            raise NexSmsError(
                f"nexsms returned no quote for country={country_code} service={service_code}"
            )
        price = quote["min_price"]
        refusal = _price_window_refusal(price, min_price, max_price)
        if refusal:
            raise NexSmsError(
                f"nexsms cheapest number for country={country_code} service={service_code} "
                f"costs {_format_price(price)}: {refusal}"
            )
        order = self._unwrap(
            self._post(
                PATH_PURCHASE,
                {
                    "serviceCode": service_code,
                    "countryId": _as_country_id(country_code),
                    "quantity": 1,
                    "price": price,
                },
            ),
            "purchase failed",
        )
        order = order if isinstance(order, dict) else {}
        phones = order.get("phoneNumbers")
        phone = ""
        if isinstance(phones, list) and phones:
            phone = str(phones[0] or "").strip()
        if not phone:
            raise NexSmsError("nexsms purchase succeeded but returned no phone number")
        return NexSmsActivation(
            phone=normalize_phone(phone),
            service=service_code,
            country=country_code,
            price=_format_price(order.get("totalAmount") or price),
        )

    def get_status(self, handle: str) -> dict:
        """Newest message for ``handle``, in ``smsbower``'s status vocabulary."""
        number = api_phone_number(handle)
        if not number:
            return {"status": STATUS_WAIT_CODE}
        data = self._unwrap(
            self._get(
                PATH_MESSAGES,
                {"phoneNumber": number, "format": MESSAGES_FORMAT_LATEST},
            ),
            "sms lookup failed",
        )
        if not isinstance(data, dict):
            return {"status": STATUS_WAIT_CODE}
        code = str(data.get("code") or "").strip()
        if not code:
            return {"status": STATUS_WAIT_CODE}
        return {
            "status": STATUS_OK,
            "code": code,
            "text": str(data.get("text") or ""),
            "expires_time": str(data.get("expiresTime") or ""),
        }

    def wait_for_code(
        self,
        handle: str,
        timeout: int = 120,
        poll_interval: int = 5,
        previous_code: str = "",
    ) -> Optional[str]:
        """Poll until a code newer than ``previous_code`` arrives, else ``None``.

        Cancelling on timeout is deliberately *not* done here: the caller owns
        that decision because it also owns the retry budget, and it is the same
        division of labour the sms-activate path uses.
        """
        deadline = time.time() + timeout
        attempt = 0
        previous_code = str(previous_code or "").strip()
        while time.time() < deadline:
            attempt += 1
            try:
                status = self.get_status(handle)
                if status["status"] == STATUS_OK:
                    code = str(status.get("code") or "").strip()
                    if code and code != previous_code:
                        return code
            except Exception as exc:
                _LOGGER.warning(
                    "nexsms poll attempt %s failed for %s: %s",
                    attempt, handle, exc,
                    extra={"event": "nexsms_poll_failed", "attempt": attempt},
                )
            wait = min(poll_interval, max(1, deadline - time.time()))
            if wait > 0:
                time.sleep(wait)
        return None

    def complete(self, handle: str) -> bool:
        """No-op success: this vendor settles the rental when the number expires.

        ``True`` is deliberate rather than incidental. ``False`` means
        "completion failed" to every caller, and the caller's reaction to that is
        to cancel -- which here would discard a number that had already been paid
        for and had already delivered its code.
        """
        return True

    def cancel(self, handle: str) -> bool:
        """Release ``handle`` and request a refund.

        The vendor refuses a number younger than two minutes, so a refusal is
        normal rather than exceptional. It is logged with the raw reply instead
        of being flattened to ``False``: "refused because too new" and "the
        network died" are otherwise indistinguishable in the logs, which is
        exactly the ambiguity ``smsbower.cancel`` documents.
        """
        number = api_phone_number(handle)
        if not number:
            return False
        try:
            refunded = self._unwrap(
                self._post(PATH_CLOSE_ACTIVATION, {"phoneNumber": number}),
                "cancel failed",
            )
        except Exception as error:
            _LOGGER.warning(
                "nexsms refused to cancel %s: %s",
                number, error,
                extra={"event": "nexsms_cancel_refused", "phone": number},
            )
            return False
        emit(
            _LOGGER,
            "  [nexsms] cancelled %s (refund=%s)",
            number,
            refunded,
            extra={"event": "nexsms_cancel_ok", "phone": number, "refund": refunded},
        )
        return True

    def request_additional(self, handle: str) -> bool:
        """Always ``True``: there is no "prepare for another code" call.

        The number stays live until it expires, so a second send needs no vendor
        round-trip. Reporting ``False`` would make the caller cancel a working
        number and buy another one.
        """
        return bool(api_phone_number(handle))


def _as_country_id(country_code: str):
    """Send a numeric country id as an int; anything else verbatim.

    The vendor's own examples pass a number, but the id ultimately comes from
    operator config, so a non-numeric value is forwarded unchanged rather than
    crashing -- the vendor then answers a normal error instead of the tool
    raising a ``ValueError`` mid-run.
    """
    text = str(country_code or "").strip()
    return int(text) if text.isdigit() else text


def _price_window_refusal(price: float, min_price: str, max_price: str) -> str:
    """``""`` when ``price`` is inside the configured window, else the reason.

    An empty bound is not a bound: both default to unset except ``max_price``,
    which ``phone_reuse`` defaults to ``0.06``.
    """
    floor = _as_float(min_price)
    if floor is not None and price < floor:
        return f"below the configured min_price {_format_price(floor)}"
    ceiling = _as_float(max_price)
    if ceiling is not None and price > ceiling:
        return f"above the configured max_price {_format_price(ceiling)}"
    return ""

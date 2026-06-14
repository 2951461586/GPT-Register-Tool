"""SMSBower handler API client.

The API is compatible with the common sms-activate handler protocol:
acquire a number, poll its activation status, then complete/cancel it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import requests as _requests


DEFAULT_ENDPOINT = "https://smsbower.page/stubs/handler_api.php"
OPENAI_SERVICE_CODE = "dr"
GHANA_COUNTRY_CODE = "38"

SERVICE_ALIASES = {
    "openai": OPENAI_SERVICE_CODE,
    "chatgpt": OPENAI_SERVICE_CODE,
    "openai(chatgpt)": OPENAI_SERVICE_CODE,
    "openai (chatgpt)": OPENAI_SERVICE_CODE,
}

COUNTRY_ALIASES = {
    "ghana": GHANA_COUNTRY_CODE,
    "gh": GHANA_COUNTRY_CODE,
    "+233": GHANA_COUNTRY_CODE,
    "233": GHANA_COUNTRY_CODE,
}


def normalize_service(service: str) -> str:
    value = str(service or OPENAI_SERVICE_CODE).strip()
    if not value:
        return OPENAI_SERVICE_CODE
    return SERVICE_ALIASES.get(value.lower(), value)


def normalize_country(country: str) -> str:
    value = str(country or GHANA_COUNTRY_CODE).strip()
    if not value:
        return GHANA_COUNTRY_CODE
    return COUNTRY_ALIASES.get(value.lower(), value)


def normalize_phone(phone: str) -> str:
    value = str(phone or "").strip()
    if not value:
        return ""
    if value.startswith("+"):
        return "+" + "".join(ch for ch in value[1:] if ch.isdigit())
    if value.startswith("00"):
        return "+" + "".join(ch for ch in value[2:] if ch.isdigit())
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"+{digits}" if digits else ""


def _as_price(value) -> float:
    try:
        return float(str(value or "").strip())
    except Exception:
        return 0.0


@dataclass
class SmsBowerActivation:
    activation_id: str
    phone: str
    service: str
    country: str
    price: str = ""
    acquired_at: float = field(default_factory=time.time)


@dataclass
class SmsBowerClient:
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    timeout: int = 15

    def _do(self, action: str, params: dict | None = None) -> str:
        query = {"api_key": self.api_key, "action": action}
        if params:
            query.update(params)
        response = _requests.get(self.endpoint, params=query, timeout=self.timeout)
        response.raise_for_status()
        return response.text.strip()

    def get_number(
        self,
        service: str = OPENAI_SERVICE_CODE,
        country: str = GHANA_COUNTRY_CODE,
        max_price: str = "",
        min_price: str = "",
    ) -> SmsBowerActivation:
        service_code = normalize_service(service)
        country_code = normalize_country(country)
        params = {"service": service_code, "country": country_code}
        if max_price:
            params["maxPrice"] = str(max_price)
        if min_price:
            params["minPrice"] = str(min_price)

        result = self._do("getNumberV2", params)
        if result.startswith("{"):
            return self._parse_get_number_v2(result, service_code, country_code)
        return self._parse_access_number(result, service_code, country_code)

    def _parse_get_number_v2(self, result: str, service: str, country: str) -> SmsBowerActivation:
        data = json.loads(result)
        activation_id = str(
            data.get("activationId")
            or data.get("activation_id")
            or data.get("id")
            or ""
        ).strip()
        phone = normalize_phone(
            data.get("phoneNumber")
            or data.get("phone")
            or data.get("number")
            or ""
        )
        price = str(data.get("activationCost") or data.get("price") or "")
        if not activation_id or not phone:
            error = data.get("error") or data.get("message") or result[:200]
            raise RuntimeError(f"getNumberV2 error: {error}")
        return SmsBowerActivation(
            activation_id=activation_id,
            phone=phone,
            service=service,
            country=country,
            price=price,
        )

    def _parse_access_number(self, result: str, service: str, country: str) -> SmsBowerActivation:
        parts = result.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise RuntimeError(f"getNumber error: {result}")
        return SmsBowerActivation(
            activation_id=parts[1],
            phone=normalize_phone(parts[2]),
            service=service,
            country=country,
        )

    def get_status(self, activation_id: str) -> dict:
        result = self._do("getStatus", {"id": activation_id})
        return self._parse_status(result)

    def _parse_status(self, result: str) -> dict:
        if result.startswith("STATUS_OK:"):
            code = result[len("STATUS_OK:"):].strip().strip("'\"")
            return {"status": "OK", "code": code}
        if result == "STATUS_WAIT_CODE":
            return {"status": "WAIT_CODE"}
        if result.startswith("STATUS_WAIT_RETRY:"):
            code = result[len("STATUS_WAIT_RETRY:"):].strip().strip("'\"")
            return {"status": "WAIT_RETRY", "code": code}
        if result == "STATUS_CANCEL":
            return {"status": "CANCEL"}
        raise RuntimeError(f"getStatus error: {result}")

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = 120,
        poll_interval: int = 5,
        previous_code: str = "",
        accept_wait_retry: bool = False,
    ) -> Optional[str]:
        deadline = time.time() + timeout
        attempt = 0
        previous_code = str(previous_code or "").strip()
        while time.time() < deadline:
            attempt += 1
            try:
                status = self.get_status(activation_id)
                if status["status"] == "OK":
                    code = str(status.get("code") or "").strip()
                    if code and code != previous_code:
                        return code
                if status["status"] == "WAIT_RETRY":
                    code = str(status.get("code") or "").strip()
                    if accept_wait_retry and code and code != previous_code:
                        return code
                if status["status"] == "CANCEL":
                    print(f"  [smsbower] activation {activation_id} was cancelled")
                    return None
            except Exception as exc:
                print(f"  [smsbower] poll attempt {attempt} error: {exc}")
            wait = min(poll_interval, max(1, deadline - time.time()))
            if wait > 0:
                time.sleep(wait)
        return None

    def set_status(self, activation_id: str, status: str) -> str:
        return self._do("setStatus", {"id": activation_id, "status": str(status)})

    def complete(self, activation_id: str) -> bool:
        try:
            return self.set_status(activation_id, "6") == "ACCESS_ACTIVATION"
        except Exception:
            return False

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.set_status(activation_id, "8") == "ACCESS_CANCEL"
        except Exception:
            return False

    def request_additional(self, activation_id: str) -> bool:
        try:
            return self.set_status(activation_id, "3") in {"ACCESS_RETRY_GET", "ACCESS_READY"}
        except Exception:
            return False

    def get_balance(self) -> str:
        result = self._do("getBalance")
        prefix = "ACCESS_BALANCE:"
        if result.startswith(prefix):
            return result[len(prefix):]
        raise RuntimeError(f"getBalance error: {result}")

    def get_services(self) -> list[dict]:
        result = self._do("getServicesList")
        data = json.loads(result)
        services = data.get("services", data)
        if isinstance(services, list):
            return [
                {
                    "code": normalize_service(item.get("code") or item.get("activate_org_code") or item.get("slug") or ""),
                    "name": item.get("name") or item.get("title") or "",
                }
                for item in services
                if isinstance(item, dict)
            ]
        if isinstance(services, dict):
            return [
                {"code": str(code), "name": info.get("name", code) if isinstance(info, dict) else str(info)}
                for code, info in services.items()
            ]
        return []

    def get_countries(self) -> list[dict]:
        result = self._do("getCountries")
        data = json.loads(result)
        countries = []
        items = data.values() if isinstance(data, dict) else data
        for key, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            country_id = str(
                item.get("activate_org_code")
                or item.get("id")
                or item.get("country_id")
                or key
            )
            name = item.get("eng") or item.get("title") or item.get("name") or str(key)
            countries.append({"id": country_id, "name": name, "prefix": item.get("prefix", "")})
        return countries

    def get_prices(self, service: str = "", country: str = "") -> list[dict]:
        params = {}
        if service:
            params["service"] = normalize_service(service)
        if country:
            params["country"] = normalize_country(country)
        result = self._do("getPricesV3", params)
        data = json.loads(result)
        return _flatten_prices(data)


def _flatten_prices(data) -> list[dict]:
    offers = []
    if not isinstance(data, dict):
        return offers
    for country_id, by_service in data.items():
        if not isinstance(by_service, dict):
            continue
        for service, by_provider in by_service.items():
            if not isinstance(by_provider, dict):
                continue
            for provider_id, offer in by_provider.items():
                if not isinstance(offer, dict):
                    continue
                offers.append({
                    "country_id": str(country_id),
                    "service": str(service),
                    "provider_id": str(offer.get("provider_id") or provider_id),
                    "price": str(offer.get("price", "")),
                    "count": offer.get("count", 0),
                })
    return sorted(offers, key=lambda item: (_as_price(item.get("price")), str(item.get("provider_id"))))


def acquire_and_wait_code(
    api_key: str,
    service: str = OPENAI_SERVICE_CODE,
    country: str = GHANA_COUNTRY_CODE,
    max_price: str = "",
    timeout: int = 120,
    poll_interval: int = 5,
    endpoint: str = DEFAULT_ENDPOINT,
) -> dict:
    client = SmsBowerClient(api_key=api_key, endpoint=endpoint)
    try:
        activation = client.get_number(service=service, country=country, max_price=max_price)
        print(
            "  [smsbower] acquired "
            f"{activation.phone} (id={activation.activation_id}, price={activation.price})"
        )
    except Exception as exc:
        return {"ok": False, "error": f"acquire_failed:{exc}"}

    code = client.wait_for_code(activation.activation_id, timeout=timeout, poll_interval=poll_interval)
    if not code:
        client.cancel(activation.activation_id)
        return {
            "ok": False,
            "error": "sms_timeout",
            "activation_id": activation.activation_id,
            "phone": activation.phone,
        }
    return {
        "ok": True,
        "activation_id": activation.activation_id,
        "phone": activation.phone,
        "code": code,
        "price": activation.price,
    }

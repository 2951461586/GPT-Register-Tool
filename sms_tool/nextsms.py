"""NexSMS API client for phone verification."""

from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests as _requests

from .smsbower import SmsBowerActivation, normalize_phone


DEFAULT_ENDPOINT = "https://sms.nextactionplus.com/api/"
OPENAI_SERVICE_CODE = "openai"
DEFAULT_COUNTRY_CODE = "US"

SERVICE_ALIASES = {
    "dr": OPENAI_SERVICE_CODE,
    "openai": OPENAI_SERVICE_CODE,
    "chatgpt": OPENAI_SERVICE_CODE,
    "codex": OPENAI_SERVICE_CODE,
    "openai(chatgpt)": OPENAI_SERVICE_CODE,
    "openai (chatgpt)": OPENAI_SERVICE_CODE,
}


def normalize_service(service: str) -> str:
    value = str(service or OPENAI_SERVICE_CODE).strip()
    if not value:
        return OPENAI_SERVICE_CODE
    return SERVICE_ALIASES.get(value.lower(), value)


def normalize_country(country: str) -> str:
    value = str(country or DEFAULT_COUNTRY_CODE).strip()
    return value.upper() if value else DEFAULT_COUNTRY_CODE


def _extract_code(text: str) -> str:
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", str(text or ""))
    return match.group(1) if match else ""


class NexSmsClient:
    def __init__(self, api_key: str, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 20):
        self.api_key = str(api_key or "").strip()
        self.endpoint = str(endpoint or DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT
        self.timeout = timeout

    def _url(self, path: str) -> str:
        path = str(path or "").lstrip("/")
        base = self.endpoint.rstrip("/")
        parsed = urlparse(base)
        normalized_path = parsed.path.rstrip("/")
        if normalized_path.endswith("/api/v1"):
            return f"{base}/{path}"
        if normalized_path.endswith("/api"):
            return f"{base}/v1/{path}"
        return f"{base}/api/v1/{path}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs):
        response = _requests.request(
            method,
            self._url(path),
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        if 200 <= response.status_code < 300:
            if not response.text.strip():
                return {}
            try:
                return response.json()
            except Exception:
                return response.text.strip()
        message = response.text[:300]
        try:
            body = response.json()
            if isinstance(body, dict):
                message = str(body.get("error") or body.get("message") or message)
        except Exception:
            pass
        raise RuntimeError(f"{response.status_code}:{message}")

    def get_number(
        self,
        service: str = OPENAI_SERVICE_CODE,
        country: str = DEFAULT_COUNTRY_CODE,
        pricing_option: int | str = 0,
    ) -> SmsBowerActivation:
        service_code = normalize_service(service)
        country_code = normalize_country(country)
        data = self._request(
            "POST",
            "orders",
            json={
                "service": service_code,
                "country": country_code,
                "pricing_option": int(pricing_option or 0),
                "quantity": 1,
            },
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"orders error: {data}")
        orders = data.get("orders") if isinstance(data.get("orders"), list) else []
        if not orders and isinstance(data.get("order"), dict):
            orders = [data["order"]]
        if not orders:
            message = data.get("error") or data.get("message") or data.get("errors") or data
            raise RuntimeError(f"orders error: {message}")
        order = orders[0]
        if not isinstance(order, dict):
            raise RuntimeError(f"orders error: {order}")
        order_id = str(order.get("id") or "").strip()
        phone = normalize_phone(
            order.get("phone_number_full")
            or order.get("phone_number")
            or order.get("phone")
            or ""
        )
        if not order_id or not phone:
            raise RuntimeError(f"orders error: missing order id or phone: {order}")
        return SmsBowerActivation(
            activation_id=order_id,
            phone=phone,
            service=service_code,
            country=country_code,
            price=str(order.get("price") or order.get("price_cents") or ""),
        )

    def get_status(self, activation_id: str) -> dict:
        result = self._request("GET", f"sms-url/{activation_id}", params={"format": "json"})
        if isinstance(result, dict):
            status = str(result.get("status") or "").upper()
            received = bool(result.get("received")) or status == "YES"
            code = str(result.get("code") or "").strip()
            message = str(result.get("message") or result.get("sms") or "").strip()
            if received:
                return {"status": "OK", "code": code or _extract_code(message), "message": message}
            return {"status": "WAIT_CODE", "message": message}
        text = str(result or "").strip()
        if text.upper().startswith("YES|"):
            message = text.split("|", 1)[1].strip()
            return {"status": "OK", "code": _extract_code(message), "message": message}
        if text.upper().startswith("NO"):
            return {"status": "WAIT_CODE"}
        raise RuntimeError(f"sms-url error: {text[:200]}")

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = 120,
        poll_interval: int = 5,
        previous_code: str = "",
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
            except Exception as exc:
                print(f"  [nextsms] poll attempt {attempt} error: {exc}")
            wait = min(poll_interval, max(1, deadline - time.time()))
            if wait > 0:
                time.sleep(wait)
        return None

    def request_additional(self, activation_id: str) -> bool:
        try:
            self._request("POST", f"orders/{activation_id}/resend", json={})
            return True
        except Exception:
            return False

    def cancel(self, activation_id: str) -> bool:
        try:
            self._request("POST", f"orders/{activation_id}/cancel", json={})
            return True
        except Exception:
            return False

    def complete(self, activation_id: str) -> bool:
        return True

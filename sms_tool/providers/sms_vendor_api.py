"""SMS vendor catalog and balance over the wire, in one place.

Why this module exists (P7-B)
-----------------------------
The desktop used to read these two answers itself (`SmsProviderCatalogClient`, 703
lines of C# holding two protocol families). That put a third network egress in the
UI process: no proxy, no logging, and a second copy of a protocol ``sms_tool``
already speaks. The same two reads now live here, behind the resident desktop-read
channel, so the catalog has one implementation and the desktop has one egress.

Two families, and the family is decided by the provider table -- never guessed from
the endpoint string:

* **sms-activate** (``smsbower`` / ``herosms`` / ``grizzly``) -- one URL with
  ``?action=``, balance as a bare ``ACCESS_BALANCE:<amount>`` line;
* **nexsms** -- dated ``/api/...`` paths, the key in the query string, and a
  ``{code, message, data}`` envelope.

Every failure is reported, never raised: the caller is an interactive dialog, and a
vendor that is briefly down must degrade to "type the country yourself", not to an
exception in the UI.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Mapping

from . import ports

#: Which providers speak which family. Mirrors ``SmsProviderCatalog`` on the C#
#: side; ``tests/workbench/test_sms_vendor_api_contract.py`` pins the two tables
#: together so a new vendor cannot enter one side only.
SMS_ACTIVATE_PROVIDERS = frozenset({"smsbower", "herosms", "grizzly"})
NEXSMS_PROVIDERS = frozenset({"nexsms"})
OPENAI_SERVICE = "dr"


def family_of(provider: Any) -> str:
    """``"sms-activate"`` / ``"nexsms"``, or ``""`` for an unknown provider."""
    key = str(provider or "").strip().lower()
    if key in SMS_ACTIVATE_PROVIDERS:
        return "sms-activate"
    if key in NEXSMS_PROVIDERS:
        return "nexsms"
    return ""


def _session():
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session()
    try:
        session.trust_env = False
    except Exception:  # broad-except: compat - older curl_cffi builds lack it
        pass
    return session


def _get(session, url: str) -> str:
    response = ports.request(session, "get", url, timeout=20, label="SMS vendor API")
    return str(getattr(response, "text", "") or "")


def read_balance(provider: Any, api_key: str, endpoint: str) -> dict[str, Any]:
    """``{"ok": True, "balance": "12.34"}`` or ``{"ok": False, "error": ...}``."""
    key = str(api_key or "").strip()
    base = str(endpoint or "").rstrip("/")
    family = family_of(provider)
    if not family:
        return {"ok": False, "error": f"unknown_sms_provider:{provider}"}
    if not key or not base:
        return {"ok": False, "error": "missing_credentials"}

    session = _session()
    try:
        if family == "sms-activate":
            body = _get(session, f"{base}?action=getBalance&api_key={urllib.parse.quote(key)}")
            for line in body.splitlines():
                if line.startswith("ACCESS_BALANCE:"):
                    return {"ok": True, "balance": line.split(":", 1)[1].strip()}
            return {"ok": False, "error": _first_error(body) or "balance_missing"}
        body = _get(session, f"{base}/api/balance?apiKey={urllib.parse.quote(key)}")
        payload = _json(body)
        if isinstance(payload, Mapping) and payload.get("data") is not None:
            data = payload["data"]
            if isinstance(data, Mapping):
                for name in ("balance", "money", "amount"):
                    if data.get(name) is not None:
                        return {"ok": True, "balance": str(data[name])}
                return {"ok": False, "error": "balance_missing"}
            return {"ok": True, "balance": str(data)}
        return {
            "ok": False,
            "error": str(payload.get("message") or "balance_missing")
            if isinstance(payload, Mapping)
            else "balance_missing",
        }
    except Exception as exc:  # broad-except: compat - the dialog shows the reason
        return {"ok": False, "error": f"transport:{exc}"}
    finally:
        try:
            session.close()
        except Exception:  # broad-except: compat - cleanup best effort
            pass


def read_catalog(provider: Any, api_key: str, endpoint: str, service: str = "") -> dict[str, Any]:
    """``{"ok": True, "countries": [{"id","name","name_zh","tiers":[...]}]}``.

    ``name_zh`` is empty when the vendor does not serve one; the desktop falls back
    to ``name``, exactly as the previous C# reader did.
    """
    key = str(api_key or "").strip()
    base = str(endpoint or "").rstrip("/")
    family = family_of(provider)
    if not family:
        return {"ok": False, "error": f"unknown_sms_provider:{provider}"}
    if not key or not base:
        return {"ok": False, "error": "missing_credentials"}

    session = _session()
    try:
        if family == "sms-activate":
            countries = _sms_activate_countries(session, base, key)
            prices = _sms_activate_prices(session, base, key, str(service or OPENAI_SERVICE))
            for country in countries:
                country["tiers"] = prices.get(country["id"], [])
            # A country with no OpenAI tier is not rentable here; drop it so the
            # operator does not pick a country the vendor cannot serve.
            rentable = [country for country in countries if country["tiers"]]
            return {"ok": True, "countries": rentable}
        return _nexsms_catalog(session, base, key, str(service or OPENAI_SERVICE))
    except Exception as exc:  # broad-except: compat - the dialog shows the reason
        return {"ok": False, "error": f"transport:{exc}"}
    finally:
        try:
            session.close()
        except Exception:  # broad-except: compat - cleanup best effort
            pass


# --------------------------------------------------------------------------- #
# family helpers
# --------------------------------------------------------------------------- #


def _sms_activate_countries(session, base: str, key: str) -> list[dict[str, Any]]:
    body = _get(session, f"{base}?action=getCountries&api_key={urllib.parse.quote(key)}")
    payload = _json(body)
    if not isinstance(payload, Mapping):
        raise ValueError("getCountries did not return JSON")
    countries = []
    for country_id, names in payload.items():
        if str(country_id) == "0" or not isinstance(names, Mapping):
            continue
        countries.append(
            {
                "id": str(country_id),
                "name": str(names.get("eng") or names.get("rus") or country_id),
                "name_zh": str(names.get("chn") or ""),
                "tiers": [],
            }
        )
    return countries


def _sms_activate_prices(session, base: str, key: str, service: str) -> dict[str, list[dict[str, Any]]]:
    """``getPricesV3`` first, ``getPrices`` as the documented fallback."""
    quoted = urllib.parse.quote(key)
    quoted_service = urllib.parse.quote(service)
    for action in ("getPricesV3", "getPrices"):
        body = _get(session, f"{base}?action={action}&api_key={quoted}&service={quoted_service}")
        payload = _json(body)
        if not isinstance(payload, Mapping):
            continue
        tiers: dict[str, list[dict[str, Any]]] = {}
        for country_id, entry in payload.items():
            if not isinstance(entry, Mapping):
                continue
            cost = entry.get("cost") if entry.get("cost") is not None else entry.get("price")
            count = entry.get("count", entry.get("qty"))
            if cost is None:
                continue
            tiers[str(country_id)] = [
                {
                    "price": str(cost),
                    "count": int(count) if str(count or "").lstrip("-").isdigit() else -1,
                }
            ]
        if tiers:
            return tiers
    return {}


def _nexsms_catalog(session, base: str, key: str, service: str) -> dict[str, Any]:
    """``/api/countries`` for names, ``/api/getCountryByService`` for the price map.

    Both are needed: the country list carries names only, the by-service call carries
    the whole ``priceMap`` (measured 2026-09-23: 195 rows vs 183).
    """
    quoted = urllib.parse.quote(key)
    countries_payload = _json(_get(session, f"{base}/api/countries?apiKey={quoted}"))
    service_payload = _json(
        _get(session, f"{base}/api/getCountryByService?apiKey={quoted}&service={urllib.parse.quote(service)}")
    )
    names: dict[str, str] = {}
    for row in _data_list(countries_payload):
        country_id = str(row.get("id") or row.get("countryId") or "")
        if country_id:
            names[country_id] = str(row.get("name") or row.get("countryName") or country_id)

    countries = []
    for row in _data_list(service_payload):
        country_id = str(row.get("countryId") or row.get("id") or "")
        if not country_id:
            continue
        tiers = []
        for price, count in _price_map(row.get("priceMap")):
            tiers.append({"price": price, "count": count})
        if not tiers:
            continue
        countries.append(
            {
                "id": country_id,
                "name": str(row.get("countryName") or names.get(country_id) or country_id),
                "name_zh": "",
                "tiers": tiers,
            }
        )
    return {"ok": True, "countries": countries}


def _price_map(raw: Any) -> list[tuple[str, int]]:
    tiers: list[tuple[str, int]] = []
    if not isinstance(raw, Mapping):
        return tiers
    for price, count in raw.items():
        try:
            tiers.append((str(price), int(count)))
        except (TypeError, ValueError):
            continue
    return tiers


def _data_list(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("data", "list", "countries"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    return []


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _first_error(body: str) -> str:
    for line in (body or "").splitlines():
        if line.startswith("ERROR"):
            return line.strip()
    return ""


__all__ = [
    "NEXSMS_PROVIDERS",
    "OPENAI_SERVICE",
    "SMS_ACTIVATE_PROVIDERS",
    "family_of",
    "read_balance",
    "read_catalog",
]

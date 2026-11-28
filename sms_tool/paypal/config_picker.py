"""PayPal payment input selection: cards, addresses and result persistence.

Extracted from ``sms_tool.paypal.auto``. Owns every helper that *chooses* or
*persists* payment inputs; it contains no browser automation at all.

``_pick_phone_and_sms`` used to live here too, reading the static phone pool
(``paypal_auto.phone_numbers`` / ``phone_number`` + ``sms_api_url``). It was
removed on 2026-09-22 with the rest of that mode: the numbers it handed out
pointed at activations this codebase never created, so nothing completed or
cancelled them. See :data:`sms_tool.phone.sms_utils.NO_NUMBER_SOURCE_MESSAGE`.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from ..config import CFG
from ..storage import upsert_account
from .proxy import load_native_module

_NATIVE_PAYPAL_CONFIG_PICKER = load_native_module(
    "paypal-config-picker-rs",
    "paypal_config_picker_rs",
    "RUST_ANYTHING_UNIT_PAYPAL_CONFIG_PICKER",
)
_NATIVE_SAFE_PAYPAL_SESSION_STEM = getattr(_NATIVE_PAYPAL_CONFIG_PICKER, "_safe_paypal_session_stem", None)

_STATE_ABBREV = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def _normalize_us_state(state: Any) -> str:
    text = str(state or "")
    if len(text) > 2:
        return _STATE_ABBREV.get(text.lower(), text[:2].upper())
    return text


def _sanitize_paypal_alias_local(base_email: Any) -> str:
    text = str(base_email or "")
    if text and "@" in text:
        local = text.rsplit("@", 1)[0]
        return re.sub(r"[^a-zA-Z0-9.]", "", local)[:20]
    return ""


def _safe_paypal_session_stem(email: Any) -> str:
    if _NATIVE_SAFE_PAYPAL_SESSION_STEM is not None and isinstance(email, (str, type(None))):
        return str(_NATIVE_SAFE_PAYPAL_SESSION_STEM(email))
    cleaned = str(email or "unknown").replace("+", "")
    return re.sub(r"[^a-zA-Z0-9_.@-]+", "_", cleaned)


def _pick_card_and_address(cfg: dict) -> tuple[dict, dict]:
    cards = cfg.get("cards") or []
    addresses = cfg.get("addresses") or []
    if not cards or not addresses:
        raise RuntimeError("paypal_auto.cards and paypal_auto.addresses must be configured")

    index_file = cfg.get("card_index_file", "runtime/paypal_card_index.txt")
    idx = _read_index(index_file)
    card = cards[idx % len(cards)]
    addr = addresses[idx % len(addresses)]
    _write_index(index_file, idx + 1)

    state = _normalize_us_state(addr.get("state", ""))

    return card, {
        "line1": addr.get("line1", ""),
        "city": addr.get("city", ""),
        "state": state,
        "postal_code": addr.get("postal_code", ""),
    }


def _read_index(path: str) -> int:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_index(path: str, value: int):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value), encoding="utf-8")


def _generate_alias_email(base_email: str) -> str:
    """Generate a PayPal alias email (always Gmail) from base mailbox email."""
    gmail_local = _sanitize_paypal_alias_local(base_email)
    if not gmail_local:
        gmail_local = f"buyer{random.randint(1000, 9999)}"
    suffix = random.randint(100, 999)
    return f"{gmail_local}+pp{suffix}@gmail.com"


def _save_paypal_result(data: dict, json_path: str) -> str:
    """Save payment result to session JSON and SQLite."""
    if not json_path:
        safe = _safe_paypal_session_stem(data.get("email") or "unknown")
        output_dir = Path(CFG.get("output", {}).get("directory", "sessions"))
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = str(output_dir / f"session_{safe}_{int(time.time())}.json")

    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)
    return json_path

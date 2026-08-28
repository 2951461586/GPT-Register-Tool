"""PayPal payment input selection: cards, addresses, phone/SMS and result persistence.

Extracted from ``sms_tool.paypal_auto``. Owns every helper that *chooses* or
*persists* payment inputs; it contains no browser automation at all.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import CFG
from ..storage import upsert_account

_STATE_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

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

    state = addr.get("state", "")
    if len(state) > 2:
        state = _STATE_ABBREV.get(state.lower(), state[:2].upper())

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

def _pick_phone_and_sms(cfg: dict) -> tuple[str, str]:
    """Pick a phone number and SMS API URL via round-robin.

    Supports two config formats:
      - New:  "phone_numbers": [{"phone": "...", "sms_api_url": "..."}, ...]
      - Legacy fallback: single "phone_number" + "sms_api_url"
    """
    phone_list = cfg.get("phone_numbers") or []
    if phone_list:
        index_file = cfg.get("phone_index_file", "runtime/paypal_phone_index.txt")
        idx = _read_index(index_file)
        entry = phone_list[idx % len(phone_list)]
        _write_index(index_file, idx + 1)
        return entry["phone"], entry["sms_api_url"]

    # Legacy fallback
    return cfg.get("phone_number", ""), cfg.get("sms_api_url", "")

def _generate_alias_email(base_email: str) -> str:
    """Generate a PayPal alias email (always Gmail) from base mailbox email."""
    gmail_local = ""
    if base_email and "@" in base_email:
        local = base_email.rsplit("@", 1)[0]
        gmail_local = re.sub(r"[^a-zA-Z0-9.]", "", local)[:20]
    if not gmail_local:
        gmail_local = f"buyer{random.randint(1000, 9999)}"
    suffix = random.randint(100, 999)
    return f"{gmail_local}+pp{suffix}@gmail.com"

def _save_paypal_result(data: dict, json_path: str) -> str:
    """Save payment result to session JSON and SQLite."""
    if not json_path:
        email = (data.get("email") or "unknown").replace("+", "")
        safe = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", email)
        output_dir = Path(CFG.get("output", {}).get("directory", "sessions"))
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = str(output_dir / f"session_{safe}_{int(time.time())}.json")

    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)
    return json_path

"""Project-local PayPal one-click browser payment adapter.

The adapter keeps the public ``--one-click-pay --payment-method paypal``
boundary stable while using this repository's browser automation module. It
does not import an external browser project and it does not regenerate PayPal
links; a saved PayPal URL must already exist in the session/SQLite account row.
"""

from __future__ import annotations

import json
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

from .account_seed import extract_access_token as _seed_access_token
from .account_seed import load_account_seed as _load_seed
from .storage import list_paypal_accounts, mark_paypal_status, upsert_account
from .utils import _generate_password, _random_name


PROJECT_ROOT = Path(__file__).resolve().parent.parent

_FIRST_NAMES = ["James", "John", "Robert", "Michael", "David", "William", "Mary", "Linda", "Barbara", "Jennifer"]
_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Anderson"]
_US_ADDRESSES = [
    ("1209 Orange St", "Wilmington", "DE", "19801"),
    ("350 5th Ave", "New York", "NY", "10118"),
    ("600 Congress Ave", "Austin", "TX", "78701"),
    ("1 Market St", "San Francisco", "CA", "94105"),
    ("200 S Biscayne Blvd", "Miami", "FL", "33131"),
    ("500 W Madison St", "Chicago", "IL", "60661"),
]
_EMAIL_DOMAINS = ["outlook.com", "hotmail.com", "gmail.com", "yahoo.com"]


def one_click_pay_batch(args) -> None:
    """CLI entrypoint for PayPal browser one-click payment."""
    cfg = _load_config()
    browser_cfg = _browser_cfg(cfg)
    if not browser_cfg.get("enabled", True):
        print("[one-click-pay] paypal_browser disabled")
        return

    emails = _resolve_target_emails(args)
    if not emails:
        print("[one-click-pay] no pending accounts")
        return

    print(f"[one-click-pay] PayPal browser automation: {len(emails)} account(s)", flush=True)
    success_count = 0
    fail_count = 0
    for index, email in enumerate(emails, 1):
        print(f"\n[one-click-pay] === {index}/{len(emails)}: {email} ===", flush=True)
        result = one_click_pay(
            email=email,
            session_file=getattr(args, "session_file", "") if len(emails) == 1 else "",
            proxy=getattr(args, "proxy", None),
            cfg=cfg,
        )
        if result.get("ok"):
            mark_paypal_status(email, "completed")
            success_count += 1
            print(f"[one-click-pay] {email} payment completed: {result.get('callback_url', '')}", flush=True)
        else:
            fail_count += 1
            print(f"[one-click-pay] {email} payment failed: {result.get('error', 'unknown')}", flush=True)

    print(f"\n[one-click-pay] done: success={success_count} failed={fail_count} total={len(emails)}")


def one_click_pay(
    *,
    email: str = "",
    session_file: str = "",
    proxy: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run project-local PayPal browser automation for one ChatGPT account."""
    cfg = cfg or _load_config()
    browser_cfg = _browser_cfg(cfg)

    data, json_path = _load_seed(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    if target_email:
        data["email"] = target_email

    access_token = _seed_access_token(data)
    if not access_token:
        return {"ok": False, "email": target_email, "error": "missing_access_token"}

    paypal_url_result = _resolve_paypal_url(
        access_token,
        data=data,
        email=target_email,
        proxy=proxy,
        cfg=browser_cfg,
    )
    if not paypal_url_result.get("ok"):
        return {
            "ok": False,
            "email": target_email,
            "error": paypal_url_result.get("error", "paypal_url_unavailable"),
        }
    paypal_url = str(paypal_url_result.get("paypal_url") or "").strip()

    try:
        phone = get_next_phone(cfg)
        persona = _generate_persona(browser_cfg, target_email)
        print(
            "[one-click-pay] Browser profile: "
            f"engine={browser_cfg.get('browser_engine', 'camoufox')} "
            f"country={persona['country']} "
            f"email={persona['email']} "
            f"card=****{persona['card']['number'][-4:]} "
            f"phone=****{str(phone.get('phone', ''))[-4:]}",
            flush=True,
        )
        flow_result = _run_internal_browser_flow(
            browser_cfg,
            paypal_url=paypal_url,
            identity=persona["identity"],
            password=persona["password"],
            card=persona["card"],
            billing=persona["billing"],
            email=persona["email"],
            phone=phone,
            proxy=proxy,
            cookie_header=str(data.get("cookie_header") or ""),
        )
    except Exception as exc:
        result = {
            "ok": False,
            "email": target_email,
            "error": str(exc) or type(exc).__name__,
            "paypal_url": paypal_url,
        }
        _persist_browser_result(data, json_path, result)
        return result

    result = {
        **flow_result,
        "email": target_email,
        "paypal_url": paypal_url,
        "alias_email": flow_result.get("alias_email") or persona["email"],
        "card_last4": flow_result.get("card_last4") or persona["card"]["number"][-4:],
        "phone_last4": str(phone.get("phone", ""))[-4:],
        "country": persona["country"],
        "engine": str(browser_cfg.get("browser_engine") or "camoufox"),
        "password": flow_result.get("password") or persona["password"],
    }
    if result.get("ok"):
        result.setdefault("paypal_status", "completed")
    else:
        result.setdefault("error", "internal_browser_payment_failed")
    _persist_browser_result(data, json_path, result)
    return result


def _load_config() -> dict[str, Any]:
    try:
        return json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _browser_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    current = cfg.get("paypal_browser") if isinstance(cfg.get("paypal_browser"), dict) else {}
    legacy = cfg.get("paypal_auto") if isinstance(cfg.get("paypal_auto"), dict) else {}
    merged = {
        "enabled": True,
        "browser_engine": "camoufox",
        "country": "US",
        "card_brand": "visa",
        "email_mode": "random",
        "headless": True,
        "phone_index_file": "runtime/paypal_browser_phone_index.txt",
        "sms_poll_interval": 5,
        "sms_timeout": 120,
        "debug_screenshots": True,
        "debug_dir": "runtime/paypal_debug",
    }
    merged.update({k: v for k, v in legacy.items() if k not in {"cards", "addresses", "phone_numbers"}})
    merged.update(current)
    if "engine" in merged and "browser_engine" not in current:
        legacy_engine = str(merged.get("engine") or "").strip().lower()
        merged["browser_engine"] = "cloakbrowser" if legacy_engine == "cloakbrowser" else "camoufox"
    return merged


def _resolve_target_emails(args) -> list[str]:
    if getattr(args, "email", None):
        return [_normalize_email(args.email)]
    if getattr(args, "email_file", None):
        path = Path(args.email_file)
        values = []
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            value = raw.strip()
            if value and not value.startswith("#"):
                values.append(_normalize_email(value.split()[0]))
        return _unique(values)
    accounts = list_paypal_accounts()
    return _unique(
        a.get("email") or a.get("identifier") or ""
        for a in accounts
        if str(a.get("payment_method") or "paypal").strip().lower() != "gopay"
        and str(a.get("paypal_status") or "").strip().lower() != "completed"
    )


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _unique(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        item = _normalize_email(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _resolve_paypal_url(
    access_token: str,
    *,
    data: dict[str, Any],
    email: str,
    proxy: Optional[str],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    _ = access_token, proxy, cfg
    saved_paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    saved_url = str(saved_paypal.get("url") or data.get("paypal_url") or "").strip()
    saved_status = str(data.get("paypal_status") or saved_paypal.get("status") or "").strip().lower()
    updated_at = _int_value(data.get("paypal_updated_at") or saved_paypal.get("updated_at"))
    if (not saved_url or not updated_at) and email:
        rows = list_paypal_accounts(email)
        row = rows[0] if rows else {}
        saved_url = saved_url or str(row.get("paypal_url") or "").strip()
        saved_status = saved_status or str(row.get("paypal_status") or "").strip().lower()
        updated_at = _int_value(row.get("paypal_updated_at") or row.get("updated_at"))

    age = int(time.time()) - updated_at if updated_at else None
    if saved_url:
        print(f"[one-click-pay] Using saved PayPal URL: status={saved_status or '-'} age={age}", flush=True)
        return {"ok": True, "paypal_url": saved_url, "used_saved": True}

    return {"ok": False, "error": "missing_saved_paypal_url"}


def _generate_persona(cfg: dict[str, Any], target_email: str) -> dict[str, Any]:
    country = str(cfg.get("country") or "US").strip().upper()
    if country in {"", "AUTO"}:
        country = "US"
    if country != "US":
        # The current project-local browser form filler is US checkout oriented.
        country = "US"

    first_name, last_name = _random_name()
    if not first_name or not last_name:
        first_name = random.choice(_FIRST_NAMES)
        last_name = random.choice(_LAST_NAMES)
    alias_email = _payment_email(cfg, target_email)
    password = str(cfg.get("paypal_password") or _generate_password()).strip()
    card = _generate_card(str(cfg.get("card_brand") or "visa"))
    billing = _generate_address(country)

    return {
        "country": country,
        "email": alias_email,
        "identity": {"first_name": first_name, "last_name": last_name},
        "password": password,
        "card": card,
        "billing": billing,
    }


def _payment_email(cfg: dict[str, Any], target_email: str) -> str:
    mode = str(cfg.get("email_mode") or "random").strip().lower()
    if mode == "account" and target_email:
        return target_email
    if mode == "plus_alias" and target_email and "@" in target_email:
        local, domain = target_email.split("@", 1)
        return f"{local}+pp{random.randint(100000, 999999)}@{domain}"
    domains = cfg.get("email_domains") if isinstance(cfg.get("email_domains"), list) else _EMAIL_DOMAINS
    domain = str(random.choice(domains) if domains else "outlook.com").strip().lstrip("@") or "outlook.com"
    local = "pp" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"{local}@{domain}"


def _generate_card(brand: str) -> dict[str, str]:
    brand = str(brand or "visa").strip().lower()
    if brand in {"mastercard", "master"}:
        prefix = str(random.randint(51, 55))
        length = 16
        cvv_len = 3
    elif brand == "amex":
        prefix = random.choice(["34", "37"])
        length = 15
        cvv_len = 4
    else:
        brand = "visa"
        prefix = "4"
        length = 16
        cvv_len = 3

    body_len = length - len(prefix) - 1
    partial = prefix + "".join(random.choices(string.digits, k=body_len))
    number = partial + _luhn_check_digit(partial)
    exp_month = f"{random.randint(1, 12):02d}"
    exp_year = str(time.localtime().tm_year + random.randint(2, 5))
    cvv = "".join(random.choices(string.digits, k=cvv_len))
    return {
        "number": number,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "expiry_month": exp_month,
        "expiry_year": exp_year,
        "expiration_date": f"{exp_month}/{exp_year}",
        "cvv": cvv,
        "brand": brand,
    }


def _luhn_check_digit(partial: str) -> str:
    digits = [int(ch) for ch in partial]
    total = 0
    parity = (len(digits) + 1) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - (total % 10)) % 10)


def _generate_address(country: str) -> dict[str, str]:
    line1, city, state, postal_code = random.choice(_US_ADDRESSES)
    return {
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": country,
    }


def get_next_phone(cfg: dict[str, Any]) -> dict[str, str]:
    browser_cfg = _browser_cfg(cfg)
    pool = browser_cfg.get("phone_pool") or []
    if not pool:
        pool = (cfg.get("paypal_nocard") or {}).get("phone_pool") or []
    if not pool:
        auto = cfg.get("paypal_auto") or {}
        pool = auto.get("phone_numbers") or []
        if not pool and (auto.get("phone_number") or auto.get("sms_api_url")):
            pool = [{"phone": auto.get("phone_number", ""), "sms_api_url": auto.get("sms_api_url", "")}]
    pool = [item for item in pool if isinstance(item, dict)]
    if not pool:
        raise RuntimeError("PayPal browser automation requires paypal_browser.phone_pool or sms_api_url")
    index_file = str(browser_cfg.get("phone_index_file") or "runtime/paypal_browser_phone_index.txt")
    idx = _read_index(index_file)
    entry = pool[idx % len(pool)]
    _write_index(index_file, idx + 1)
    phone = str(entry.get("phone") or entry.get("phone_number") or "").strip()
    sms_api_url = str(entry.get("sms_api_url") or entry.get("sms_url") or "").strip()
    if not phone or not sms_api_url:
        raise RuntimeError("paypal_browser.phone_pool entries must include phone and sms_api_url")
    return {"phone": phone, "sms_api_url": sms_api_url}


def _run_internal_browser_flow(
    cfg: dict[str, Any],
    *,
    paypal_url: str,
    identity: dict[str, str],
    password: str,
    card: dict[str, str],
    billing: dict[str, str],
    email: str,
    phone: dict[str, str],
    proxy: Optional[str],
    cookie_header: str = "",
) -> dict[str, Any]:
    from .paypal_auto import _try_browser_pay

    return _try_browser_pay(
        paypal_url=paypal_url,
        card=card,
        address=billing,
        first_name=str(identity.get("first_name") or ""),
        last_name=str(identity.get("last_name") or ""),
        alias_email=email,
        password=password,
        phone=phone["phone"],
        sms_api_url=phone["sms_api_url"],
        cfg=cfg,
        proxy=proxy,
        headless=_bool_value(cfg.get("headless"), True),
        cookie_header=cookie_header,
    )


def _persist_browser_result(data: dict[str, Any], json_path: str, result: dict[str, Any]) -> None:
    now = int(time.time())
    paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    if result.get("paypal_url"):
        paypal["url"] = result["paypal_url"]
    paypal["status"] = "completed" if result.get("ok") else "failed"
    paypal["payment_method"] = "paypal"
    data["paypal"] = paypal
    data["payment_method"] = "paypal"
    data["paypal_status"] = paypal["status"]
    data["paypal_updated_at"] = now
    data["success"] = bool(data.get("success", True))
    if result.get("access_token"):
        data["access_token"] = result["access_token"]
    if result.get("oauth_refresh_token"):
        data["oauth_refresh_token"] = result["oauth_refresh_token"]
    if result.get("refresh_token_status"):
        data["refresh_token_status"] = result["refresh_token_status"]
        data["refresh_token_updated_at"] = now
    if result.get("password"):
        data["password"] = result["password"]
    if result.get("ok"):
        data["paypal_completed_at"] = now
        data["paypal_browser"] = {
            "ok": True,
            "engine": result.get("engine", ""),
            "country": result.get("country", ""),
            "alias_email": result.get("alias_email", ""),
            "card_last4": result.get("card_last4", ""),
            "phone_last4": result.get("phone_last4", ""),
            "callback_url": result.get("callback_url", ""),
            "completed_at": now,
        }
    else:
        data["paypal_browser"] = {
            "ok": False,
            "error": result.get("error", ""),
            "updated_at": now,
        }
    if json_path:
        target = Path(json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)


def _read_index(path: str) -> int:
    try:
        return int(_project_file(path).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_index(path: str, value: int) -> None:
    target = _project_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(value), encoding="utf-8")


def _project_file(path: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default

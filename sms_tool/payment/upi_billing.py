"""Sticky, de-duplicated Indian billing identities for the UPI lane.

Why this module exists
----------------------
``session._upi_billing_profile`` used to pick a name from **5** candidates and an
address from **4**, with ``secrets.randbelow``, on every single invocation.  Two
consequences, both measured as risk signals rather than as bugs:

* one account retried three times presented three different names *and* three
  different addresses -- a billing identity that mutates between attempts is a
  contradiction no legitimate user produces;
* with 20 combinations, a 4-lane batch (``payment.batch`` caps UPI at 4 workers)
  shares an address across accounts with near-certainty, which links otherwise
  unrelated accounts together.

The reference implementation solves both (``upi_extract.py:4132-4600``): a
deterministic seed per account, a pool big enough that collisions are avoidable,
a cross-process lock so two lanes cannot take the same row, and one persisted
sticky assignment per account.  This module is that design, minus the
``hindi``/Devanagari variant -- see :func:`billing_script`.

Contract kept from the old implementation
-----------------------------------------
``billing_profile()`` returns the same keys (``email``, ``name``, ``country``,
``line1``, ``line2``, ``city``, ``postal_code``, ``state``) and the same override
chain: **pool → ``fixed_billing`` / ``UPI_USE_FIXED_BILLING`` → per-field
environment**.  Only the *source* of the pool value changed (sticky when an
account key is known, random when it is not).
"""

from __future__ import annotations

import hashlib
import random
import re
import secrets
import time
from collections.abc import Mapping
from typing import Any

from .upi_identity_store import env_flag, env_text
from .upi_identity_store import account_entry, ledger_transaction

#: Email domains offered when generating a billing address.
EMAIL_DOMAINS: tuple[str, ...] = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")

#: Baseline for ``UPI_USE_FIXED_BILLING`` / ``upi.fixed_billing``.  Owned here
#: (with the name/address pools) so the frozen ``upi_link`` package does not have
#: to keep the data; ``upi_link.constants`` re-exports it for the facade.
FIXED_BILLING: dict[str, str] = {
    "name": "Rahul Sharma",
    "email": "upi-scanner@example.com",
    "line1": "Flat 302, Sai Residency",
    "line2": "MG Road, Andheri East",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal": "400069",
    "country": "IN",
}

#: Back-compat aliases: the public names the facade exposes.
UPI_EMAIL_DOMAINS = EMAIL_DOMAINS
UPI_BILLING_IN = FIXED_BILLING

#: Given/family name pool.  44 × 34 = 1496 name pairs, every one Latin script.
#: Deliberately all-Latin: see :func:`billing_script`.
BILLING_NAMES: tuple[tuple[str, str], ...] = (
    ("Aisha", "Sharma"),
    ("Arjun", "Mehta"),
    ("Kavya", "Gupta"),
    ("Rohan", "Kapoor"),
    ("Priya", "Nair"),
    ("Vikram", "Iyer"),
    ("Ananya", "Reddy"),
    ("Siddharth", "Chopra"),
    ("Meera", "Joshi"),
    ("Aditya", "Rao"),
    ("Neha", "Desai"),
    ("Karan", "Malhotra"),
    ("Divya", "Pillai"),
    ("Rahul", "Banerjee"),
    ("Sneha", "Kulkarni"),
    ("Nikhil", "Agarwal"),
    ("Pooja", "Shetty"),
    ("Manish", "Verma"),
    ("Ritika", "Saxena"),
    ("Amit", "Trivedi"),
    ("Tanvi", "Bhatt"),
    ("Harish", "Menon"),
    ("Isha", "Chauhan"),
    ("Varun", "Bhatia"),
    ("Shruti", "Mahajan"),
    ("Deepak", "Pandey"),
    ("Anjali", "Sethi"),
    ("Sameer", "Qureshi"),
    ("Kritika", "Mishra"),
    ("Rajat", "Sinha"),
    ("Nandini", "Krishnan"),
    ("Gaurav", "Yadav"),
    ("Aditi", "Ranganathan"),
    ("Suresh", "Patil"),
    ("Aarav", "Kohli"),
    ("Lakshmi", "Venkatesh"),
    ("Farhan", "Ali"),
    ("Swati", "Bhardwaj"),
    ("Yash", "Thakur"),
    ("Nisha", "Ramesh"),
    ("Kabir", "Sundaram"),
    ("Preeti", "Chakraborty"),
    ("Aravind", "Subramanian"),
    ("Zoya", "Khan"),
)

#: ``(street, city, postal_code, state)`` rows.
#:
#: ``state`` is the two-letter code, matching what the old random pool sent in
#: ``tax_region[state]``.  Rows are spread over 22 states so that an exit in any
#: major circle can be given a same-state address later without inventing data.
BILLING_ADDRESSES: tuple[tuple[str, str, str, str], ...] = (
    ("24 Park Street", "Kolkata", "700016", "WB"),
    ("14 MG Road", "Bengaluru", "560001", "KA"),
    ("18 Marine Drive", "Mumbai", "400020", "MH"),
    ("32 Connaught Place", "New Delhi", "110001", "DL"),
    ("7 Banjara Hills Road", "Hyderabad", "500034", "TS"),
    ("41 Anna Salai", "Chennai", "600002", "TN"),
    ("12 Law Garden Road", "Ahmedabad", "380006", "GJ"),
    ("9 Koregaon Park Lane", "Pune", "411001", "MH"),
    ("63 Hazratganj Road", "Lucknow", "226001", "UP"),
    ("5 Civil Lines Road", "Jaipur", "302006", "RJ"),
    ("22 Sector 17 Road", "Chandigarh", "160017", "CH"),
    ("3 Salt Lake Road", "Kolkata", "700091", "WB"),
    ("88 Andheri East Road", "Mumbai", "400069", "MH"),
    ("17 Indiranagar Road", "Bengaluru", "560038", "KA"),
    ("29 Jubilee Hills Road", "Hyderabad", "500033", "TS"),
    ("11 Adyar Road", "Chennai", "600020", "TN"),
    ("56 Navrangpura Road", "Ahmedabad", "380009", "GJ"),
    ("4 Kothrud Road", "Pune", "411038", "MH"),
    ("38 Gomti Nagar Road", "Lucknow", "226010", "UP"),
    ("15 Malviya Nagar Road", "Jaipur", "302017", "RJ"),
    ("21 Sector 35 Road", "Chandigarh", "160022", "CH"),
    ("6 Bistupur Road", "Jamshedpur", "831001", "JH"),
    ("47 Ashok Nagar Road", "Patna", "800001", "BR"),
    ("13 Bhubaneswar Road", "Bhubaneswar", "751001", "OD"),
    ("25 Guwahati Road", "Guwahati", "781005", "AS"),
    ("8 Kochi Marine Road", "Kochi", "682016", "KL"),
    ("19 Trivandrum Road", "Thiruvananthapuram", "695001", "KL"),
    ("33 Coimbatore Road", "Coimbatore", "641002", "TN"),
    ("2 Madurai Road", "Madurai", "625001", "TN"),
    ("44 Visakhapatnam Road", "Visakhapatnam", "530003", "AP"),
    ("16 Vijayawada Road", "Vijayawada", "520010", "AP"),
    ("27 Nagpur Road", "Nagpur", "440010", "MH"),
    ("36 Nashik Road", "Nashik", "422005", "MH"),
    ("51 Bhopal Road", "Bhopal", "462003", "MP"),
    ("7 Indore Road", "Indore", "452001", "MP"),
    ("23 Raipur Road", "Raipur", "492001", "CG"),
    ("45 Dehradun Road", "Dehradun", "248001", "UK"),
    ("31 Panaji Road", "Panaji", "403001", "GA"),
    ("10 Mysuru Road", "Mysuru", "570001", "KA"),
    ("39 Mangaluru Road", "Mangaluru", "575003", "KA"),
    ("54 Guntur Road", "Guntur", "522002", "AP"),
    ("26 Siliguri Road", "Siliguri", "734001", "WB"),
    ("42 Ranchi Road", "Ranchi", "834001", "JH"),
    ("18 Varanasi Road", "Varanasi", "221001", "UP"),
    ("58 Agra Road", "Agra", "282001", "UP"),
    ("12 Jodhpur Road", "Jodhpur", "342001", "RJ"),
    ("40 Udaipur Road", "Udaipur", "313001", "RJ"),
    ("35 Surat Road", "Surat", "395003", "GJ"),
)

#: House-number ceiling and the suffixes that make "157" vs "157A" distinct.
_HOUSE_LIMIT = 400
_HOUSE_SUFFIXES = ("", "", "", "A", "B", "C")

#: Public aliases kept for the facade (``sms_tool.upi_link.UPI_BILLING_NAMES``).
#: P0-2 moved these pools out of ``constants`` without renaming them.
UPI_BILLING_NAMES = BILLING_NAMES
UPI_BILLING_ADDRESSES = BILLING_ADDRESSES

#: How many deterministic candidates to probe before accepting a collision.
#: 1496 names × 48 addresses × 400 houses is far more than a batch will ever
#: need; the cap only bounds a corrupt ledger that claims everything is taken.
_MAX_PROBE = 20000

#: Billing text script.  Only ``latin`` is implemented on purpose -- see
#: :func:`billing_script`.
_SCRIPTS: frozenset[str] = frozenset({"latin"})


def _int(value: Any, default: int = 0) -> int:
    """Parse an int defensively; a malformed seed must not raise."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def billing_script() -> str:
    """Billing text script for the invoice address.

    ``latin`` is the only implemented value, and that is a measurement, not an
    omission: the reference implementation ships a Devanagari (``hindi``) variant
    and its own module header records the A/B result -- *"出链率与账单脚本强相关
    （latin 69.6% / hindi 0%）"*.  There is no Devanagari name/street/PIN data in
    this repository to validate against, so shipping one would be inventing
    evidence.  ``UPI_BILLING_SCRIPT`` is read (and an unknown value falls back to
    ``latin``) so the switch exists the day real data does.
    """
    value = env_text("UPI_BILLING_SCRIPT", "latin").strip().lower() or "latin"
    return value if value in _SCRIPTS else "latin"


def _seed_for(account_key: str) -> int:
    """Deterministic 63-bit seed from the account key.

    A per-account seed is what makes a *retry* land on the same candidate before
    the ledger has an entry for it (first run: allocate; crash before write:
    next run rebuilds the same identity instead of a new one).
    """
    text = str(account_key or "").strip()
    if not text:
        return secrets.randbits(63)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _email_for(first_name: str, last_name: str, rng: random.Random) -> str:
    domain = rng.choice(EMAIL_DOMAINS)
    return f"{first_name.lower()}.{last_name.lower()}{rng.randint(1000, 9999)}@{domain}"


def billing_identity(seed: int, step: int = 0) -> tuple[dict[str, str], str]:
    """Build candidate ``#step`` for ``seed``; returns ``(profile, identity)``.

    ``identity`` is the de-duplication key (name + house/street + city + PIN +
    state, lower-cased).  It deliberately excludes the email: two accounts may
    not share a *name and address*, but an email is per-account by construction.
    """
    mixed = (_int(seed) + _int(step) * 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
    rng = random.Random(mixed)
    first_name, last_name = rng.choice(BILLING_NAMES)
    street, city, postal_code, state = rng.choice(BILLING_ADDRESSES)
    house = f"{rng.randint(1, _HOUSE_LIMIT)}{rng.choice(_HOUSE_SUFFIXES)}"
    # The curated streets start with a number for readability; strip it so the
    # generated house number does not stack ("157 24 Park Street").
    street_text = re.sub(r"^\d{1,4}[A-Za-z]?\s+", "", street).strip() or street
    line1 = f"{house} {street_text}"
    profile = {
        "email": _email_for(first_name, last_name, rng),
        "name": f"{first_name} {last_name}",
        "country": "IN",
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal_code,
        "state": state,
    }
    identity = "|".join((profile["name"], line1, city, postal_code, state)).lower()
    return profile, identity


def _random_profile() -> dict[str, str]:
    profile, _identity = billing_identity(secrets.randbits(63), 0)
    return profile


def assigned_profile(account_key: str, cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """This account's sticky billing identity, allocating a unique one if new."""
    key = str(account_key or "").strip()
    if not key:
        return _random_profile()
    with ledger_transaction(cfg=cfg) as ledger:
        entry = account_entry(ledger, key)
        billing = entry.get("billing")
        if isinstance(billing, Mapping):
            profile = billing.get("profile")
            if isinstance(profile, Mapping) and profile.get("line1") and profile.get("postal_code"):
                return {str(k): str(v) for k, v in profile.items()}
        used = ledger.get("identities")
        taken: set[str] = set(used) if isinstance(used, Mapping) else set()
        seed = _seed_for(key)
        candidate: dict[str, str] | None = None
        identity = ""
        for step in range(_MAX_PROBE):
            candidate, identity = billing_identity(seed, step)
            if identity not in taken:
                break
        if candidate is None:  # pragma: no cover - needs a corrupt ledger
            candidate, identity = billing_identity(secrets.randbits(63), 0)
        identities = ledger.setdefault("identities", {})
        identities[identity] = key
        entry["billing"] = {
            "identity": identity,
            "profile": candidate,
            "script": billing_script(),
            "assigned_at": time.time(),
        }
        return dict(candidate)


def _fixed_overlay(profile: dict[str, str], cfg: Mapping[str, Any] | None) -> dict[str, str]:
    """``upi.fixed_billing`` / ``UPI_USE_FIXED_BILLING`` overlay (unchanged)."""
    section: Mapping[str, Any] = cfg if isinstance(cfg, Mapping) else {}
    fixed = section.get("fixed_billing")
    if not (env_flag("UPI_USE_FIXED_BILLING", False) or fixed):
        return profile
    baseline: dict[str, Any] = dict(FIXED_BILLING)
    if isinstance(fixed, Mapping):
        baseline.update({str(k): str(v) for k, v in fixed.items() if v is not None})
    # The fixed profile's key is ``postal``; the internal one is ``postal_code``.
    if baseline.get("postal") and not baseline.get("postal_code"):
        baseline["postal_code"] = baseline["postal"]
    merged = dict(profile)
    merged.update({k: str(v) for k, v in baseline.items() if k in merged})
    return merged


_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("email", "UPI_EMAIL"),
    ("name", "UPI_NAME"),
    ("country", "UPI_BILLING_COUNTRY"),
    ("line1", "UPI_LINE1"),
    ("line2", "UPI_LINE2"),
    ("city", "UPI_CITY"),
    ("postal_code", "UPI_POSTAL_CODE"),
    ("state", "UPI_STATE"),
)


def billing_profile(
    cfg: Mapping[str, Any] | None = None,
    *,
    account_key: str = "",
    use_store: bool = True,
) -> dict[str, str]:
    """Billing profile for one account: **sticky** when the account is known.

    ``use_store=False`` reproduces the historical fully-random behaviour; it
    exists for tests and for an operator escape hatch, not as the default.
    """
    key = str(account_key or "").strip()
    if use_store and key:
        try:
            profile = assigned_profile(key, cfg)
        except Exception:
            # A locked or unwritable ledger must never stop a link. Falling back
            # to a random identity is exactly the pre-existing behaviour.
            profile = _random_profile()
    else:
        profile = _random_profile()
    profile = _fixed_overlay(profile, cfg)
    for field, env_name in _ENV_FIELDS:
        value = env_text(env_name, "")
        if value:
            profile[field] = value
    profile["country"] = str(profile.get("country") or "IN").strip().upper()
    return profile


__all__ = [
    "BILLING_ADDRESSES",
    "EMAIL_DOMAINS",
    "BILLING_NAMES",
    "FIXED_BILLING",
    "UPI_BILLING_ADDRESSES",
    "UPI_BILLING_NAMES",
    "assigned_profile",
    "billing_identity",
    "billing_profile",
    "billing_script",
]

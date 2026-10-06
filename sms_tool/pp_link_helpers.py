"""Shared helpers for PP (PayPal) direct-link generation.

Extracted from ``gen_pp_link.py`` to keep the extractor focused on the
three-stage proxy-routing logic.  These utilities cover:

* proxy template normalization and per-country variable substitution
* BA / EC token extraction from redirect URLs
* redirect resolution (internal → external)
* Stripe amount / diagnostics utilities
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

import requests

try:
    from .checkout_contract import CheckoutRequestContract, CheckoutSessionContract
    from .cross_process_gate import cross_process_write_lock
    from .paths import runtime_file
    from .phone_proxy import match_proxy_region, normalize_proxy_url
except ImportError:  # pragma: no cover - direct script execution
    from checkout_contract import CheckoutRequestContract, CheckoutSessionContract  # type: ignore
    from cross_process_gate import cross_process_write_lock  # type: ignore
    from paths import runtime_file  # type: ignore
    from phone_proxy import match_proxy_region, normalize_proxy_url  # type: ignore

try:
    from .paypal_proxy import (
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )
except ImportError:  # pragma: no cover - direct script execution
    from paypal_proxy import (  # type: ignore
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )

# curl_cffi functional API (preferred for checkout to avoid Session cookie conflicts)
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_STRIPE_PK = (os.environ.get("PP_STRIPE_PUBLISHABLE_KEY", "") or "").strip() or (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
from .timeouts import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT

RETRY_ATTEMPTS = 3

_SIDE_EFFECT_STAGES = frozenset({"confirm", "approve", "poll", "follow_redirect"})

PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+", re.I)
PAYPAL_BA_RE = re.compile(r"https://www\.paypal\.com/agreements/approve\?[^\s\"']+", re.I)

BILLING_DATA = {
    "DE": {
        "name": ("Lukas", "Schneider"),
        "street": "Friedrichstrasse 123",
        "city": "Berlin",
        "state": "BE",
        "postal": "10117",
    },
    "GB": {
        "name": ("James", "Smith"),
        "street": "10 Downing Street",
        "city": "London",
        "state": "London",
        "postal": "SW1A 2AA",
    },
    "US": {
        "name": ("James", "Smith"),
        "street": "3110 Sunset Boulevard",
        "city": "Los Angeles",
        "state": "CA",
        "postal": "90026",
    },
    "AU": {
        "name": ("Oliver", "Smith"),
        "street": "123 George Street",
        "city": "Sydney",
        "state": "NSW",
        "postal": "2000",
    },
    "JP": {
        "name": ("Taro", "Yamada"),
        "street": "1-1-2 Oshiage",
        "city": "Sumida-ku",
        "state": "Tokyo",
        "postal": "131-0045",
    },
    "FR": {
        "name": ("Pierre", "Dupont"),
        "street": "10 Rue de Rivoli",
        "city": "Paris",
        "state": "Ile-de-France",
        "postal": "75001",
    },
    "CA": {
        "name": ("James", "Smith"),
        "street": "100 King Street W",
        "city": "Toronto",
        "state": "ON",
        "postal": "M5X 1C6",
    },
    "SG": {
        "name": ("Wei", "Tan"),
        "street": "1 Raffles Place",
        "city": "Singapore",
        "state": "Singapore",
        "postal": "048616",
    },
    "NZ": {
        "name": ("James", "Smith"),
        "street": "1 Queen Street",
        "city": "Auckland",
        "state": "Auckland",
        "postal": "1010",
    },
    "IE": {
        "name": ("James", "Smith"),
        "street": "1 O'Connell Street",
        "city": "Dublin",
        "state": "Dublin",
        "postal": "D01 F5P2",
    },
    "TH": {
        "name": ("Somchai", "Prasert"),
        "street": "123 Sukhumvit Road",
        "city": "Bangkok",
        "state": "Bangkok",
        "postal": "10110",
    },
    "TR": {
        "name": ("Mehmet", "Yilmaz"),
        "street": "Istiklal Caddesi 123",
        "city": "Istanbul",
        "state": "Istanbul",
        "postal": "34421",
    },
    "IN": {
        "name": ("Rahul", "Sharma"),
        "street": "Flat 302, Sai Residency",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal": "400069",
    },
    "BR": {
        "name": ("Joao", "Silva"),
        "street": "Avenida Paulista 1000",
        "city": "Sao Paulo",
        "state": "SP",
        "postal": "01310-100",
    },
    "KR": {"name": ("Minjun", "Kim"), "street": "123 Teheran-ro", "city": "Seoul", "state": "Seoul", "postal": "06134"},
    "NL": {"name": ("Daan", "de Vries"), "street": "Damrak 1", "city": "Amsterdam", "state": "NH", "postal": "1012 JS"},
    "ES": {"name": ("Hugo", "Garcia"), "street": "Calle Mayor 1", "city": "Madrid", "state": "MD", "postal": "28013"},
    "IT": {
        "name": ("Alessandro", "Rossi"),
        "street": "Via del Corso 1",
        "city": "Rome",
        "state": "RM",
        "postal": "00186",
    },
    "PL": {"name": ("Jan", "Kowalski"), "street": "Nowy Swiat 1", "city": "Warsaw", "state": "MZ", "postal": "00-001"},
    "SE": {
        "name": ("Erik", "Andersson"),
        "street": "Drottninggatan 1",
        "city": "Stockholm",
        "state": "AB",
        "postal": "111 51",
    },
    "NO": {
        "name": ("Lars", "Hansen"),
        "street": "Karl Johans gate 1",
        "city": "Oslo",
        "state": "Oslo",
        "postal": "0154",
    },
    "DK": {
        "name": ("Magnus", "Nielsen"),
        "street": "Stroget 1",
        "city": "Copenhagen",
        "state": "Hovedstaden",
        "postal": "1050",
    },
    "FI": {
        "name": ("Matti", "Korhonen"),
        "street": "Aleksanterinkatu 1",
        "city": "Helsinki",
        "state": "Uusimaa",
        "postal": "00100",
    },
    "CH": {"name": ("Luis", "Keller"), "street": "Bahnhofstrasse 1", "city": "Zurich", "state": "ZH", "postal": "8001"},
    "AT": {
        "name": ("Paul", "Hofer"),
        "street": "Karntner Strasse 1",
        "city": "Vienna",
        "state": "Wien",
        "postal": "1010",
    },
    "BE": {"name": ("Lucas", "Peeters"), "street": "Rue Neuve 1", "city": "Brussels", "state": "BRU", "postal": "1000"},
    "PT": {
        "name": ("Joao", "Silva"),
        "street": "Rua Augusta 1",
        "city": "Lisbon",
        "state": "Lisboa",
        "postal": "1100-053",
    },
    "CZ": {
        "name": ("Jakub", "Novak"),
        "street": "Vaclavske namesti 1",
        "city": "Prague",
        "state": "PR",
        "postal": "110 00",
    },
    "GR": {
        "name": ("Georgios", "Papadopoulos"),
        "street": "Ermou 1",
        "city": "Athens",
        "state": "ATT",
        "postal": "105 63",
    },
    "HU": {"name": ("Laszlo", "Nagy"), "street": "Vaci utca 1", "city": "Budapest", "state": "BU", "postal": "1052"},
    "RO": {
        "name": ("Andrei", "Popescu"),
        "street": "Calea Victoriei 1",
        "city": "Bucharest",
        "state": "B",
        "postal": "010061",
    },
    "AE": {
        "name": ("Ahmed", "Al Mansoori"),
        "street": "Sheikh Zayed Road 1",
        "city": "Dubai",
        "state": "DU",
        "postal": "00000",
    },
    "SA": {
        "name": ("Abdullah", "Al Saud"),
        "street": "King Fahd Road 1",
        "city": "Riyadh",
        "state": "RD",
        "postal": "11564",
    },
    "IL": {
        "name": ("David", "Cohen"),
        "street": "Dizengoff Street 1",
        "city": "Tel Aviv",
        "state": "TA",
        "postal": "6433201",
    },
    "ZA": {
        "name": ("Thabo", "Mokoena"),
        "street": "Long Street 1",
        "city": "Cape Town",
        "state": "WC",
        "postal": "8001",
    },
    "EG": {"name": ("Omar", "Hassan"), "street": "Tahrir Square 1", "city": "Cairo", "state": "C", "postal": "11511"},
    "MX": {
        "name": ("Diego", "Garcia"),
        "street": "Avenida Reforma 1",
        "city": "Mexico City",
        "state": "CMX",
        "postal": "06600",
    },
    "AR": {
        "name": ("Mateo", "Gonzalez"),
        "street": "Avenida de Mayo 1",
        "city": "Buenos Aires",
        "state": "C",
        "postal": "C1084",
    },
    "CL": {
        "name": ("Benjamin", "Lopez"),
        "street": "Avenida Libertador 1",
        "city": "Santiago",
        "state": "RM",
        "postal": "8320000",
    },
    "CO": {
        "name": ("Santiago", "Ramirez"),
        "street": "Carrera 7 1",
        "city": "Bogota",
        "state": "DC",
        "postal": "110111",
    },
    "PE": {
        "name": ("Diego", "Fernandez"),
        "street": "Avenida Larco 1",
        "city": "Lima",
        "state": "LIM",
        "postal": "15074",
    },
    "MY": {
        "name": ("Ahmad", "Rahman"),
        "street": "Jalan Bukit Bintang 1",
        "city": "Kuala Lumpur",
        "state": "KUL",
        "postal": "55100",
    },
    "ID": {
        "name": ("Budi", "Santoso"),
        "street": "Jalan Thamrin 1",
        "city": "Jakarta",
        "state": "JK",
        "postal": "10310",
    },
    "VN": {
        "name": ("Minh", "Nguyen"),
        "street": "Dong Khoi 1",
        "city": "Ho Chi Minh City",
        "state": "SG",
        "postal": "700000",
    },
    "PH": {"name": ("Juan", "Santos"), "street": "Ayala Avenue 1", "city": "Makati", "state": "NCR", "postal": "1226"},
    "HK": {
        "name": ("Ka", "Chan"),
        "street": "Queens Road Central 1",
        "city": "Hong Kong",
        "state": "HK",
        "postal": "000000",
    },
    "TW": {"name": ("Wei", "Chen"), "street": "Xinyi Road 1", "city": "Taipei", "state": "TPE", "postal": "110"},
    "UA": {
        "name": ("Oleksandr", "Shevchenko"),
        "street": "Khreshchatyk 1",
        "city": "Kyiv",
        "state": "KV",
        "postal": "01001",
    },
    "NG": {
        "name": ("Chinedu", "Okafor"),
        "street": "Broad Street 1",
        "city": "Lagos",
        "state": "LA",
        "postal": "100001",
    },
    "KE": {
        "name": ("James", "Mwangi"),
        "street": "Kenyatta Avenue 1",
        "city": "Nairobi",
        "state": "NRB",
        "postal": "00100",
    },
}

#: Extra per-country billing identities beyond ``BILLING_DATA[code]``.
#:
#: ``BILLING_DATA`` holds exactly one identity per country, so every Checkout
#: for that country billed from the *same* street address. A single invariant
#: address across N checkouts is itself an observable pattern; each curated
#: market therefore gets a small pool of equally plausible alternatives.
#:
#: 🔴 ``BILLING_DATA[code]`` is always ``billing_address_pool(code)[0]``, so
#: ``billing_for_country(code)`` stays byte-identical to its previous output for
#: the default call. That matters: the two billing lookups inside one PayPal run
#: (``_sync_tax_region`` and ``_create_payment_method``) are two stages of the
#: *same* checkout and must not disagree on the billing identity.
#:
#: Addresses are hand-authored, not geocoded -- this path deliberately carries
#: no external geocoding dependency and no per-call network traffic.
BILLING_ADDRESS_POOLS: dict[str, tuple[dict, ...]] = {
    "DE": (
        {"name": ("Jonas", "Weber"), "street": "Leopoldstrasse 42", "city": "Munich", "state": "BY", "postal": "80802"},
        {"name": ("Felix", "Hoffmann"), "street": "Monckebergstrasse 7", "city": "Hamburg", "state": "HH", "postal": "20095"},
        {"name": ("Marie", "Koch"), "street": "Hohe Strasse 68", "city": "Cologne", "state": "NW", "postal": "50667"},
    ),
    "GB": (
        {"name": ("Harry", "Bennett"), "street": "12 Deansgate", "city": "Manchester", "state": "England", "postal": "M3 2BW"},
        {"name": ("Ella", "Fraser"), "street": "45 Princes Street", "city": "Edinburgh", "state": "Scotland", "postal": "EH2 2BY"},
        {"name": ("Owen", "Rees"), "street": "8 Queen Street", "city": "Cardiff", "state": "Wales", "postal": "CF10 2BY"},
    ),
    "US": (
        {"name": ("Michael", "Torres"), "street": "350 5th Avenue", "city": "New York", "state": "NY", "postal": "10118"},
        {"name": ("Sarah", "Whitfield"), "street": "233 S Wacker Drive", "city": "Chicago", "state": "IL", "postal": "60606"},
        {"name": ("Daniel", "Pierce"), "street": "1420 5th Avenue", "city": "Seattle", "state": "WA", "postal": "98101"},
    ),
    "FR": (
        {"name": ("Camille", "Leroy"), "street": "25 Rue de la Republique", "city": "Lyon", "state": "Auvergne-Rhone-Alpes", "postal": "69002"},
        {"name": ("Mathieu", "Moreau"), "street": "9 La Canebiere", "city": "Marseille", "state": "Provence-Alpes-Cote d'Azur", "postal": "13001"},
        {"name": ("Chloe", "Girard"), "street": "14 Rue d'Alsace-Lorraine", "city": "Toulouse", "state": "Occitanie", "postal": "31000"},
    ),
    "JP": (
        {"name": ("Kenji", "Sato"), "street": "3-1-1 Umeda", "city": "Osaka", "state": "Osaka", "postal": "530-0001"},
        {"name": ("Yuki", "Tanaka"), "street": "1-1-4 Meieki", "city": "Nagoya", "state": "Aichi", "postal": "450-0002"},
        {"name": ("Aoi", "Suzuki"), "street": "2-1-1 Kita-ichijo", "city": "Sapporo", "state": "Hokkaido", "postal": "060-0001"},
    ),
    "NL": (
        {"name": ("Sem", "Bakker"), "street": "Coolsingel 40", "city": "Rotterdam", "state": "ZH", "postal": "3011 AD"},
        {"name": ("Lotte", "Visser"), "street": "Spui 68", "city": "The Hague", "state": "ZH", "postal": "2511 BT"},
        {"name": ("Bram", "Smit"), "street": "Oudegracht 114", "city": "Utrecht", "state": "UT", "postal": "3511 AW"},
    ),
    "BR": (
        {"name": ("Rafael", "Costa"), "street": "Avenida Rio Branco 156", "city": "Rio de Janeiro", "state": "RJ", "postal": "20040-901"},
        {"name": ("Beatriz", "Almeida"), "street": "Avenida Afonso Pena 1212", "city": "Belo Horizonte", "state": "MG", "postal": "30130-003"},
        {"name": ("Lucas", "Ribeiro"), "street": "Rua XV de Novembro 300", "city": "Curitiba", "state": "PR", "postal": "80020-310"},
    ),
    "KR": (
        {"name": ("Jihoon", "Park"), "street": "100 Jungang-daero", "city": "Busan", "state": "Busan", "postal": "48939"},
        {"name": ("Seoyeon", "Choi"), "street": "25 Inha-ro", "city": "Incheon", "state": "Incheon", "postal": "22212"},
        {"name": ("Hyunwoo", "Jung"), "street": "88 Dongseong-ro", "city": "Daegu", "state": "Daegu", "postal": "41911"},
    ),
    "PL": (
        {"name": ("Piotr", "Nowak"), "street": "Rynek Glowny 12", "city": "Krakow", "state": "MA", "postal": "31-042"},
        {"name": ("Anna", "Wisniewska"), "street": "Dlugi Targ 24", "city": "Gdansk", "state": "PM", "postal": "80-828"},
        {"name": ("Tomasz", "Lewandowski"), "street": "Rynek 15", "city": "Wroclaw", "state": "DS", "postal": "50-101"},
    ),
    "CH": (
        {"name": ("Nicolas", "Rochat"), "street": "Rue du Rhone 62", "city": "Geneva", "state": "GE", "postal": "1204"},
        {"name": ("Sandra", "Brunner"), "street": "Freie Strasse 40", "city": "Basel", "state": "BS", "postal": "4001"},
        {"name": ("Martin", "Gerber"), "street": "Spitalgasse 30", "city": "Bern", "state": "BE", "postal": "3011"},
    ),
    "VN": (
        {"name": ("Trung", "Tran"), "street": "32 Hang Bai", "city": "Hanoi", "state": "HN", "postal": "100000"},
        {"name": ("Linh", "Pham"), "street": "88 Bach Dang", "city": "Da Nang", "state": "DN", "postal": "550000"},
        {"name": ("Hoa", "Le"), "street": "12 Tran Phu", "city": "Nha Trang", "state": "KH", "postal": "650000"},
    ),
    "PH": (
        {"name": ("Ramon", "Dela Cruz"), "street": "1000 Roxas Boulevard", "city": "Manila", "state": "NCR", "postal": "1000"},
        {"name": ("Grace", "Reyes"), "street": "1100 Quezon Avenue", "city": "Quezon City", "state": "NCR", "postal": "1100"},
        {"name": ("Paolo", "Bautista"), "street": "6000 Osmena Boulevard", "city": "Cebu City", "state": "Region VII", "postal": "6000"},
    ),
    "ID": (
        {"name": ("Andi", "Pratama"), "street": "Jalan Tunjungan 45", "city": "Surabaya", "state": "JI", "postal": "60275"},
        {"name": ("Dewi", "Lestari"), "street": "Jalan Asia Afrika 133", "city": "Bandung", "state": "JB", "postal": "40112"},
        {"name": ("Rizky", "Hidayat"), "street": "Jalan Gatot Subroto 88", "city": "Medan", "state": "SU", "postal": "20112"},
    ),
    "IN": (
        {"name": ("Arjun", "Mehta"), "street": "12 Connaught Place", "city": "New Delhi", "state": "Delhi", "postal": "110001"},
        {"name": ("Priya", "Nair"), "street": "45 MG Road", "city": "Bengaluru", "state": "Karnataka", "postal": "560001"},
        {"name": ("Vikram", "Desai"), "street": "7 FC Road", "city": "Pune", "state": "Maharashtra", "postal": "411004"},
    ),
    "ES": (
        {"name": ("Sergio", "Martin"), "street": "Passeig de Gracia 43", "city": "Barcelona", "state": "CT", "postal": "08007"},
        {"name": ("Lucia", "Fernandez"), "street": "Plaza del Ayuntamiento 1", "city": "Valencia", "state": "VC", "postal": "46002"},
        {"name": ("Javier", "Romero"), "street": "Avenida de la Constitucion 20", "city": "Seville", "state": "AN", "postal": "41004"},
    ),
}

#: Runtime cursor filename for :func:`reserve_billing_variant`.
BILLING_VARIANT_STATE_FILENAME = "billing_address_cursor.json"


def billing_address_pool(country: str) -> tuple[dict, ...]:
    """Every curated billing identity for ``country``, the default one first.

    ``billing_address_pool(code)[0]`` is exactly ``BILLING_DATA[code]`` (or the
    generated template for an uncovered market), so index 0 is the pre-existing
    behaviour.
    """
    code = str(country or "DE").strip().upper() or "DE"
    base = BILLING_DATA.get(code) or _generated_billing(code)
    return (base, *BILLING_ADDRESS_POOLS.get(code, ()))


def _billing_variant_state_path() -> Path:
    """Resolve the cursor through ``paths`` so the test sandbox re-roots it.

    Binding the resolved path at import time would freeze it to the real
    ``runtime/`` and defeat the ``isolated_runtime`` fixture.
    """
    return runtime_file({}, BILLING_VARIANT_STATE_FILENAME)


def reserve_billing_variant(country: str, *, state_path: Any = None) -> int:
    """Advance ``country``'s cursor and return the next billing variant index.

    Best-effort by design. This only decorrelates one run from the previous one,
    so a missing or unwritable runtime directory must degrade to the default
    identity rather than fail a Checkout: **every** failure path returns ``0``.

    The read-modify-write is serialised by the shared cross-process file lock so
    two concurrent runs (CLI + workbench) cannot reserve the same variant. The
    cursor is advanced before the caller uses the index, so a run that later
    fails still consumes its slot -- a skipped address is the cheap direction to
    be wrong in, whereas a repeated one is the pattern this exists to break.

    Returns ``0`` for a single-entry pool (nothing to rotate).
    """
    code = str(country or "DE").strip().upper() or "DE"
    pool = billing_address_pool(code)
    if len(pool) <= 1:
        return 0
    path = Path(state_path) if state_path is not None else _billing_variant_state_path()
    try:
        with cross_process_write_lock(path.parent / f"{path.name}.lock"):
            state: dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
            except (OSError, ValueError):
                state = {}
            try:
                previous = int(state.get(code, -1))
            except (TypeError, ValueError):
                previous = -1
            index = (previous + 1) % len(pool)
            state[code] = index
            temporary = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return index
    except Exception:
        # GateTimeoutError (another process holds the lock), OSError (read-only
        # runtime), JSON garbage, or anything else: the identity is advisory.
        return 0


# ─── 代理工具 ──────────────────────────────────────────────────────────────────


def normalize_proxy_template(template: str) -> str:
    """规范化代理模板，支持多种格式:
    - 标准: user:pass@host:port
    - 反转: host:port@user:pass
    - 冒号分隔: host:port:user:pass
    """
    proxy = str(template or "").strip()
    if not proxy:
        return proxy

    if "@" not in proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            if "." in host and port.isdigit():
                return normalize_proxy_url(f"{user}:{pwd}@{host}:{port}")
        return normalize_proxy_url(proxy)

    parts = proxy.split("@")
    if len(parts) != 2:
        return normalize_proxy_url(proxy)
    left, right = parts
    if re.match(r"^[a-zA-Z0-9.\-]+:\d+$", left) and "." in left.split(":")[0]:
        return normalize_proxy_url(f"{right}@{left}")
    return normalize_proxy_url(proxy)


def proxy_for_country_template(template: str, country: str) -> str:
    """Replace a proxy template's region with the requested country.

    Region rewriting is delegated to the single-authority
    :func:`phone_proxy.match_proxy_region` (→ ``proxy_entry.retarget_region``)
    so this helper inherits every provider template the canonical parser knows
    — ``region-XX`` (Cliproxy), ``geo-XX`` (9http), IPWO ``custom_zone_XX`` and
    the Kookeey ``BASE-CC-SESSION-TTL`` password shape.  It used to hard-code
    its own ``region-[A-Za-z]{2}`` rewrite, which silently ignored every other
    provider: the caller believed it had routed to the requested country while
    the egress region never changed.

    Two shapes stay local, because the canonical rewrite does not own them:

    * the Cliproxy ``-st-<state>-city-<city>`` sticky-routing suffix, which is
      dropped for every country except ``JP`` (elsewhere it pins the egress to
      a stale city);
    * the password-less ``user-XX`` fallback, reachable only when the
      credential carries no recognisable region token at all.
    """
    proxy = normalize_proxy_template(template)
    country = str(country or "").strip().upper()
    if not proxy or not country:
        return proxy
    userinfo, separator, host = proxy.rpartition("@")
    if not separator:
        return proxy

    rewritten = match_proxy_region(proxy, country)
    if rewritten == proxy:
        # No provider template matched.  The ``-XX`` fallback can only fire for
        # a password-less proxy: ``rpartition("@")`` leaves ``:password`` at the
        # end of *userinfo*, so ``-[A-Za-z]{2}$`` never matches a credentialed
        # one.  Returning the template untouched is the honest outcome there —
        # there is no region token to rewrite.
        replaced_user, count = re.subn(r"-[A-Za-z]{2}$", f"-{country}", userinfo)
        if count != 1:
            return proxy
        return normalize_proxy_url(f"{replaced_user}@{host}")

    if country != "JP":
        rewritten_user, _, rewritten_host = rewritten.rpartition("@")
        cleaned = re.sub(r"-st-[^-@]+-city-[^-@]+(?=-sid-)", "", rewritten_user, count=1)
        if cleaned != rewritten_user:
            return normalize_proxy_url(f"{cleaned}@{rewritten_host}")
    return rewritten


def rotate_proxy_session_id(proxy: str) -> str:
    """轮转代理会话标识（如果代理模板支持）。"""
    return rotate_stage_proxy_session(proxy)


# ─── BA / EC Token 提取 ──────────────────────────────────────────────────────


def is_paypal_ba_approve_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())


def extract_ba_token(url: str) -> str:
    marker = "ba_token="
    lower = url.lower()
    if marker not in lower:
        return ""
    start = lower.find(marker) + len(marker)
    end = len(url)
    for sep in ("&", "#", '"', "'", " "):
        pos = url.find(sep, start)
        if pos != -1:
            end = min(end, pos)
    return url[start:end]


PAYPAL_ORIGIN = "https://www.paypal.com"


def extract_paypal_approve_url(text: str) -> str:
    """Pull the PayPal approve URL out of arbitrary page/JSON text.

    Single owner of approve-URL extraction (canonical form returned when only
    a ``ba_token`` is present); ``paypal_protocol`` and the reconciliation
    path consume this instead of re-parsing.
    """
    body = str(text or "").replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")
    match = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+", body)
    if match:
        return match.group(0)
    match = re.search(r"ba_token=(BA-[A-Za-z0-9_.-]+)", body)
    if match:
        return f"{PAYPAL_ORIGIN}/agreements/approve?ba_token={quote(match.group(1), safe='')}"
    return ""


def find_url_in_value(value: Any, patterns: list[re.Pattern]) -> str:
    """Recursively search *value* (dict/str/list) for the first URL match."""
    if isinstance(value, str):
        for pat in patterns:
            m = pat.search(value)
            if m:
                return m.group(0)
        return ""
    if isinstance(value, dict):
        for key in ("url", "redirect_url", "return_url"):
            if key in value:
                found = find_url_in_value(value[key], patterns)
                if found:
                    return found
        for v in value.values():
            found = find_url_in_value(v, patterns)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_url_in_value(item, patterns)
            if found:
                return found
    return ""


def extract_redirect_url(payload: dict) -> str:
    next_action = payload.get("next_action") or {}
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect = next_action.get("redirect_to_url") or {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    url = find_url_in_value(payload, [PM_REDIRECT_RE, PAYPAL_BA_RE])
    if url:
        return url
    for intent_key in ("setup_intent", "payment_intent"):
        intent = payload.get(intent_key) or {}
        action = intent.get("next_action") if isinstance(intent, dict) else {}
        redirect = action.get("redirect_to_url") if isinstance(action, dict) else {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    return ""


# ─── 重定向追踪 ────────────────────────────────────────────────────────────────


def resolve_external_redirect(
    session: Any,
    redirect_url: str,
    max_hops: int = 5,
) -> str:
    """Follow redirects until a PayPal approval URL or terminal location."""
    current = redirect_url
    for _ in range(max_hops):
        if not current:
            return ""
        if is_paypal_ba_approve_url(current):
            return current
        try:
            resp = session.get(current, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        except Exception:
            return current
        if resp.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(resp.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


# ─── Stripe / 金额 ─────────────────────────────────────────────────────────────


def _generated_billing(country: str) -> dict:
    """Placeholder template for a country with no curated entry.

    It deliberately keeps the **requested** country instead of reusing Germany.
    A German billing address on a non-German Checkout is a country/currency
    mismatch, and that silent default was applied to every uncovered market.
    """
    code = str(country or "").strip().upper() or "DE"
    return {
        "name": ("John", "Doe"),
        "street": f"1 {code} Main Street",
        "city": code,
        "state": code,
        "postal": "00000",
    }


def billing_for_country(country: str, *, variant: int | None = None) -> dict:
    """The billing identity for ``country``.

    ``variant=None`` returns the country's default identity, which is what every
    non-reserving caller wants: the two billing lookups inside one PayPal run are
    separate stages of one checkout, so a rotating default here would let
    ``_sync_tax_region`` and ``_create_payment_method`` bill from different
    streets. Pass the index from :func:`reserve_billing_variant` to decorrelate
    that run from the previous one while keeping the run internally consistent.
    """
    code = str(country or "DE").upper()
    pool = billing_address_pool(code)
    data = pool[0] if variant is None else pool[int(variant) % len(pool)]
    return {
        "country": code,
        "name": data["name"],
        "email": f"buyer{uuid.uuid4().int % 9000 + 1000}@example.{code.lower()}",
        "street": data["street"],
        "city": data["city"],
        "state": data["state"],
        "postal": data["postal"],
    }


def stripe_amount_details(init_payload: dict) -> dict:
    if not isinstance(init_payload, dict):
        return {"amount": None, "currency": "", "source": "unknown"}
    currency = str(init_payload.get("currency") or "").lower()
    total_summary = init_payload.get("total_summary") or {}
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return {
            "amount": int(total_summary["due"]),
            "currency": str(total_summary.get("currency") or currency).lower(),
            "source": "total_summary.due",
        }
    invoice = init_payload.get("invoice") or {}
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return {
            "amount": int(invoice["amount_due"]),
            "currency": str(invoice.get("currency") or currency).lower(),
            "source": "invoice.amount_due",
        }
    return {"amount": None, "currency": currency, "source": "unknown"}


# ─── 诊断 ─────────────────────────────────────────────────────────────────────


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    direct = payload.get("submission_attempt")
    if isinstance(direct, dict):
        return direct
    for value in payload.values():
        if isinstance(value, dict):
            found = find_submission_attempt(value)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                found = find_submission_attempt(item)
                if found:
                    return found
    return {}


def stripe_confirm_error_diagnostics(
    response: Any,
    cs_id: str,
    pm_id: str,
    init_payload: dict,
) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {}
    submission = find_submission_attempt(payload)
    parts = [
        f"stripe_confirm_failed:http={getattr(response, 'status_code', 0)}",
        f"cs_id={str(cs_id)[:18]}",
        f"pm_id={str(pm_id)[:18]}",
        f"amount={stripe_amount_details(init_payload).get('amount')}",
        f"init_checksum={'present' if init_payload.get('init_checksum') else 'missing'}",
    ]
    for label, value in (
        ("error_type", error.get("type")),
        ("error_code", error.get("code")),
        ("error_param", error.get("param")),
        ("error_message", error.get("message")),
        ("submission_state", submission.get("state")),
        ("submission_reason", submission.get("reason")),
        ("submission_code", submission.get("code")),
        ("submission_message", submission.get("message")),
    ):
        if value not in (None, ""):
            compact = re.sub(r"\s+", " ", str(value)).strip()
            parts.append(f"{label}={compact[:180]}")
    if len(parts) == 5:
        body = re.sub(r"\s+", " ", str(getattr(response, "text", ""))).strip()
        parts.append(f"body={body[:180]}")
    return "; ".join(parts)

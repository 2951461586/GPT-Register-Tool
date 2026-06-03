#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import httpx

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except Exception:  # pragma: no cover - dependency is present on the current box.
    Cipher = algorithms = modes = default_backend = None

AUTH = "https://accounts.goto-products.com"
API = "https://api.gojekapi.com"
CUSTOMER = "https://customer.gopayapi.com"

APP_ID = "com.gojek.gopay"
APP_VERSION = "2.10.0"
APP_BUILD = "2100"
AUTH_ID = "gopay:consumer:app"
AUTH_SECRET = "raOUumeMRBNifqvZRFjvsgTnjAlaA9"
SIGNUP_CLIENT_NAME = "gopay_consumer_app"
# Signup body still uses the LoginSDK client secret in the closest live/OSINT
# implementation; the endpoint-level Authorization is a separate static Basic
# suffix, not base64(client:secret).
SIGNUP_CLIENT_SECRET = AUTH_SECRET
SIGNUP_BASIC_UUID = "bb648413-b637-443a-8ebf-176cf9b5dc32"
SIGNUP_BASIC_SUFFIX = base64.b64encode(SIGNUP_BASIC_UUID.encode("utf-8")).decode("ascii")
SIGNUP_XOR_SECRET_CANDIDATE = "09f5686f234a8cf023cef42089ba483ba8c66fc64d9a4b778f7ecf9f0976010e"
AUTHSDK_VERSION = "1.0.0"
CVSDK_VERSION = "1.0.0"
X_E2_DEFAULT = "ED9A2B38749FBDE9ACA61D6A685B7"
DISPLAY_ENCODER_SUPPORT_CODE_KEY = "F7qonjstEjipAjlZ9O1E16S5Oo2PEHLe"
# Runtime-recovered from the in-app enhanced DisplayEncoder path
# libbatteryOpt.so+0x733dc -> +0x76150 (HMAC-SHA256).
DISPLAY_ENCODER_ENHANCED_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
# Runtime-observed enhanced key for one logged-in paylater request. It proves
# where the enhanced key enters getAppCodec, but is request/session dependent
# and is therefore not used as the default for fresh signup.
DISPLAY_ENCODER_ENHANCED_KEY_OBSERVED = "1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC"


def minjson(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def new_device_id() -> str:
    # App-side X-UniqueId observed as 16 lower-case hex (Android per-package SSAID).
    return secrets.token_hex(8)


def new_d1() -> str:
    return ":".join(f"{b:02X}" for b in os.urandom(32))


def normalize_id_phone(phone: str) -> Tuple[str, str]:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("62"):
        return "+62", digits[2:]
    if digits.startswith("0"):
        return "+62", digits[1:]
    return "+62", digits


def normalize_proxy(proxy: Optional[str]) -> str:
    value = str(proxy or "").strip()
    if value.lower().startswith("socks5h://"):
        return "socks5://" + value[len("socks5h://"):]
    return value


def extract_path(base: str, path: str) -> Tuple[str, str]:
    u = urlparse(base)
    return u.netloc, path


def pick_first(obj: Any, names: Iterable[str]) -> Optional[Any]:
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] not in (None, ""):
                return obj[n]
        for v in obj.values():
            got = pick_first(v, names)
            if got not in (None, ""):
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = pick_first(v, names)
            if got not in (None, ""):
                return got
    return None


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    n = block_size - (len(data) % block_size)
    return data + bytes([n]) * n


def tokenize_pin_aes_ecb(pin: str, pin_token: str) -> str:
    """Current pure-Python candidate for Dart `tokenizePin`.

    Static Android evidence:
    `getNavigationItemCount.smali` exposes MethodChannel `help_center_sdk`
    method `encrypt(textToEncrypt, encryptionKey)`, backed by
    `com.gojek.gopay.CipherUtil.encrypt`. It repeats the key until it has at
    least 16 chars, takes the first 16 chars, then uses Java
    `Cipher.getInstance("AES")`; on Android this is AES/ECB/PKCS5Padding.

    The GoPay Dart AOT strings place `tokenizePin`, `pin_token`, and
    `encryptionKey` in the same PIN flow area, so this is wired as the default
    tokenizer while X-E1 is being finished. If runtime later disproves it,
    callers can still override with `--tokenized-pin` or
    `--pin-tokenizer-cmd`.
    """
    if not pin or not re.fullmatch(r"\d{4,8}", pin):
        raise ValueError("PIN must be 4-8 digits")
    if not pin_token:
        raise ValueError("empty pin_token")
    if Cipher is None:
        raise RuntimeError("cryptography package is required for AES PIN tokenization")
    key = (pin_token * ((16 + len(pin_token) - 1) // len(pin_token)))[:16].encode("utf-8")
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    out = enc.update(pkcs7_pad(pin.encode("utf-8"), 16)) + enc.finalize()
    return base64.b64encode(out).decode("ascii")


@dataclass
class DeviceProfile:
    unique_id: str
    x_m1: str
    d1: str = ""
    x_e2: str = X_E2_DEFAULT
    x_session_id: str = ""
    adjts: str = "host:D"
    phone_make: str = "Xiaomi"
    phone_model: str = "Xiaomi, 23117RK66C"
    device_os: str = "Android, 15"
    app_id: str = APP_ID
    app_version: str = APP_VERSION
    app_build: str = APP_BUILD

    def __post_init__(self) -> None:
        if not self.d1:
            self.d1 = new_d1()
        if not self.x_session_id:
            self.x_session_id = str(uuid.uuid4())

    @classmethod
    def default(cls, unique_id: Optional[str] = None, x_m1: Optional[str] = None) -> "DeviceProfile":
        # Runtime X-M1 is part of the device-rate-limit surface. Generate a
        # fresh tuple in the same shape as the mobile wrapper.
        uid = unique_id or new_device_id()
        # Keep the shape close to the runtime captures.  A more "rich" M1
        # tuple with location providers/wifi MACs reached CVS but triggered
        # init_verification rate limits more often in live runs; the captured
        # app tuple used wifi-off placeholders and a stable-looking Widevine
        # marker, so randomise only the per-device parts.
        widevine = "RkFLRV9XVk5f" + time.strftime("%Y%m%d") + "_" + secrets.token_hex(2).upper()
        fp15 = secrets.token_hex(16)
        dev_uuid = str(uuid.uuid4())
        make = "Xiaomi"
        model = "Xiaomi, 23117RK66C"
        chipset = "pineapple"
        return cls(
            unique_id=uid,
            d1=new_d1(),
            x_m1=x_m1
            or (
                f"3:{int(time.time()*1000)}-{secrets.randbelow(10**19)},"
                f"4:{secrets.randbelow(900000)+100000},"
                f"5:{chipset}|2265|8,"
                "6:<wifi is turned off>,"
                "7:<wifi is turned off>,"
                "8:1080x2400,"
                "10:0,"
                f"11:{widevine},"
                f"15:{fp15},"
                f"16:{dev_uuid}"
            ),
            phone_make=make,
            phone_model=model,
        )

    @classmethod
    def from_capture(cls, artifact_path: str | os.PathLike[str], index: int = 0) -> "DeviceProfile":
        data = json.loads(pathlib.Path(artifact_path).read_text(encoding="utf-8"))
        row = data[index] if isinstance(data, list) else (data.get("items") or data.get("artifacts"))[index]
        return cls(
            unique_id=row["x_uniqueid"],
            x_m1=row["x_m1"],
            d1=row.get("d1") or row.get("D1") or new_d1(),
            x_e2=row.get("x_e2") or X_E2_DEFAULT,
            x_session_id=row.get("x_session_id") or row.get("X-Session-ID") or "",
            adjts=row.get("adjts") or row.get("AdjTs") or "host:D",
            phone_model=row.get("phone_model") or "Xiaomi, 23117RK66C",
            phone_make=(row.get("phone_model") or "Xiaomi").split(",")[0],
            device_os=row.get("device_os") or "Android, 15",
            app_id=row.get("app_id") or APP_ID,
            app_version=row.get("app_version") or APP_VERSION,
        )


class XESigner:
    name = "none"

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        raise NotImplementedError


class NullSigner(XESigner):
    name = "none"

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        return ""


class CapturedSigner(XESigner):
    """Replay-only signer.

    It returns an already captured X-E1 only when method/host/path/body and
    X-UniqueId match byte-for-byte. It is useful for regression and for proving
    the HTTP layer, but cannot sign a new phone/body.
    """

    name = "captured"

    def __init__(self, artifact_path: str | os.PathLike[str]):
        data = json.loads(pathlib.Path(artifact_path).read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else (data.get("items") or data.get("artifacts") or [])
        self.rows = rows

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        hp = host + path
        uid = headers.get("x-uniqueid") or headers.get("X-UniqueId")
        for row in self.rows:
            if (
                row.get("method", "").upper() == method.upper()
                and row.get("host_path") == hp
                and row.get("body_text") == body_text
                and row.get("x_uniqueid") == uid
            ):
                return row["x_e1"]
        raise RuntimeError(f"no captured X-E1 for {method} {hp} uid={uid} body={body_text[:160]}")


class AdbOracleSigner(XESigner):
    """Call the current /data/local/tmp app_process oracle.

    This does not drive the GoPay UI. Current local evidence shows this oracle
    produces a syntactically valid fallback X-E1, but server rejects it with
    GoPay-1000 for fresh bodies unless the enhanced DisplayEncoder state is
    solved. The runner keeps this mode because it is the clean swappable seam.
    """

    name = "adb-oracle"

    def __init__(
        self,
        adb: str = "./adb/adb",
        oracle_dex: str = "/data/local/tmp/oracle.dex",
        libbattery: str = "/data/local/tmp/libbatteryOpt.so",
        liboracle: str = "/data/local/tmp/liboracle.so",
        timeout: int = 25,
    ):
        self.adb = adb
        self.oracle_dex = oracle_dex
        self.libbattery = libbattery
        self.liboracle = liboracle
        self.timeout = timeout

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        ts_s = str(ts or int(time.time() * 1000))
        support = [
            headers.get("authorization", ""),
            ts_s,
            host + path,
            method.upper(),
            body_text or "",
            headers.get("d1", ""),
            headers.get("x-phonemodel", ""),
            headers.get("x-m1", ""),
            headers.get("x-deviceos", ""),
            headers.get("x-appid", ""),
            headers.get("x-appversion", ""),
            "D",
            headers.get("x-uniqueid", ""),
        ]
        raw = "\x1f".join(support)
        b64 = base64.b64encode(raw.encode()).decode()
        cmd = (
            f"CLASSPATH={self.oracle_dex} app_process64 /system/bin OracleMain "
            f"{self.libbattery} {self.liboracle} --b64 {b64}"
        )
        p = subprocess.run([self.adb, "shell", cmd], text=True, capture_output=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(f"oracle failed rc={p.returncode}\nSTDOUT={p.stdout}\nSTDERR={p.stderr}")
        first = p.stdout.strip().splitlines()[0]
        try:
            obj = json.loads(first)
            return obj["ext"]
        except Exception:
            return first.strip()


class PurePythonXESigner(XESigner):
    """Pure Python implementation of libbatteryOpt.so getAppCodec fallback.

    Static/runtime proof points:
    - libbatteryOpt.so+0x82774 is HMAC-SHA256(key, data) with 32-byte output.
    - libbatteryOpt.so+0x815e8 is MD5-to-lower-hex.
    - libbatteryOpt.so+0x6e944 assembles X-E1 as
      hmac_hex + ':' + 80-byte-random-hex + ':' + supportEducation + ':' + ts.
    - Direct app_process oracle call to 0x6e944 with key
      DISPLAY_ENCODER_SUPPORT_CODE_KEY matched this exact HMAC message order.

    The real in-app enhanced DisplayEncoder path can substitute a different
    resolution key. Keep `resolution_key` configurable so the protocol runner
    remains adb-free once that key is supplied/recovered.
    """

    name = "pure"

    def __init__(self, resolution_key: str = DISPLAY_ENCODER_SUPPORT_CODE_KEY, random_hex: Optional[str] = None):
        if not resolution_key:
            raise ValueError("empty X-E1 resolution key")
        self.resolution_key = resolution_key
        self.random_hex = random_hex

    @staticmethod
    def body_md5(body_text: str) -> str:
        return hashlib.md5((body_text or "").encode("utf-8")).hexdigest()

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        ts_s = str(ts or int(time.time() * 1000))
        auth = headers.get("authorization", "") or ""
        if auth.startswith("Bearer "):
            # Native strips the literal prefix before using supportPulsa.
            auth = auth[len("Bearer ") :]
        rand_hex = self.random_hex or secrets.token_hex(80)
        if not re.fullmatch(r"[0-9a-fA-F]{160}", rand_hex):
            raise ValueError("X-E1 random hex must be exactly 160 hex chars")
        rand_hex = rand_hex.lower()
        support = {
            "supportPulsa": auth,
            "supportDataPackage": ts_s,
            "supportPlnToken": host + path,
            "supportEMoney": method.upper(),
            "supportPln": body_text or "",
            "supportPdam": headers.get("d1", "") or "",
            "supportBpjs": headers.get("x-phonemodel", ""),
            "supportInternetCable": headers.get("x-m1", ""),
            "supportPhonePostPaid": headers.get("x-deviceos", ""),
            "supportMultifinance": headers.get("x-appid", ""),
            "supportEInvoicing": headers.get("x-appversion", ""),
            "supportEducation": "D",
            "supportInsurance": headers.get("x-uniqueid", ""),
        }
        # Exact concatenation order from libbatteryOpt.so+0x6eb8c..0x6f1bc.
        msg = ":".join(
            [
                support["supportPulsa"],
                support["supportBpjs"],
                support["supportInternetCable"],
                support["supportEInvoicing"],
                self.body_md5(support["supportPln"]),
                support["supportInsurance"],
                support["supportEMoney"],
                support["supportPhonePostPaid"],
                support["supportDataPackage"],
                support["supportPdam"],
                support["supportPlnToken"],
                support["supportMultifinance"],
                rand_hex,
            ]
        )
        sig = hmac.new(self.resolution_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{sig}:{rand_hex}:D:{ts_s}"


class EnhancedPythonXESigner(XESigner):
    """Pure Python implementation of the in-app enhanced X-E1 path.

    Runtime proof:
    `runtime_now/trace_xe1_core_20260530_012608.stdout` captured
    libbatteryOpt.so+0x733dc immediately before +0x76150:

    - key: ``DISPLAY_ENCODER_ENHANCED_KEY``
    - message:
      ``GOPAY;{model}:{auth};{uid}:{d1};{md5(body)}:{host_path};...``
    - final:
      ``hmac_sha256_hex(message):random_80_bytes_hex:D:timestamp``

    This is the signer used by the live app for
    ``/v1/support/customer/initiate`` and the same field construction is used
    for the rest of the protocol calls.
    """

    name = "enhanced"

    def __init__(self, resolution_key: str = DISPLAY_ENCODER_ENHANCED_KEY, random_hex: Optional[str] = None):
        if not resolution_key:
            raise ValueError("empty enhanced X-E1 key")
        self.resolution_key = resolution_key
        self.random_hex = random_hex

    @staticmethod
    def body_md5(body_text: str) -> str:
        return hashlib.md5((body_text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _auth_value(headers: Dict[str, str]) -> str:
        auth = headers.get("authorization", "") or headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            auth = auth[len("Bearer ") :]
        return auth

    @staticmethod
    def _phone_make(headers: Dict[str, str]) -> str:
        make = headers.get("x-phonemake", "") or headers.get("X-PhoneMake", "") or ""
        if make:
            return make
        model = headers.get("x-phonemodel", "") or headers.get("X-PhoneModel", "") or ""
        return model.split(",", 1)[0].strip() if model else ""

    @staticmethod
    def _os_family(headers: Dict[str, str]) -> str:
        os_name = headers.get("x-deviceos", "") or headers.get("X-DeviceOs", "") or ""
        return os_name.split(",", 1)[0].strip() if os_name else ""

    def build_message(
        self,
        method: str,
        host: str,
        path: str,
        body_text: str,
        headers: Dict[str, str],
        ts_s: str,
        rand_hex: str,
    ) -> str:
        fields = [
            "GOPAY",
            headers.get("x-phonemodel", "") or headers.get("X-PhoneModel", ""),
            self._auth_value(headers),
            headers.get("x-uniqueid", "") or headers.get("X-UniqueId", ""),
            headers.get("d1", "") or headers.get("D1", ""),
            self.body_md5(body_text),
            host + path,
            method.upper(),
            ts_s,
            headers.get("x-deviceos", "") or headers.get("X-DeviceOs", ""),
            headers.get("x-appversion", "") or headers.get("X-AppVersion", ""),
            headers.get("x-m1", "") or headers.get("X-M1", ""),
            headers.get("x-appid", "") or headers.get("X-AppId", ""),
            rand_hex,
            self._phone_make(headers),
            self._os_family(headers),
        ]
        out = []
        for i, value in enumerate(fields):
            out.append(value or "")
            if i != len(fields) - 1:
                out.append(";" if i % 2 == 0 else ":")
        return "".join(out)

    def sign(self, method: str, host: str, path: str, body_text: str, headers: Dict[str, str], ts: Optional[int] = None) -> str:
        ts_s = str(ts or int(time.time() * 1000))
        rand_hex = self.random_hex or secrets.token_hex(80)
        if not re.fullmatch(r"[0-9a-fA-F]{160}", rand_hex):
            raise ValueError("enhanced X-E1 random hex must be exactly 160 hex chars")
        rand_hex = rand_hex.lower()
        msg = self.build_message(method, host, path, body_text or "", headers, ts_s, rand_hex)
        sig = hmac.new(self.resolution_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{sig}:{rand_hex}:D:{ts_s}"


class GoPayProtocol:
    def __init__(
        self,
        device: DeviceProfile,
        signer: Optional[XESigner] = None,
        client_id: str = AUTH_ID,
        client_secret: str = AUTH_SECRET,
        debug: bool = True,
        dry_run: bool = False,
        proxy: Optional[str] = None,
        timeout: int = 35,
    ):
        self.device = device
        self.signer = signer or NullSigner()
        self.client_id = client_id
        self.client_secret = client_secret
        self.debug = debug
        self.dry_run = dry_run
        self.proxy = normalize_proxy(proxy)
        try:
            self.c = httpx.Client(timeout=timeout, http2=True, follow_redirects=False, trust_env=False, proxy=self.proxy or None)
        except ImportError as exc:
            if "http2" not in str(exc).lower():
                raise
            self.c = httpx.Client(timeout=timeout, http2=False, follow_redirects=False, trust_env=False, proxy=self.proxy or None)

    def close(self) -> None:
        self.c.close()

    def headers(self, auth: Optional[str] = None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {
            "accept": "application/json",
            "accept-language": "id-ID",
            "accept-encoding": "gzip",
            "authorization": f"Bearer {auth}" if auth else "",
            "content-type": "application/json",
            "country-code": "ID",
            "gojek-country-code": "ID",
            "gojek-service-area": "1",
            "gojek-timezone": "Asia/Jakarta",
            "d1": self.device.d1,
            "transaction-id": str(uuid.uuid4()),
            "user-agent": f"GoPay/{self.device.app_version} ({self.device.app_id}; build:{self.device.app_build}; Android, 15)",
            "x-appid": self.device.app_id,
            "x-apptype": "GOPAY",
            "x-appversion": self.device.app_version,
            "x-authsdk-version": AUTHSDK_VERSION,
            "x-deviceos": self.device.device_os,
            "x-devicetoken": "",
            "x-e2": self.device.x_e2,
            "x-help-version": self.device.app_version,
            "x-m1": self.device.x_m1,
            "x-phonemake": self.device.phone_make,
            "x-phonemodel": self.device.phone_model,
            "x-platform": "Android",
            "x-request-id": str(uuid.uuid4()),
            "x-uniqueid": self.device.unique_id,
            "x-user-locale": "id_ID",
            "x-user-type": "customer",
        }
        if extra:
            h.update({k.lower(): v for k, v in extra.items()})
        return h

    def _send(self, method: str, base: str, path: str, body: Optional[Dict[str, Any]], auth: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None, sign_ts: Optional[int] = None, sign_path: Optional[str] = None, body_text_override: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        host, pth = extract_path(base, sign_path or path)
        body_text = body_text_override if body_text_override is not None else ("" if body is None else minjson(body))
        h = self.headers(auth=auth, extra=extra_headers)
        # The accounts stack in the Android wrapper sends these on all
        # goto-auth/CVS requests, not only `/cvs/*`.  `/goto-auth/token` is
        # particularly sensitive to the same low-level fingerprint header set.
        if base == AUTH:
            h["x-cvsdk-version"] = CVSDK_VERSION
        h.setdefault("x-e3", hashlib.md5(body_text.encode("utf-8")).hexdigest())
        h.setdefault("x-session-id", self.device.x_session_id)
        h.setdefault("adjts", self.device.adjts or "host:D")
        xe1 = self.signer.sign(method, host, pth, body_text, h, ts=sign_ts)
        if xe1:
            h["x-e1"] = xe1
        if self.debug:
            print(f"\n>>> {method.upper()} {base}{path}")
            if sign_path and sign_path != path:
                print(f">>> sign-path {host}{sign_path}")
            print(">>> signer", self.signer.name)
            print(">>> headers", json.dumps({k: h[k] for k in sorted(h) if k in {
                "authorization", "transaction-id", "x-request-id", "x-uniqueid", "x-m1", "x-e1", "x-e2",
                "x-e3", "x-session-id", "adjts", "x-cvsdk-version", "x-appversion", "x-authsdk-version", "x-phonemodel", "x-deviceos",
            }}, ensure_ascii=False, indent=2))
            print(">>> body", body_text)
        if self.dry_run:
            return 0, {"dry_run": True, "url": base + path, "headers": h, "body": body_text}, {}
        r = self.c.request(method, base + path, headers=h, content=body_text.encode() if body_text else b"")
        if self.debug:
            print("<<<", r.status_code, r.headers.get("content-type"))
            print(r.text[:5000])
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return r.status_code, data, dict(r.headers)

    def post(self, base: str, path: str, body: Dict[str, Any], auth: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None, sign_path: Optional[str] = None, body_text_override: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        return self._send("POST", base, path, body, auth=auth, extra_headers=extra_headers, sign_path=sign_path, body_text_override=body_text_override)

    def get(self, base: str, path: str, auth: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None, sign_path: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        return self._send("GET", base, path, None, auth=auth, extra_headers=extra_headers, sign_path=sign_path)

    def login_methods(self, phone_local: str, country_code: str = "+62") -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "phone_number": phone_local,
            "country_code": country_code,
            "email": "",
            "device_verification_token_id": "",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        return self.post(AUTH, "/goto-auth/login/methods", body)

    def cvs_methods(self, phone_local: str, flow: str = "signup", country_code: str = "+62", device_verification_token_id: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "country_code": country_code,
            "email_address": None,
            "client_id": self.client_id,
            "phone_number": phone_local,
            "client_secret": self.client_secret,
            "flow": flow,
            "device_verification_token_id": device_verification_token_id,
        }
        return self.post(AUTH, "/cvs/v1/methods", body)

    def cvs_initiate(self, phone_local: str, verification_id: str, method: str = "otp_sms", flow: str = "signup", country_code: str = "+62", device_verification_token_id: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "verification_id": verification_id,
            "flow": flow,
            "verification_method": method,
            "country_code": country_code,
            "email_address": None,
            "client_id": self.client_id,
            "phone_number": phone_local,
            "client_secret": self.client_secret,
            "is_multiple_method": None,
            "device_verification_token_id": device_verification_token_id,
        }
        # Current edge behavior blocks the literal path at Tencent WAF before
        # the request reaches CVS, while the backend normalizes a doubled slash.
        # Sign the canonical path (what the app signs / what CVS validates) but
        # send `/cvs/v1//initiate` through the edge.  With an invalid
        # verification_id this reaches the business layer as
        # `scp-cvs:error:validation:verification_id_invalid`, proving the
        # bypass without relying on adb/Frida/App runtime.
        return self.post(AUTH, "/cvs/v1//initiate", body, extra_headers={"key": "value"}, sign_path="/cvs/v1/initiate")

    def cvs_retry(self, otp_token: str, method: str = "otp_sms", flow: str = "signup") -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT exact body at libapp.so+0x1246e14 with nested data
        # toJson at libapp.so+0x1246ed8:
        #   {client_id, client_secret, flow, verification_method,
        #    data: {otp_token}}
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "flow": flow,
            "verification_method": method,
            "data": {"otp_token": otp_token},
        }
        return self.post(AUTH, "/cvs/v1/retry", body)

    def cvs_verify(self, phone_local: str, verification_id: str, otp: str, method: str = "otp_sms", flow: str = "signup", country_code: str = "+62", device_verification_token_id: Optional[str] = None, otp_token: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        # Recovered from Flutter AOT (libapp.so):
        #   map keys = client_id, client_secret, flow, verification_method,
        #              verification_id, data
        # For OTP methods `data` is a nested map: {"otp": code,
        # "otp_token": token_from_initiate}. Top-level otp/code fields make
        # the server answer scp-cvs:error:missing_field; a plain-string data
        # value returns scp-cvs:error:malformed_request.
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "flow": flow,
            "verification_method": method,
            "verification_id": verification_id,
            "data": {"otp": otp, "otp_token": otp_token or ""},
        }
        return self.post(AUTH, "/cvs/v1/verify", body)

    def customer_signup(
        self,
        phone_local: str,
        full_name: str,
        country_code: str = "+62",
        verification_token: Optional[str] = None,
        signup_client_name: str = SIGNUP_CLIENT_NAME,
        signup_client_secret: str = SIGNUP_CLIENT_SECRET,
        signup_basic: Optional[str] = None,
        signed_up_country: str = "62",
        email: str = "",
        escape_client_name_colon: bool = False,
    ) -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT exact shape:
        #   wrapper:     libapp.so+0x24c3604
        #   body toJson: libapp.so+0x159f034
        #   data toJson: libapp.so+0x159f0c8
        #   headers: Verification-Token: Bearer <token>,
        #            Authorization: Basic <eZh._lzs auth suffix>
        #
        # Important: the old implementation incorrectly sent
        # phone_number/country_code/full_name/client_id at the top level.  The
        # app sends only {client_name, client_secret, data:{...}}.
        phone_full = re.sub(r"\D", "", phone_local)
        if country_code == "+62" and not phone_full.startswith("62"):
            phone_full = "62" + phone_full
        body = {
            "client_name": signup_client_name,
            "client_secret": signup_client_secret,
            "data": {
                "name": full_name,
                "phone": phone_full,
                "email": email,
                "signed_up_country": signed_up_country,
                "onboarding_partner": SIGNUP_CLIENT_NAME,
            },
        }
        if signup_basic is None:
            # AOT concatenates "Basic " with an injected suffix.  OSINT for
            # the same endpoint recovered this as base64(UUID), not
            # base64(client_name:client_secret).
            signup_basic = SIGNUP_BASIC_SUFFIX
        extra: Dict[str, str] = {"authorization": f"Basic {signup_basic}"}
        if verification_token:
            extra["verification-token"] = f"Bearer {verification_token}"
        # Runtime path routing shows api.gojekapi.com reaches the business
        # layer; customer.gopayapi.com returns Kong/ESA 404 for this path.
        #
        # Edge/WAF behavior differs from the app's canonical request path:
        # literal `/v7/customers/signup` is commonly stopped by Tencent WAF,
        # while the backend normalizes a doubled slash and reaches the customer
        # service.  Keep the X-E1 canonical path equal to the AOT literal, but
        # send `/v7//customers/signup` through the edge, mirroring the proven
        # `/cvs/v1//initiate` bypass pattern.
        body_text_override = None
        if escape_client_name_colon and ":" in signup_client_name:
            # Tencent WAF false-positives on the literal colon-bearing client
            # name, while JSON unicode escapes are decoded to the same string
            # by the backend. Sign exactly what is sent on the wire.
            body_text_override = minjson(body).replace("gopay:consumer:app", "gopay\\u003aconsumer\\u003aapp")
        return self.post(
            API,
            "/v7//customers/signup",
            body,
            extra_headers=extra,
            sign_path="/v7/customers/signup",
            body_text_override=body_text_override,
        )

    def pin_allowed(self, access_token: str, pin: str) -> Tuple[int, Any, Dict[str, str]]:
        return self.post(CUSTOMER, "/api/v1/users/pins/allowed", {"pin": pin}, auth=access_token)

    def user_profile(self, access_token: str) -> Tuple[int, Any, Dict[str, str]]:
        return self.get(CUSTOMER, "/v1/users/profile", auth=access_token)

    def cvs_methods_pin(self, access_token: str) -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "country_code": None,
            "email_address": None,
            "client_id": self.client_id,
            "phone_number": None,
            "client_secret": self.client_secret,
            "flow": "goto_pin_wa_sms",
            "device_verification_token_id": None,
        }
        return self.post(AUTH, "/cvs/v1/methods", body, auth=access_token)

    def cvs_initiate_pin(self, access_token: str, verification_id: str, method: str = "otp_sms") -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "verification_id": verification_id,
            "flow": "goto_pin_wa_sms",
            "verification_method": method,
            "country_code": None,
            "email_address": None,
            "client_id": self.client_id,
            "phone_number": None,
            "client_secret": self.client_secret,
            "is_multiple_method": None,
            "device_verification_token_id": None,
        }
        return self.post(AUTH, "/cvs/v1//initiate", body, auth=access_token, extra_headers={"key": "value"}, sign_path="/cvs/v1/initiate")

    def cvs_retry_pin(self, access_token: str, otp_token: str, method: str = "otp_sms") -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "flow": "goto_pin_wa_sms",
            "verification_method": method,
            "data": {"otp_token": otp_token},
        }
        return self.post(AUTH, "/cvs/v1/retry", body, auth=access_token)

    def cvs_verify_pin(self, access_token: str, verification_id: str, otp: str, otp_token: str, method: str = "otp_sms") -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "flow": "goto_pin_wa_sms",
            "verification_method": method,
            "verification_id": verification_id,
            "data": {"otp": otp, "otp_token": otp_token},
        }
        return self.post(AUTH, "/cvs/v1/verify", body, auth=access_token)

    def accountlist(self, verification_token: str) -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT request wrapper at libapp.so+0x241a274:
        #   POST /goto-auth/accountlist
        #   headers: Transaction-ID, Verification-Token
        #   body toJson at libapp.so+0x12697e4: {client_id, client_secret}
        # A stale token returns HTTP 401 {"message":"Invalid/Expired token"},
        # confirming this is the post-CVS step that resolves numeric account_id
        # before /goto-auth/token.
        body = {"client_id": self.client_id, "client_secret": self.client_secret}
        return self.post(AUTH, "/goto-auth/accountlist", body, extra_headers={"Verification-Token": f"Bearer {verification_token}"})

    def device_verification_login(self, phone_local: str, verification_id: str, otp: str, country_code: str = "+62", method: str = "otp_sms") -> Tuple[int, Any, Dict[str, str]]:
        body = {
            "phone_number": phone_local,
            "country_code": country_code,
            "verification_id": verification_id,
            "challenge_id": verification_id,
            "otp": otp,
            "code": otp,
            "method": method,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_device_id": self.device.unique_id,
        }
        return self.post(AUTH, "/goto-auth/device-verification/login", body)

    def token(self, *, verification_token: Optional[str] = None, authorization_code: Optional[str] = None, refresh_token: Optional[str] = None, account_id: str = "", ext_user_token: Optional[str] = None) -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT exact toJson at libapp.so+0xbbdbe4:
        #   {grant_type, account_id, token, client_id, client_secret, ext_user_token}
        # `grant_type` is an enum->string map, not "verification_token" /
        # "authorization_code".  For CVS OTP it is "cvs"; for auth-code
        # exchange it is "auth_code"; refresh remains "refresh_token".
        body: Dict[str, Any] = {
            "grant_type": "",
            "account_id": account_id,
            "token": "",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "ext_user_token": ext_user_token,
        }
        if verification_token:
            body.update({"grant_type": "cvs", "token": verification_token})
        elif authorization_code:
            body.update({"grant_type": "auth_code", "token": authorization_code})
        elif refresh_token:
            body.update({"grant_type": "refresh_token", "token": refresh_token})
        else:
            raise ValueError("need verification_token / authorization_code / refresh_token")
        return self.post(AUTH, "/goto-auth/token", body)

    def pin_setup_token(self, access_token: str, pin: str, challenge_id: str = "", verification_token: str = "") -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT exact body at libapp.so+0xcd0314:
        #   {client_id, pin, challenge_id}
        # The surrounding request builder also sends static/nullable headers:
        #   is-token-required, otp_auth_token, Verification-Token.
        body = {"client_id": self.client_id, "pin": pin, "challenge_id": challenge_id}
        extra = {"is-token-required": "true"}
        if verification_token:
            extra["verification-token"] = verification_token
        return self.post(API, "/api/v2/users/pins/setup/tokens", body, auth=access_token, extra_headers=extra)

    def pin_setup_token_after_otp(self, access_token: str, pin: str, verification_token: str, challenge_id: str = "", client_id: str = "") -> Tuple[int, Any, Dict[str, str]]:
        # Signup PIN setup path from the recovered reference flow:
        #   POST customer.gopayapi.com/api/v2/users/pins/setup/tokens
        #   body {"client_id":"","pin":pin,"challenge_id":""}
        #   headers Verification-Token: Bearer <goto_pin_wa_sms token>,
        #           Is-Token-Required: false
        body = {"client_id": client_id, "pin": pin, "challenge_id": challenge_id}
        extra = {"verification-token": f"Bearer {verification_token}", "is-token-required": "false"}
        return self.post(CUSTOMER, "/api/v2/users/pins/setup/tokens", body, auth=access_token, extra_headers=extra)

    def set_pin(self, access_token: str, pin: str, otp: str = "", otp_auth_token: str = "", notification_mode: str = "otp_sms") -> Tuple[int, Any, Dict[str, str]]:
        # Flutter AOT exact body at libapp.so+0xcd3164:
        #   {pin}
        # Request-side metadata around libapp.so+0x2314144 includes:
        #   otp, otp_auth_token, notification_mode
        body = {"pin": pin}
        extra = {"notification_mode": notification_mode}
        if otp:
            extra["otp"] = otp
        if otp_auth_token:
            extra["otp_auth_token"] = otp_auth_token
        return self.post(API, "/v2/users/pin", body, auth=access_token, extra_headers=extra)

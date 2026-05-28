#!/usr/bin/env python3
"""Host-side ADB helper for GoPay OTP and app unlink.

The server intentionally exposes the same HTTP surface as termux/otp_server.py:

  GET  /health
  GET  /otp
  POST /otp/clear
  POST /gopay/unlink

Run it on the machine that can reach the Android emulator/device through adb.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
OTP_HINTS = ("gopay", "gojek", "midtrans", "otp", "verification", "verifikasi", "kode")
DEFAULT_PACKAGE = "com.gojek.gopay"


@dataclass
class ServerConfig:
    adb_path: str = "adb"
    serial: str = ""
    package: str = DEFAULT_PACKAGE
    launch_activity: str = ""
    command_timeout: float = 15.0
    post_unlink_back_steps: int = 0
    otp_regex: str = OTP_PATTERN.pattern
    otp_hints: tuple[str, ...] = OTP_HINTS
    profile_patterns: list[str] = field(default_factory=lambda: [
        "Profile", "Profil", "Akun", "Account", "Saya",
    ])
    settings_patterns: list[str] = field(default_factory=lambda: [
        "Account & App Settings", "Pengaturan Akun", "Pengaturan",
        "Settings", "Account settings",
    ])
    linked_apps_patterns: list[str] = field(default_factory=lambda: [
        "Linked Apps", "Aplikasi Terhubung", "Aplikasi terhubung",
        "Connected apps", "OpenAI", "OpenAI LLC", "ChatGPT",
    ])
    openai_patterns: list[str] = field(default_factory=lambda: [
        "OpenAI LLC", "OpenAI", "ChatGPT",
    ])
    unlink_patterns: list[str] = field(default_factory=lambda: [
        "Unlink", "Putuskan", "Lepas", "Disconnect", "Hapus",
    ])
    confirm_patterns: list[str] = field(default_factory=lambda: [
        "Unlink", "Putuskan", "Ya", "OK", "Confirm", "Konfirmasi",
    ])


class AdbError(RuntimeError):
    pass


class AdbDevice:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg

    def adb(self, *args: str, timeout: float | None = None, check: bool = False) -> subprocess.CompletedProcess:
        cmd = [self.cfg.adb_path]
        if self.cfg.serial:
            cmd += ["-s", self.cfg.serial]
        cmd += list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or self.cfg.command_timeout,
        )
        if check and result.returncode != 0:
            raise AdbError((result.stderr or result.stdout or "adb command failed").strip())
        return result

    def shell(self, *args: str, timeout: float | None = None, check: bool = False) -> str:
        result = self.adb("shell", *args, timeout=timeout, check=check)
        return result.stdout or ""

    def devices(self) -> str:
        return self.adb("devices").stdout or ""

    def screen_size(self) -> tuple[int, int]:
        raw = self.shell("wm", "size")
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", raw)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1080, 2400

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def swipe_up(self) -> None:
        width, height = self.screen_size()
        self.shell(
            "input",
            "swipe",
            str(width // 2),
            str(int(height * 0.78)),
            str(width // 2),
            str(int(height * 0.28)),
            "450",
        )

    def keyevent(self, key: str) -> None:
        self.shell("input", "keyevent", key)

    def launch_gopay(self) -> None:
        if self.cfg.launch_activity:
            self.shell("am", "start", "-n", self.cfg.launch_activity, timeout=20)
            return
        monkey = self.shell(
            "monkey",
            "-p",
            self.cfg.package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=20,
        )
        if "No activities found" in monkey or "monkey aborted" in monkey.lower():
            self.shell("am", "start", "-n", f"{self.cfg.package}/.deeplink.DeeplinkActivity", timeout=20)

    def dump_ui(self) -> str:
        self.shell("uiautomator", "dump", "/sdcard/window.xml", timeout=20)
        xml_text = self.shell("cat", "/sdcard/window.xml", timeout=20)
        return xml_text.strip()

    def notification_text(self) -> str:
        return self.shell("dumpsys", "notification", "--noredact", timeout=20)


def _node_text(node: ET.Element) -> str:
    fields = [
        node.attrib.get("text") or "",
        node.attrib.get("content-desc") or "",
    ]
    return " ".join(fields)


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(part) for part in match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _bounds_rect(bounds: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _norm_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _parse_ui_nodes(xml_text: str) -> list[ET.Element]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(html.unescape(xml_text))
        except ET.ParseError:
            return []
    return list(root.iter("node"))


def find_element(xml_text: str, patterns: list[str]) -> tuple[int, int, str] | None:
    needles = [p.lower() for p in patterns if p]
    if not needles:
        return None
    for node in _parse_ui_nodes(xml_text):
        text = _node_text(node)
        lower = text.lower()
        if any(needle in lower for needle in needles):
            center = _bounds_center(node.attrib.get("bounds", ""))
            if center:
                return center[0], center[1], text[:160]
    return None


def ui_contains(xml_text: str, patterns: list[str]) -> bool:
    needles = [p.lower() for p in patterns if p]
    if not needles:
        return False
    for node in _parse_ui_nodes(xml_text):
        lower = _node_text(node).lower()
        if any(needle in lower for needle in needles):
            return True
    return False


EMPTY_LINKED_APPS_PATTERNS = [
    "No apps linked to your GoPay",
    "No apps linked",
    "Belum ada aplikasi",
    "Tidak ada aplikasi",
]


def is_account_settings_page(xml_text: str) -> bool:
    return ui_contains(xml_text, ["Account & app settings", "Pengaturan akun"])


def is_linked_apps_page(xml_text: str) -> bool:
    if is_account_settings_page(xml_text):
        return False
    return ui_contains(xml_text, ["Linked apps", "Aplikasi terhubung"])


def is_empty_linked_apps_page(xml_text: str) -> bool:
    return is_linked_apps_page(xml_text) and ui_contains(xml_text, EMPTY_LINKED_APPS_PATTERNS)


def already_unlinked_result(started: float, log: list[str]) -> dict[str, Any]:
    return {
        "status": "already_unlinked",
        "ok": True,
        "elapsed": round(time.time() - started, 2),
        "log": log + ["linked_apps: OpenAI not present; already unlinked"],
    }


def maybe_back_after_unlink(device: AdbDevice, cfg: ServerConfig, log: list[str]) -> None:
    steps = max(0, int(cfg.post_unlink_back_steps or 0))
    for _ in range(steps):
        device.keyevent("KEYCODE_BACK")
        time.sleep(0.4)
    if steps:
        log.append(f"post_unlink: back_steps={steps}")


def _current_package(xml_text: str) -> str:
    nodes = _parse_ui_nodes(xml_text)
    if not nodes:
        return ""
    return str(nodes[0].attrib.get("package") or "")


def tap_first(
    device: AdbDevice,
    patterns: list[str],
    log: list[str],
    label: str,
    *,
    scrolls: int = 0,
    tap_delay: float = 1.0,
    scroll_delay: float = 0.5,
) -> bool:
    for attempt in range(scrolls + 1):
        xml_text = device.dump_ui()
        found = find_element(xml_text, patterns)
        if found:
            x, y, text = found
            log.append(f"{label}: tap ({x},{y}) text={text!r}")
            device.tap(x, y)
            time.sleep(tap_delay)
            return True
        if attempt < scrolls:
            log.append(f"{label}: not found, swipe up")
            device.swipe_up()
            time.sleep(scroll_delay)
    log.append(f"{label}: not found")
    return False


def find_confirm_button(xml_text: str, patterns: list[str]) -> tuple[int, int, str] | None:
    exact_needles = {_norm_text(p).lower() for p in patterns if _norm_text(p)}
    blocked_fragments = (
        "?",
        "openai",
        "from gopay",
        "linked on",
        "account",
        "settings",
    )
    candidates: list[tuple[float, int, int, str]] = []
    for node in _parse_ui_nodes(xml_text):
        text = _norm_text(_node_text(node))
        if not text:
            continue
        lower = text.lower()
        if lower not in exact_needles:
            continue
        if any(fragment in lower for fragment in blocked_fragments):
            continue
        rect = _bounds_rect(node.attrib.get("bounds", ""))
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        clickable = (node.attrib.get("clickable") or "").lower() == "true"
        enabled = (node.attrib.get("enabled") or "true").lower() != "false"
        width = max(0, x2 - x1)
        height = max(0, y2 - y1)
        score = y / 10
        if clickable:
            score += 1000
        if enabled:
            score += 100
        if 30 <= height <= 180 and width >= 30:
            score += 20
        candidates.append((score, x, y, text[:160]))
    if not candidates:
        return None
    _, x, y, text = max(candidates, key=lambda item: item[0])
    return x, y, text


def tap_confirm(device: AdbDevice, patterns: list[str], log: list[str]) -> bool:
    xml_text = device.dump_ui()
    found = find_confirm_button(xml_text, patterns)
    if not found:
        log.append("confirm: button not found")
        return False
    x, y, text = found
    log.append(f"confirm: tap button ({x},{y}) text={text!r}")
    device.tap(x, y)
    time.sleep(1.0)
    return True


def extract_otp(raw: str, cfg: ServerConfig) -> dict[str, Any]:
    regex = re.compile(cfg.otp_regex, re.IGNORECASE)
    blocks = re.split(r"\n\s*NotificationRecord\(", raw or "")
    candidates = blocks if len(blocks) > 1 else [raw or ""]
    for block in reversed(candidates):
        haystack = block.lower()
        if "whatsapp" not in haystack and "com.whatsapp" not in haystack:
            continue
        message_fields = re.findall(
            r"android\.(?:title|text|bigText|subText|summaryText)=\w*String \((.*?)\)",
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        message_text = " ".join(message_fields)
        if not message_text:
            # Fallback for older dumpsys formats. Restrict to extras lines so
            # Android resource IDs such as 0x010804d4 are not treated as OTPs.
            message_text = " ".join(
                line.strip()
                for line in block.splitlines()
                if "android.title" in line or "android.text" in line or "android.bigText" in line
            )
        text_haystack = message_text.lower()
        if not any(hint.lower() in text_haystack for hint in cfg.otp_hints):
            continue
        match = regex.search(message_text)
        if match:
            ts = 0.0
            ts_match = re.search(r"m(?:Creation|Ranking|Update)TimeMs=(\d+)", block)
            if ts_match:
                try:
                    ts = int(ts_match.group(1)) / 1000.0
                except ValueError:
                    ts = 0.0
            return {
                "otp": match.group(1),
                "ts": ts,
                "raw": " ".join(message_text.split())[:1000],
                "source": "adb_notification",
            }
    return {}


class OtpCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"otp": None, "ts": 0, "raw": "", "source": ""}

    def clear(self) -> None:
        with self._lock:
            self._data = {"otp": None, "ts": 0, "raw": "", "source": ""}

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def set(self, otp: str, raw: str, source: str, ts: float | None = None) -> dict[str, Any]:
        with self._lock:
            self._data = {
                "otp": otp,
                "ts": float(ts or time.time()),
                "raw": raw,
                "source": source,
            }
            return dict(self._data)


def read_otp(device: AdbDevice, cfg: ServerConfig, cache: OtpCache, since_ts: float = 0) -> dict[str, Any]:
    result = extract_otp(device.notification_text(), cfg)
    if result.get("otp"):
        ts = float(result.get("ts") or 0)
        if since_ts and ts and ts <= since_ts:
            return {}
        return cache.set(
            result["otp"],
            result.get("raw", ""),
            result.get("source", "adb_notification"),
            ts=ts or None,
        )
    return {}


def gopay_unlink(device: AdbDevice, cfg: ServerConfig) -> dict[str, Any]:
    log: list[str] = []
    started = time.time()
    try:
        log.append("adb devices: " + " | ".join(device.devices().splitlines()[:4]))
        device.shell("cmd", "statusbar", "collapse", timeout=5)
        xml_text = device.dump_ui()
        if is_empty_linked_apps_page(xml_text):
            log.append("linked_apps: empty page detected on current screen")
            maybe_back_after_unlink(device, cfg, log)
            return already_unlinked_result(started, log)
        if _current_package(xml_text) != cfg.package:
            log.append("launch GoPay")
            device.launch_gopay()
            time.sleep(1.8)
            xml_text = device.dump_ui()
            if is_empty_linked_apps_page(xml_text):
                log.append("linked_apps: empty page detected after launch")
                maybe_back_after_unlink(device, cfg, log)
                return already_unlinked_result(started, log)
        else:
            log.append("GoPay already foreground; reuse current screen")

        width, height = device.screen_size()
        if is_linked_apps_page(xml_text):
            log.append("linked_apps: page detected after launch")
        elif not tap_first(device, cfg.profile_patterns, log, "profile", scrolls=0, tap_delay=0.8):
            x, y = int(width * 0.86), int(height * 0.93)
            log.append(f"profile fallback: tap bottom-right ({x},{y})")
            device.tap(x, y)
            time.sleep(1.0)

        xml_text = device.dump_ui()
        if not is_linked_apps_page(xml_text):
            tap_first(device, cfg.settings_patterns, log, "settings", scrolls=1, tap_delay=0.8, scroll_delay=0.4)
            tap_first(device, cfg.linked_apps_patterns, log, "linked_apps", scrolls=1, tap_delay=0.8, scroll_delay=0.4)

        xml_text = device.dump_ui()
        if is_empty_linked_apps_page(xml_text):
            log.append("linked_apps: empty page detected before OpenAI search")
            maybe_back_after_unlink(device, cfg, log)
            return already_unlinked_result(started, log)

        if not tap_first(device, cfg.openai_patterns, log, "openai_app", scrolls=1, tap_delay=0.8, scroll_delay=0.4):
            xml_text = device.dump_ui()
            if is_empty_linked_apps_page(xml_text):
                maybe_back_after_unlink(device, cfg, log)
                return already_unlinked_result(started, log)
            return {
                "status": "not_found",
                "ok": False,
                "error": "OpenAI linked app not found",
                "elapsed": round(time.time() - started, 2),
                "log": log,
            }

        if not tap_first(device, cfg.unlink_patterns, log, "unlink", scrolls=1, tap_delay=0.8, scroll_delay=0.4):
            return {
                "status": "not_found",
                "ok": False,
                "error": "unlink button not found",
                "elapsed": round(time.time() - started, 2),
                "log": log,
            }

        # Dialogs sometimes animate in slowly.
        time.sleep(0.8)
        if not tap_confirm(device, cfg.confirm_patterns, log):
            return {
                "status": "not_found",
                "ok": False,
                "error": "confirm button not found",
                "elapsed": round(time.time() - started, 2),
                "log": log,
            }
        maybe_back_after_unlink(device, cfg, log)
        return {
            "status": "ok",
            "ok": True,
            "elapsed": round(time.time() - started, 2),
            "log": log,
        }
    except Exception as exc:
        try:
            device.keyevent("KEYCODE_HOME")
        except Exception:
            pass
        return {
            "status": "error",
            "ok": False,
            "error": str(exc),
            "elapsed": round(time.time() - started, 2),
            "log": log,
        }


def make_handler(device: AdbDevice, cfg: ServerConfig, cache: OtpCache):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GoPayAdbServer/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {fmt % args}")

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                cached = cache.get()
                self._json(200, {
                    "ok": True,
                    "adb_serial": cfg.serial,
                    "package": cfg.package,
                    "otp_stored": bool(cached.get("otp")),
                    "otp_ts": cached.get("ts", 0),
                })
                return
            if self.path.startswith("/otp"):
                since = self._since_ts()
                cached = cache.get()
                if cached.get("otp") and float(cached.get("ts") or 0) > since:
                    self._json(200, cached)
                    return
                result = read_otp(device, cfg, cache, since_ts=since)
                if result.get("otp") and float(result.get("ts") or 0) > since:
                    self._json(200, result)
                else:
                    self._json(200, {"otp": None, "ts": 0})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if self.path.startswith("/otp/clear"):
                cache.clear()
                self._json(200, {"ok": True})
                return
            if self.path.startswith("/gopay/unlink"):
                self._json(200, gopay_unlink(device, cfg))
                return
            self._json(404, {"ok": False, "error": "not found"})

        def _since_ts(self) -> float:
            try:
                return float(self.headers.get("X-Since-Ts", "0") or "0")
            except ValueError:
                return 0.0

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def load_config(path: str) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def build_config(args: argparse.Namespace) -> ServerConfig:
    file_cfg = load_config(args.config)
    adb_cfg = file_cfg.get("adb") if isinstance(file_cfg.get("adb"), dict) else file_cfg
    cfg = ServerConfig(
        adb_path=args.adb_path or str(adb_cfg.get("adb_path") or "adb"),
        serial=args.serial or os.getenv("ADB_SERIAL", "").strip() or str(adb_cfg.get("serial") or ""),
        package=args.package or str(adb_cfg.get("package") or DEFAULT_PACKAGE),
        launch_activity=args.launch_activity or str(adb_cfg.get("launch_activity") or ""),
        command_timeout=float(adb_cfg.get("command_timeout") or 15),
        post_unlink_back_steps=int(adb_cfg.get("post_unlink_back_steps") or 0),
        otp_regex=str(adb_cfg.get("otp_regex") or OTP_PATTERN.pattern),
    )
    if isinstance(adb_cfg.get("otp_hints"), list):
        cfg.otp_hints = tuple(str(item) for item in adb_cfg["otp_hints"] if str(item).strip())
    for attr in (
        "profile_patterns",
        "settings_patterns",
        "linked_apps_patterns",
        "openai_patterns",
        "unlink_patterns",
        "confirm_patterns",
    ):
        value = adb_cfg.get(attr)
        if isinstance(value, list) and value:
            setattr(cfg, attr, [str(item) for item in value if str(item).strip()])
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="GoPay OTP/unlink ADB HTTP sidecar")
    parser.add_argument("--listen", default=os.getenv("GOPAY_ADB_LISTEN", "127.0.0.1:9999"))
    parser.add_argument("--config", default="")
    parser.add_argument("--adb-path", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument("--package", default="")
    parser.add_argument("--launch-activity", default="")
    args = parser.parse_args()

    cfg = build_config(args)
    host, _, port_text = args.listen.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port_text or "9999")
    device = AdbDevice(cfg)
    cache = OtpCache()

    print(f"[*] GoPay ADB server listening on http://{host}:{port}")
    print(f"[*] adb={cfg.adb_path} serial={cfg.serial or '<default>'} package={cfg.package}")
    print("[*] endpoints: GET /health, GET /otp, POST /otp/clear, POST /gopay/unlink")

    server = ThreadingHTTPServer((host, port), make_handler(device, cfg, cache))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

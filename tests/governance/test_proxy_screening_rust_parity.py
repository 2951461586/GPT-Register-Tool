"""Parity pin between `sms_tool/proxy/screening.py` and `rust/proxy-screening-rs/markers.json`."""

from __future__ import annotations

import json
from pathlib import Path

from sms_tool.proxy.screening import DATACENTER_KEYWORDS, MOBILE_KEYWORDS

ROOT = Path(__file__).resolve().parents[2]
MARKERS_JSON = ROOT / "rust" / "proxy-screening-rs" / "markers.json"


def test_proxy_screening_keywords_match_rust_port() -> None:
    payload = json.loads(MARKERS_JSON.read_text(encoding="utf-8"))
    assert tuple(payload["datacenter_keywords"]) == tuple(DATACENTER_KEYWORDS)
    assert tuple(payload["mobile_keywords"]) == tuple(MOBILE_KEYWORDS)

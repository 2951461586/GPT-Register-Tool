"""Parity pin between `sms_tool/store/constants.py` and `rust/store-normalize-rs/src/port.rs`."""

from __future__ import annotations

import re
from pathlib import Path

from sms_tool.store.constants import KNOWN_EMAIL_DOMAINS

ROOT = Path(__file__).resolve().parents[2]
RUST_PORT = ROOT / "rust" / "store-normalize-rs" / "src" / "port.rs"


def test_known_email_domains_match_rust_port() -> None:
    text = RUST_PORT.read_text(encoding="utf-8")
    match = re.search(r"pub const KNOWN_EMAIL_DOMAINS:\s*\[&str;\s*\d+\]\s*=\s*\[(?P<body>.*?)\];", text, re.S)
    assert match is not None, "KNOWN_EMAIL_DOMAINS table missing from rust/store-normalize-rs/src/port.rs"
    rust_domains = tuple(re.findall(r'"([^"]+)"', match.group("body")))
    assert rust_domains == tuple(KNOWN_EMAIL_DOMAINS), (
        f"KNOWN_EMAIL_DOMAINS drifted between Python ({KNOWN_EMAIL_DOMAINS}) and Rust ({rust_domains})"
    )

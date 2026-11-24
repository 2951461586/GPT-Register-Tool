"""Parity pin between `sms_tool/kernel/failure_registry.py` and `rust/error-classification-rs/markers.json`."""

from __future__ import annotations

import json
from pathlib import Path

from sms_tool.kernel.failure_registry import FAILURE_CLASSES, TERMINAL_ERROR_MARKERS

ROOT = Path(__file__).resolve().parents[2]
MARKERS_JSON = ROOT / "rust" / "error-classification-rs" / "markers.json"


def test_failure_classes_and_terminal_markers_match_rust_port() -> None:
    payload = json.loads(MARKERS_JSON.read_text(encoding="utf-8"))
    rust_classes = [(entry["code"], tuple(entry["markers"])) for entry in payload["classes"]]
    py_classes = [(cls.code, tuple(cls.markers)) for cls in FAILURE_CLASSES]
    assert rust_classes == py_classes, f"FAILURE_CLASSES drifted between Python and {MARKERS_JSON}"

    rust_terminal = tuple(payload["terminal_markers"])
    assert rust_terminal == tuple(TERMINAL_ERROR_MARKERS), (
        f"TERMINAL_ERROR_MARKERS drifted between Python and {MARKERS_JSON}"
    )

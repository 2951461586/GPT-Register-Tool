"""Shared account/session seed loading helpers.

This module is the seam between payment adapters and persisted account state.
Callers get a merged session dictionary plus the backing session JSON path; the
details of SQLite row lookup, raw JSON merging, and token extraction stay here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import get_account_record


def load_account_seed(email: str = "", session_file: str = "") -> tuple[dict[str, Any], str]:
    """Load an account seed from an explicit session file or the SQLite index."""
    if session_file:
        path = Path(session_file)
        return read_json(path), str(path)

    record = get_account_record(email) if email else {}
    json_path = str(record.get("json_path") or "").strip()
    data: dict[str, Any] = {}
    if json_path:
        data = read_json(Path(json_path))

    raw_json = str(record.get("raw_json") or "").strip()
    if raw_json:
        try:
            raw_data = json.loads(raw_json)
            if isinstance(raw_data, dict):
                data = {**raw_data, **data}
        except Exception:
            pass

    if record:
        data.setdefault("email", record.get("email", ""))
        data.setdefault("access_token", record.get("access_token", ""))
        data.setdefault("cookie_header", record.get("cookie_header", ""))
        data.setdefault("oauth_refresh_token", record.get("oauth_refresh_token", ""))
        data.setdefault("refresh_token", record.get("refresh_token", ""))
    return data, json_path


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def extract_access_token(data: dict[str, Any]) -> str:
    """Extract ChatGPT access token from flat or auth-session shaped seed data."""
    token = str(data.get("access_token") or "").strip()
    if token:
        return token
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = auth_session.get(key)
        if isinstance(value, str) and value:
            return value
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = session.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

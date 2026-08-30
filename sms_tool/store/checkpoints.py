"""checkpoints submodule of the former storage.py (mechanical split, bodies unchanged)."""

import json
import time

from ..config import ConfigInput

from .connection import _connect, init_database
from .normalize import _normalize_account_email


def save_registration_checkpoint(email, state, payload, *, runtime_config: ConfigInput = None):
    """Atomically persist resumable registration state before risky follow-up calls."""
    normalized = _normalize_account_email(email)
    if not normalized:
        return False
    init_database(runtime_config=runtime_config)
    safe_payload = dict(payload or {})
    safe_payload["email"] = normalized
    safe_payload["registration_state"] = str(state or "")
    encoded = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), default=str)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    try:
        conn.execute(
            """INSERT INTO registration_checkpoints(email,state,payload_json,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET state=excluded.state,
               payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (normalized, str(state or ""), encoded, now),
        )
        conn.commit()
    finally:
        conn.close()
    return True



def get_registration_checkpoint(email, *, runtime_config: ConfigInput = None):
    normalized = _normalize_account_email(email)
    if not normalized:
        return {}
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        row = conn.execute("SELECT * FROM registration_checkpoints WHERE email=?", (normalized,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    result = dict(row)
    try:
        result["payload"] = json.loads(result.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["payload"] = {}
    return result



def clear_registration_checkpoint(email, *, runtime_config: ConfigInput = None):
    normalized = _normalize_account_email(email)
    if not normalized:
        return False
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        conn.execute("DELETE FROM registration_checkpoints WHERE email=?", (normalized,))
        conn.commit()
    finally:
        conn.close()
    return True


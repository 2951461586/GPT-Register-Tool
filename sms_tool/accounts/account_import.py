"""Import an exported ``account-cpa-*.json`` back into the local store.

Why this exists
---------------
``session_converter`` can *write* the CPA shape and ``cpa_import`` can push a
local session *into* CPA/SUB2API, but nothing could read an exported file back:
a user who exported their accounts and lost the checkout could not restore them
(issue #11). This module is that inverse path, and it is deliberately offline --
it never calls OpenAI, CPA or SUB2API.

Three rules the importer follows, each one a mistake it would otherwise make:

* **A newer token is never replaced by an older one.** Re-importing last week's
  file must not overwrite this week's access token, so items are deduplicated by
  email on the JWT ``exp`` (later wins) instead of file order -- both inside the
  document and against the token already in ``accounts.sqlite3``.
* **A synthetic ``id_token`` is not stored as a credential.**
  ``session_converter`` marks the ones it fabricates with ``id_token_synthetic``;
  persisting those would put a forged JWT in ``accounts.id_token``.
* **One bad item does not abort the batch.** Every rejection carries a stable
  reason (``missing_email`` / ``missing_token`` / ``disabled`` /
  ``older_token``) so a 200-account file reports what it could not use.

The write path is the registration one: the same session-file layout
(``sessions/session_<email>_<ts>.json``, atomic replace) plus
``store.accounts.upsert_account``, which is what keeps ``accounts.sqlite3`` and
the session files in step.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger(__name__)

DEFAULT_FILENAME_PATTERN = "session_{email}_{timestamp}.json"
#: ``register_method`` for every imported account, so a later audit can tell an
#: import from a registration without guessing from timestamps.
REGISTER_METHOD = "import"
SAFE_EMAIL = re.compile(r"[^a-zA-Z0-9_.@-]+")


class AccountImportError(RuntimeError):
    """The export document itself is unusable (not one item's problem)."""


def load_export_items(path: str | Path) -> list[dict[str, Any]]:
    """The account objects in an exporter document.

    Accepts the three shapes the desktop exporter and the converter can produce:
    a single object, an array of objects, or a document with an ``accounts``
    list.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AccountImportError(f"account_export_unreadable:{type(exc).__name__}") from exc
    if isinstance(data, dict):
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            return [item for item in accounts if isinstance(item, dict)]
        return [data] if data else []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise AccountImportError("account_export_invalid_json")


def _text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def token_expiry(token: str) -> int:
    """JWT ``exp`` in epoch seconds, ``0`` when the token is not a JWT.

    The import compares this, never ``last_refresh``: the exporter's timestamp is
    when the *file* was written, which says nothing about which token is newer.
    """
    from ..session.converter import parse_jwt_payload

    payload = parse_jwt_payload(token)
    try:
        return int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return 0


def _id_token(item: Mapping[str, Any]) -> str:
    if bool(item.get("id_token_synthetic")):
        return ""
    return _text(item, "id_token")


def _session_input(item: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """``build_session_file`` input for one exported item, or a skip reason."""
    email = _text(item, "email").lower()
    if not email:
        return None, "missing_email"
    if bool(item.get("disabled")):
        return None, "disabled"
    access_token = _text(item, "access_token")
    if not access_token and not _text(item, "session_token"):
        return None, "missing_token"
    record: dict[str, Any] = {
        "email": email,
        "access_token": access_token,
        "session_token": _text(item, "session_token"),
        "refresh_token": _text(item, "refresh_token"),
        "id_token": _id_token(item),
        "plan_type": _text(item, "plan_type", "chatgpt_plan_type"),
        "register_method": REGISTER_METHOD,
        "import_source": "account_cpa_json",
        # The exported ``last_refresh``/``expired`` fields describe the file, not
        # the account; keeping the original registration time unknown is honest.
        "imported_at": int(time.time()),
    }
    return record, ""


def merge_items(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Deduplicate by email, keeping the newest access token; collect skips."""
    kept: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    for item in items:
        record, reason = _session_input(item)
        if record is None:
            skipped.append({"email": _text(item, "email"), "reason": reason})
            continue
        email = record["email"]
        previous = kept.get(email)
        if previous is not None and token_expiry(str(previous["access_token"])) >= token_expiry(
            str(record["access_token"])
        ):
            skipped.append({"email": email, "reason": "older_token"})
            continue
        kept[email] = record
    return kept, skipped


def session_path_for(email: str, pattern: str, directory: str | Path) -> Path:
    """``sessions/session_<safe email>_<ts>.json`` -- the registration layout."""
    safe = SAFE_EMAIL.sub("_", email.replace("+", ""))
    name = pattern.format(email=safe, phone=safe, timestamp=int(time.time()))
    return Path(directory) / name


def _stored_access_token(email: str, runtime_config: Mapping[str, Any] | None) -> str:
    """The access token already in the store, or ``""`` when absent/unreadable."""
    try:
        from ..storage import get_account_record

        record = get_account_record(email, runtime_config=runtime_config)
    except (ImportError, OSError, RuntimeError, KeyError, TypeError, ValueError, sqlite3.Error):
        # No row, or no database yet: "nothing older to compare against" is a
        # normal state for a first import, not a failure of the import.
        return ""
    if not isinstance(record, Mapping):
        return ""
    return str(record.get("access_token") or "").strip()


def _write_session(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def import_sessions(
    kept: Mapping[str, Mapping[str, Any]],
    *,
    session_dir: str | Path,
    filename_pattern: str = DEFAULT_FILENAME_PATTERN,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one session file per account and upsert the SQLite row."""
    paths: list[str] = []
    failed: list[dict[str, str]] = []
    superseded: list[dict[str, str]] = []
    db_saved = 0
    from ..session.builder import build_session_file
    from ..store.accounts import upsert_account

    for email, record in kept.items():
        stored = token_expiry(_stored_access_token(email, runtime_config))
        if stored and stored > token_expiry(str(record.get("access_token") or "")):
            # The store already holds a newer credential for this account: writing
            # this file would *downgrade* it (the 2026-11-19 rule this module exists
            # to keep). Report it like any other skip instead of silently dropping.
            superseded.append({"email": email, "reason": "newer_token_in_store"})
            continue
        try:
            session = build_session_file(dict(record))
            path = session_path_for(email, filename_pattern, session_dir)
            _write_session(path, session)
            paths.append(str(path))
            if upsert_account(session, json_path=str(path), runtime_config=runtime_config):
                db_saved += 1
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            logger.warning("account import failed for %s: %s", email, type(exc).__name__)
            failed.append({"email": email, "reason": f"{type(exc).__name__}"})
    return {"session_paths": paths, "db_saved": db_saved, "failed": failed, "superseded": superseded}


def import_account_json(
    path: str | Path,
    *,
    session_dir: str | Path | None = None,
    filename_pattern: str = DEFAULT_FILENAME_PATTERN,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Import one export file; returns a report instead of raising per item."""
    from ..kernel.paths import output_dir

    items = load_export_items(path)
    kept, skipped = merge_items(items)
    directory = session_dir or output_dir(dict(runtime_config or {}))
    written = import_sessions(
        kept,
        session_dir=directory,
        filename_pattern=filename_pattern,
        runtime_config=runtime_config,
    )
    failed = written["failed"]
    skipped = [*skipped, *written["superseded"]]
    return {
        "ok": not failed,
        "source": str(path),
        "total": len(items),
        "imported": len(written["session_paths"]),
        "db_saved": written["db_saved"],
        "skipped": skipped,
        "failed": failed,
        "session_paths": written["session_paths"],
    }


__all__ = [
    "AccountImportError",
    "DEFAULT_FILENAME_PATTERN",
    "REGISTER_METHOD",
    "import_account_json",
    "import_sessions",
    "load_export_items",
    "merge_items",
    "session_path_for",
    "token_expiry",
]

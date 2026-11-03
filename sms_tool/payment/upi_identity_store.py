"""Cross-process ledger for the per-account UPI identity.

Why a ledger at all
-------------------
The UPI lane used to build its billing profile and its browser fingerprint from
``secrets.randbelow`` on **every invocation** (``session._upi_billing_profile`` /
``session._upi_fingerprint``).  One account retried three times therefore
presented three different names, addresses *and* user agents, and two accounts in
the same batch could share the same address -- exactly the correlations a risk
engine looks for, and the opposite of what the reference implementation does
(``upi_extract.py:4537 upi_billing_profile`` / ``fingerprint_store.py:320
_ensure_account_fingerprint``, both sticky per account and de-duplicated).

This module owns the *storage* half of that fix and nothing else: one JSON file,
one cross-process write lock, atomic replace.  The policies (which identity to
allocate, when to rotate, when a fingerprint cools down) live in
``billing.py`` / ``fingerprint_identity.py`` so they stay unit-testable without
a filesystem.

Layout
------
``runtime/upi_identity.json`` (``UPI_IDENTITY_STORE_DIR`` overrides the
directory)::

    {
      "version": 1,
      "accounts": {
        "<account key>": {
          "billing": {"identity": "...", "profile": {...}, "assigned_at": 0},
          "fingerprint": {"index": 2, "name": "chrome-win", "assigned_at": 0,
                          "usage": {"success": 1, "blocked": 0, "failed": 0,
                                    "last_status": "success",
                                    "last_reason": "", "cooldown_until": 0.0}}
        }
      },
      "identities": {"<billing identity>": "<account key>"}
    }

The file holds no credentials: a name, a street address, a template index and
counters.  It is still operator data (it links accounts together), which is why
it lives under ``runtime/`` next to the other state files rather than in the
package.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .upi_identity import token_account_id, token_claims

#: Ledger file name inside the runtime directory.
STORE_FILENAME = "upi_identity.json"

#: Schema version; a mismatch is treated as "start over" rather than a crash.
STORE_VERSION = 1

#: ``UPI_IDENTITY_STORE_DIR`` overrides the ledger directory.  Tests use it to
#: get an isolated file; an operator can pin both backend processes to one place.
STORE_DIR_ENV = "UPI_IDENTITY_STORE_DIR"

_local_locks: dict[str, threading.RLock] = {}
_local_guard = threading.Lock()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _runtime_file(cfg: Mapping[str, Any] | None, filename: str) -> Path:
    """``kernel.paths.runtime_file``, imported at call time.

    Module-level would add ``upi_link -> kernel`` to the import-layer ratchet for
    one path helper; keeping it local leaves that edge at zero.
    """
    from ..kernel.paths import runtime_file

    return runtime_file(cfg, filename)


# ── env readers ─────────────────────────────────────────────────────────────
# The identity/exist state modules must not import ``upi_link.env``: that would
# make the payment kernel depend on a legacy lane.  These three readers are the
# same five-line contract, kept here so every moved module imports one owner.
def env_text(name: str, default: str = "") -> str:
    """Read a string env switch (``""``/unset ⇒ ``default``)."""
    return _text(os.environ.get(name)) or default


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env switch; unknown spellings fall back to ``default``."""
    raw = _text(os.environ.get(name)).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "启用", "开启"}:
        return True
    if raw in {"0", "false", "no", "off", "禁用", "关闭"}:
        return False
    return default


def env_number(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive int env switch, clamped to ``minimum``."""
    raw = _text(os.environ.get(name))
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def store_dir(cfg: Mapping[str, Any] | None = None) -> Path:
    override = _text(os.environ.get(STORE_DIR_ENV))
    if override:
        return Path(override).expanduser()
    return _runtime_file(cfg if isinstance(cfg, Mapping) else {}, STORE_FILENAME).parent


def store_path(cfg: Mapping[str, Any] | None = None) -> Path:
    override = _text(os.environ.get(STORE_DIR_ENV))
    if override:
        directory = Path(override).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / STORE_FILENAME
    return _runtime_file(cfg if isinstance(cfg, Mapping) else {}, STORE_FILENAME)


def account_key(
    *,
    account_id: Any = "",
    access_token: Any = "",
    email: Any = "",
    session_token: Any = "",
) -> str:
    """Stable, non-reversible key for one account, or ``""`` when unknowable.

    Order matters: a caller-supplied account id wins over the token, and the
    token wins over an email, because the token is what the run actually
    authenticates with.  ``""`` is a real answer -- it means "allocate nothing
    and touch no disk", which keeps the historical random behaviour for
    throwaway tokens (and keeps the offline suite free of file writes).

    ``session_token`` is only a last resort: it rotates per login, so it keys an
    account far worse than an id -- but it is still better than nothing when the
    access token is a bare ``at`` string.
    """
    for candidate in (account_id, token_account_id(access_token), email):
        text = _text(candidate)
        if text:
            return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:24]
    # ``sub``/``email`` claims exist on tokens without ``chatgpt_account_id``.
    claims = token_claims(access_token)
    for claim in ("sub", "email"):
        text = _text(claims.get(claim))
        if text:
            return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:24]
    seeded = _text(session_token)
    if seeded:
        return hashlib.sha256(("session:" + seeded).encode("utf-8")).hexdigest()[:24]
    return ""


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _local_guard:
        lock = _local_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _local_locks[key] = lock
        return lock


def empty_ledger() -> dict[str, Any]:
    return {"version": STORE_VERSION, "accounts": {}, "identities": {}}


def read_ledger(path: Path | None = None, *, cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the ledger; a missing/corrupt/old file reads as empty.

    Identity allocation is an optimisation, never a prerequisite: a torn or
    hand-edited file must not stop a link from being generated, so every failure
    path degrades to "allocate fresh" instead of raising.
    """
    target = path or store_path(cfg)
    try:
        raw = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return empty_ledger()
    if not isinstance(raw, dict):
        return empty_ledger()
    try:
        version = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        # A hand-edited ``"version": "1"`` is still a usable ledger; an
        # unparsable one is not worth reconstructing.
        return empty_ledger()
    if version != STORE_VERSION:
        return empty_ledger()
    # Keep every section, not just the two this module owns: ``exit_health``
    # stores its ``exits`` map in the same shape, and silently dropping unknown
    # top-level keys would make ``record_block`` look like it never wrote.
    ledger: dict[str, Any] = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    ledger.setdefault("version", STORE_VERSION)
    accounts = raw.get("accounts")
    identities = raw.get("identities")
    ledger["accounts"] = accounts if isinstance(accounts, dict) else {}
    ledger["identities"] = identities if isinstance(identities, dict) else {}
    return ledger


def write_ledger(payload: dict[str, Any], path: Path | None = None, *, cfg: Mapping[str, Any] | None = None) -> None:
    """Atomically replace the ledger (temp file is pid-tagged, then renamed)."""
    target = path or store_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["version"] = STORE_VERSION
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


@contextmanager
def ledger_transaction(path: Path | None = None, *, cfg: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Read-modify-write the ledger under both a thread and a process lock.

    The in-process ``RLock`` is required because a batch runs lanes in threads:
    the OS byte lock alone is held per *handle*, so two threads of one process
    would still interleave.  The cross-process lock is the one
    ``cross_process_write_lock`` already provides for every other shared state
    file, reused here rather than re-implemented.
    """
    target = path or store_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    from ..cross_process_gate import cross_process_write_lock

    lock = _thread_lock(target)
    with lock:
        lock_file = target.with_name(target.name + ".lock")
        with cross_process_write_lock(lock_file):
            ledger = read_ledger(target)
            yield ledger
            write_ledger(ledger, target)


def account_entry(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    """Return (creating if needed) the mutable per-account entry."""
    accounts = ledger.setdefault("accounts", {})
    entry = accounts.get(key)
    if not isinstance(entry, dict):
        entry = {}
        accounts[key] = entry
    return entry


__all__ = [
    "STORE_DIR_ENV",
    "STORE_FILENAME",
    "STORE_VERSION",
    "account_entry",
    "account_key",
    "empty_ledger",
    "ledger_transaction",
    "read_ledger",
    "store_dir",
    "store_path",
    "write_ledger",
]

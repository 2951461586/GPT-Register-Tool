"""Write rotated OAuth refresh tokens back to the mailbox pool file.

Why this exists
---------------
Microsoft and Google can both return a **new** ``refresh_token`` on a successful
OAuth refresh.  ``providers/mailbox_graph.ms_oauth_refresh`` and
``providers/mailbox_gmail.refresh_gmail_access_token`` already store the rotated
value on the in-memory ``MailboxAccount``, but until 2026-09-22 nothing wrote it
back to the pool file.  The pool therefore kept the *first imported* token, so
the next run presented a value the identity provider had already retired:

    HTTP 400 ``invalid_grant`` (``error_codes`` 9002313)

That raises ``MailboxTokenExpiredError``, i.e. "this mailbox is dead" -- even
though the mailbox is fine and only our copy of the token is stale.  The ledger
records a generic fetch failure, so the real cause stays invisible.

Why it edits text instead of regenerating the file
--------------------------------------------------
The pool file is supplier/operator-authored, **not** Python-generated.  Real
pools carry comments, blank lines, a UTF-8 BOM, and columns this module does not
know about (``order_no``/``purchase_id`` on ReMail rows, an optional
``access_token`` tail on Graph rows, a ``login_password`` on Gmail app-password
rows).  Rewriting the file from parsed records would silently drop all of that,
so this module rewrites **exactly one field on exactly one line** and leaves
every other byte alone -- including the file's own line endings.

Safety rules (every one of them refuses rather than guesses)
-----------------------------------------------------------
* the anchor is the *previous* token value, and it must appear **exactly once in
  the whole file**; anything else means we cannot tell which field holds the
  token, and a wrong guess corrupts a neighbouring column;
* the line carrying that anchor must belong to the mailbox we refreshed;
* the write goes through ``cross_process_write_lock`` + ``os.replace`` so a CLI
  and a workbench process cannot interleave a read-modify-write and lose each
  other's rotation;
* every failure returns a status string and logs.  This module never raises:
  losing the write-back must not abort an OTP fetch that is otherwise fine --
  the rotated token is still live in memory for the rest of the run.

Set ``MAILBOX_RT_WRITEBACK=0`` to freeze the pool file (operator escape hatch,
same shape as the other ``*_enabled`` switches in this package).
"""

from __future__ import annotations

import codecs
import logging
import os
import re
from pathlib import Path
from typing import Any

from .cross_process_gate import GateTimeoutError, cross_process_write_lock

_LOGGER = logging.getLogger(__name__)

#: Status vocabulary returned by :func:`persist_rotated_refresh_token`.
STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_NO_POOL = "no_pool"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_DISABLED = "disabled"
STATUS_LOCK_TIMEOUT = "lock_timeout"
STATUS_ERROR = "error"

_BOM = codecs.BOM_UTF8
_FIELD_SEPARATOR = re.compile(r"-{3,}")


def writeback_enabled() -> bool:
    """``MAILBOX_RT_WRITEBACK=0`` disables the write-back (default: enabled)."""
    value = str(os.environ.get("MAILBOX_RT_WRITEBACK", "") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def resolve_pool_path(mailbox: Any, pool_path: str | Path | None = None) -> Path | None:
    """Return the pool file this mailbox came from, or ``None``.

    ``MailboxAccount.source`` is stamped by the pool loaders.  A mailbox built
    from CLI flags or a config block has no source, and there is deliberately no
    fallback to ``cwd/mailbox_tokens.txt``: guessing a path here would let a
    write-back land in a pool the record never came from.
    """
    if pool_path is not None:
        candidate = Path(pool_path)
        return candidate if candidate.is_file() else None
    source = str(getattr(mailbox, "source", "") or "").strip()
    if not source:
        return None
    candidate = Path(source)
    return candidate if candidate.is_file() else None


def _line_owner_email(line: str) -> str:
    """Extract the address a pool line belongs to.

    Handles every format ``mailbox_parsers.parse_mailbox_pool_line`` accepts:
    a ``scheme://`` prefix (``gmail://``, ``cfworker://``, ``remail://``,
    ``smailr://``), then ``---``/``----`` separated fields.  Only the first field
    is ever the address, so no provider-specific knowledge is needed.
    """
    payload = str(line or "").strip()
    if "://" in payload:
        payload = payload.split("://", 1)[1]
    first = _FIELD_SEPARATOR.split(payload, maxsplit=1)[0]
    return first.strip().lower()


def _replace_anchor(path: Path, email: str, previous: str, current: str) -> str:
    """Rewrite one line of ``path`` in place.  Caller holds the write lock."""
    raw = path.read_bytes()
    has_bom = raw.startswith(_BOM)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        _LOGGER.warning("mailbox pool %s is not UTF-8, refusing write-back: %s", path, exc)
        return STATUS_ERROR

    lines = text.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if previous in line]
    if not hits:
        # The token we refreshed is not in this file any more -- a previous run
        # already wrote back a newer one, or the row was edited by hand.
        return STATUS_NOT_FOUND
    if len(hits) > 1:
        # Two rows carrying the same token: we cannot tell which one we rotated.
        return STATUS_AMBIGUOUS
    index = hits[0]
    if _line_owner_email(lines[index]) != email:
        return STATUS_NOT_FOUND

    lines[index] = lines[index].replace(previous, current)
    data = "".join(lines).encode("utf-8")
    if has_bom:
        data = _BOM + data

    # Binary write on purpose: text mode would translate "\n" to "\r\n" on
    # Windows and rewrite every line ending in a file we only meant to touch
    # once.  ``os.replace`` keeps the swap atomic on the same volume.
    temporary = path.with_name(path.name + ".rtwrite.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except OSError as exc:
        _LOGGER.warning("mailbox pool write-back failed for %s: %s", path, exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        return STATUS_ERROR
    return STATUS_UPDATED


def persist_rotated_refresh_token(
    mailbox: Any,
    *,
    previous: str,
    current: str,
    pool_path: str | Path | None = None,
    timeout: float = 10.0,
) -> str:
    """Persist a rotated ``refresh_token`` onto the mailbox's pool line.

    ``previous`` is the value read from the pool, ``current`` the value the
    identity provider just returned.  Returns one of the ``STATUS_*`` constants;
    never raises.
    """
    previous = str(previous or "")
    current = str(current or "")
    if not current or current == previous:
        return STATUS_UNCHANGED
    if not writeback_enabled():
        return STATUS_DISABLED
    path = resolve_pool_path(mailbox, pool_path)
    if path is None:
        return STATUS_NO_POOL
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    if not email:
        return STATUS_NOT_FOUND

    status = STATUS_ERROR
    try:
        with cross_process_write_lock(path.with_name(path.name + ".lock"), timeout=timeout):
            status = _replace_anchor(path, email, previous, current)
    except GateTimeoutError as exc:
        _LOGGER.warning("mailbox pool write-back skipped (lock): %s", exc)
        return STATUS_LOCK_TIMEOUT
    except OSError as exc:
        _LOGGER.warning("mailbox pool write-back failed for %s: %s", email, exc)
        return STATUS_ERROR

    if status == STATUS_UPDATED:
        _LOGGER.info(
            "mailbox refresh token rotated and written back email=%s pool=%s",
            email,
            path,
            extra={"event": "mailbox_refresh_token_writeback", "email": email, "pool": str(path)},
        )
    elif status not in (STATUS_UNCHANGED,):
        _LOGGER.warning(
            "mailbox refresh token rotated but not written back email=%s pool=%s status=%s",
            email,
            path,
            status,
            extra={
                "event": "mailbox_refresh_token_writeback_missed",
                "email": email,
                "pool": str(path),
                "status": status,
            },
        )
    return status


def apply_rotation(
    mailbox: Any,
    *,
    previous: str,
    current: str,
    pool_path: str | Path | None = None,
) -> str:
    """Store ``current`` on the mailbox and write it back to its pool line.

    Convenience wrapper so provider adapters cannot forget one half of the
    operation: every rotation must both update the in-memory record and persist.
    """
    mailbox.refresh_token = current
    return persist_rotated_refresh_token(mailbox, previous=previous, current=current, pool_path=pool_path)

"""Typed account lifecycle operations shared by CLI and desktop adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Iterable

from .config import ConfigInput, resolve_runtime_config
from .storage import database_path


@dataclass(frozen=True)
class AccountDeleteRequest:
    email: str
    mailbox_files: tuple[str, ...] = ()
    include_session: bool = True


@dataclass(frozen=True)
class AccountDeleteResult:
    email: str
    removed_mailbox_lines: int
    removed_database_rows: int
    archived_sessions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "removed_mailbox_lines": self.removed_mailbox_lines,
            "removed_database_rows": self.removed_database_rows,
            "archived_sessions": list(self.archived_sessions),
        }


class AccountLifecycle:
    def __init__(self, runtime_config: ConfigInput = None) -> None:
        self.config = resolve_runtime_config(runtime_config)

    def delete(self, request: AccountDeleteRequest) -> AccountDeleteResult:
        email = str(request.email or "").strip()
        if not email:
            raise ValueError("email is required")
        db = database_path(self.config)
        removed_rows = 0
        if db.exists():
            import sqlite3
            with sqlite3.connect(db) as conn:
                cursor = conn.execute("DELETE FROM accounts WHERE lower(email)=lower(?)", (email,))
                removed_rows = max(0, int(cursor.rowcount or 0))
        removed_lines = 0
        mailbox_files = tuple(request.mailbox_files) or self._configured_mailbox_files()
        for raw_path in mailbox_files:
            path = Path(raw_path)
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
            kept = [line for line in lines if not self._mailbox_line_matches(line, email)]
            removed_lines += len(lines) - len(kept)
            if len(kept) != len(lines):
                path.write_text("".join(kept), encoding="utf-8")
        archived: list[str] = []
        if request.include_session:
            sessions = Path(self.config.workflow("output").get("directory") or "sessions")
            if not sessions.is_absolute():
                sessions = Path(__file__).resolve().parent.parent / sessions
            archive = sessions / "_deleted"
            for path in sessions.glob("session_*.json") if sessions.is_dir() else ():
                try:
                    import json
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, Mapping) and str(value.get("email") or "").lower() == email.lower():
                        archive.mkdir(parents=True, exist_ok=True)
                        target = archive / path.name
                        path.replace(target)
                        archived.append(str(target))
                except Exception:
                    continue
        return AccountDeleteResult(email, removed_lines, removed_rows, tuple(archived))

    def _configured_mailbox_files(self) -> tuple[str, ...]:
        email_cfg = self.config.workflow("email_registration")
        candidates: list[str] = []
        for key in ("token_file", "mailbox_file", "chatai_mailbox_file"):
            value = email_cfg.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        for value in email_cfg.get("pool_files", ()) if isinstance(email_cfg.get("pool_files"), (list, tuple)) else ():
            if str(value).strip():
                candidates.append(str(value).strip())
        root = self.config.source.parent if self.config.source.name != "<injected>" else Path.cwd()
        resolved: list[str] = []
        for value in candidates:
            path = Path(value).expanduser()
            resolved.append(str(path if path.is_absolute() else root / path))
        return tuple(dict.fromkeys(resolved))

    @staticmethod
    def _mailbox_line_matches(line: str, email: str) -> bool:
        normalized = line.strip()
        if not normalized:
            return False
        prefix = email.casefold()
        first = normalized.split("----", 1)[0].split("---", 1)[0].split("|", 1)[0].strip()
        return first.casefold() == prefix or first.casefold().startswith(prefix + "-")

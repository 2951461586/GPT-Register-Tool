"""Typed account lifecycle operations shared by CLI and desktop adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
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
        self._database_lock = Lock()
        self._mailbox_locks: dict[str, Lock] = {}
        self._mailbox_locks_lock = Lock()

    def delete_many(
        self,
        requests: Iterable[AccountDeleteRequest],
        *,
        workers: int = 4,
    ) -> list[AccountDeleteResult | Exception]:
        unique: dict[str, AccountDeleteRequest] = {}
        for request in requests:
            email = str(request.email or "").strip()
            if email:
                unique.setdefault(email.casefold(), request)
        if not unique:
            return []
        ordered = list(unique.values())
        results: list[AccountDeleteResult | Exception | None] = [None] * len(ordered)
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(ordered)))) as executor:
            pending = {executor.submit(self.delete, request): index for index, request in enumerate(ordered)}
            for future in as_completed(pending):
                index = pending[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = exc
        return [result for result in results if result is not None]

    def delete(self, request: AccountDeleteRequest) -> AccountDeleteResult:
        email = str(request.email or "").strip()
        if not email:
            raise ValueError("email is required")
        db = database_path(self.config)
        removed_rows = 0
        if db.exists():
            import sqlite3
            with self._database_lock:
                with sqlite3.connect(db) as conn:
                    cursor = conn.execute("DELETE FROM accounts WHERE lower(email)=lower(?)", (email,))
                    removed_rows = max(0, int(cursor.rowcount or 0))
        removed_lines = 0
        mailbox_files = tuple(request.mailbox_files) or self._configured_mailbox_files()
        for raw_path in mailbox_files:
            path = Path(raw_path)
            lock = self._mailbox_lock(path)
            with lock:
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

    def _mailbox_lock(self, path: Path) -> Lock:
        key = str(path.resolve()).casefold()
        with self._mailbox_locks_lock:
            return self._mailbox_locks.setdefault(key, Lock())

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

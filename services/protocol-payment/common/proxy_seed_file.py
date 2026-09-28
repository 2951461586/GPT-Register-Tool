"""Shared seed-file management for the protocol-payment extractors.

Batch 4 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and what is deliberately not
--------------------------------------------
Each extractor owns a ``proxy_seeds.txt`` list that it dedups by sticky-session
identity and, on a definitive proxy failure, rewrites in place while appending
the removed lines to a quarantine ``removed_proxies.jsonl``.  The mechanics --
path resolution, chain-key dedup, atomic rewrite, quarantine append -- are
provider-free.

The *orchestrator* (``load_proxy_seeds``) stays in each extractor: it composes
these primitives with that provider's bootstrap/promotion/provider country
constants and its own log lines, so moving it would only relocate provider
vocabulary.

Why this is not just "move the functions"
-----------------------------------------
The bodies reach for ``SCRIPT_DIR``, the extractor's ``_proxy_file_lock``, its
redaction registry (``register_proxy_for_redaction`` / ``redact_log_text`` /
``proxy_label``) and its ``proxy_chain_key``.  All of those are injected through
:class:`ProxySeedFile`, the same dependency-injection shape ``proxy_state.py``
and ``geo.py`` use.  The redaction callbacks are especially important: a single
shared registry would leak one extractor's seed strings into another's logs.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "ProxySeedFile",
    "proxy_seed_file",
    "remove_failed_proxies",
    "unique_proxy_seeds",
]


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


class ProxySeedFile:
    """One extractor's seed list plus its dedup / removal mechanics.

    Injected knobs (never defaulted):

    ``env_prefix`` / ``base_dir``
        resolve ``<PREFIX>_PROXY_SEED_FILE`` (then the shared
        ``PP_PROXY_SEED_FILE``), else ``<base_dir>/proxy_seeds.txt``.
    ``proxy_chain_key``
        the country-stable identity used to dedup and to match a failing proxy
        against a seed line.
    ``label`` / ``redact`` / ``register``
        the extractor's own redaction trio.  ``register`` is called for every
        failing proxy *before* the file is touched, so a seed that is about to
        be rewritten is already scrubbed from logs.
    ``env_bool``
        the extractor's bool env parser; ``<PREFIX>_PROXY_REMOVE_FAILED`` gates
        the whole feature.
    ``file_lock``
        the extractor's ``_proxy_file_lock`` (distinct from the state lock).
    ``clock`` / ``log``
        quarantine timestamps and the extractor's log sink.
    """

    def __init__(
        self,
        *,
        env_prefix: str,
        base_dir: Path,
        proxy_chain_key: Callable[[str], str],
        label: Callable[[str], str],
        redact: Callable[[str], str],
        register: Callable[[str], None],
        env_bool: Callable[[str, bool], bool],
        log: Callable[..., None] | None = None,
        file_lock: threading.RLock | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._env_prefix = str(env_prefix)
        self._base_dir = Path(base_dir)
        self._proxy_chain_key = proxy_chain_key
        self._label = label
        self._redact = redact
        self._register = register
        self._env_bool = env_bool
        self._log = log or (lambda *_args: None)
        self._file_lock = file_lock if file_lock is not None else threading.RLock()
        self._clock = clock or (lambda: time.time())

    def path(self) -> Path:
        raw = (
            os.environ.get(f"{self._env_prefix}_PROXY_SEED_FILE", "").strip()
            or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
        )
        return Path(raw).expanduser() if raw else self._base_dir / "proxy_seeds.txt"

    def unique(self, proxy_seeds: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        duplicates = 0
        for proxy_seed in proxy_seeds:
            chain_key = self._proxy_chain_key(proxy_seed)
            if not chain_key or chain_key in seen:
                duplicates += 1
                continue
            seen.add(chain_key)
            unique.append(proxy_seed)
        if duplicates:
            self._log(f"代理 Seed 去重: 忽略相同 sticky session {duplicates} 条", "[WARN] ")
        return unique

    def remove_failed(self, group: str, failures: list[tuple[str, str]]) -> int:
        if not failures or not self._env_bool(f"{self._env_prefix}_PROXY_REMOVE_FAILED", True):
            return 0
        for proxy, _reason in failures:
            self._register(proxy)
        path = self.path()
        if not path.is_file():
            return 0
        reasons = {self._proxy_chain_key(proxy): reason for proxy, reason in failures if self._proxy_chain_key(proxy)}
        if not reasons:
            return 0
        with self._file_lock:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            removed = [line for line in lines if self._proxy_chain_key(line) in reasons]
            if not removed:
                return 0
            kept = [line for line in lines if self._proxy_chain_key(line) not in reasons]
            quarantine = self._base_dir / "removed_proxies.jsonl"
            with quarantine.open("a", encoding="utf-8") as handle:
                for line in removed:
                    chain_key = self._proxy_chain_key(line)
                    handle.write(
                        json.dumps(
                            {
                                "time": _coerce_int(self._clock()),
                                "group": group,
                                "proxy": self._label(line.strip()),
                                "reason": self._redact(str(reasons.get(chain_key) or ""))[:300],
                                "source": path.name,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            temp_path = path.with_name(f".{path.name}.tmp")
            temp_path.write_text("".join(kept), encoding="utf-8")
            os.replace(temp_path, path)
            return len(removed)


# -- free-function surface --------------------------------------------------


def proxy_seed_file(seed_file: ProxySeedFile) -> Path:
    return seed_file.path()


def unique_proxy_seeds(seed_file: ProxySeedFile, proxy_seeds: list[str]) -> list[str]:
    return seed_file.unique(proxy_seeds)


def remove_failed_proxies(seed_file: ProxySeedFile, group: str, failures: list[tuple[str, str]]) -> int:
    return seed_file.remove_failed(group, failures)

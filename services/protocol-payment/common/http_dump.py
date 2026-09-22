"""HTTP response/request dumping shared by the protocol-payment extractors.

Batch 4 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

Scope: ideal and twint ONLY
---------------------------
`dump_http` exists in three extractors but is **not** the same function three
times.  blik differs in two behaviour-affecting ways and is therefore left
alone:

  * blik's guard is ``if not env_bool("IDEAL_DUMP", False)`` -- it never reads
    the ``force`` argument.  ideal and twint read ``force or env_bool(...)``.
    blik has live callers passing ``force=True`` (``dump_http(resp,
    "ideal_confirm", ..., force=True)``) that today dump nothing unless
    ``IDEAL_DUMP`` is set.  Folding blik into a force-honouring shared function
    would start writing dump files where none were written before -- a
    behaviour change, not a refactor.
  * blik calls ``DUMP_DIR.mkdir(...)`` inside the function; ideal and twint do
    it at import time.

So this module carries the ideal/twint shape and says so.

Injected knobs (all of them would be wrong to share)
----------------------------------------------------
  ``dump_env``   IDEAL_DUMP vs TWINT_DUMP.  No default, for the same reason
                 ``load_token(env_names=...)`` has none: a default would pick
                 one provider's switch for both.
  ``dump_dir``   each extractor writes to its own ``SCRIPT_DIR/dumps``.
  ``counter``    per-extractor dump index, see ``DumpCounter``.
  ``redact_text`` per-extractor redaction registry.
  ``env_bool``   injected so callers keep their own env parsing.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from threading import RLock
from typing import Any, Callable

__all__ = [
    "DumpCounter",
    "dump_http",
]

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class DumpCounter:
    """Per-extractor dump index, replacing the old ``_dump_counter`` global pair.

    Each extractor owns one instance.  They must NOT be shared: dumps are
    numbered per extractor inside that extractor's own ``dumps/`` directory, so
    a shared counter would make the filenames of one provider depend on how
    many dumps another provider had written.

    The lock lives here rather than in ``dump_http`` so the increment and the
    read stay atomic as one operation -- the original did both while holding
    ``_dump_lock``.
    """

    def __init__(self) -> None:
        self._value = 0
        self._lock = RLock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        """Current index, for tests.  Not used by ``dump_http``."""
        return self._value


def dump_http(
    response: Any | None,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
    *,
    dump_env: str,
    dump_dir: Path,
    counter: DumpCounter,
    redact_text: Callable[[str], str],
    env_bool: Callable[[str, bool], bool],
    strftime: Callable[[str], str] | None = None,
) -> None:
    """Write a request/response dump when dumping is on (or ``force``).

    Bodies are copied from the ideal/twint originals character for character:
    the same ``lines`` layout, the same ``json.dumps`` settings, the same
    ``write_text`` call.  Differential verification can only prove "unchanged"
    if nothing changed.

    ``strftime`` is injectable purely so a test can pin the timestamp; the
    default resolves ``time.strftime`` at call time (never bind it as a default
    argument -- see ``file_loading`` for why that pattern is a trap).
    """
    if strftime is None:
        strftime = time.strftime

    if not force and not env_bool(dump_env, False):
        return

    index = counter.next()
    name = f"{strftime('%Y%m%d-%H%M%S')}_{index:04d}_{stage}.txt"
    path = dump_dir / _SAFE_NAME_RE.sub("_", name)
    lines = [
        f"stage: {stage}",
        f"request: {request_method} {request_url}",
        "",
        "request_body:",
        redact_text(json.dumps(request_body, ensure_ascii=False, indent=2) if request_body is not None else ""),
        "",
    ]
    if response is not None:
        lines.extend(
            [
                f"status: {response.status_code}",
                f"url: {response.url}",
                "",
                "response:",
                redact_text(response.text),
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")

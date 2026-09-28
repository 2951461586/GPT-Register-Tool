"""Record / replay transport for the protocol-payment extractors (stage-2 harness).

Why this exists
---------------
Stage 2 parameterises the 23 functions where ``ideal`` and ``twint`` genuinely
differ.  The 2026-09-28 audit (``docs/audits/plan-2026-09-28-stage2-replay.md``)
found most of those differences are provider *data* or *log wording*; only three
change an HTTP request body.  The gate for any such change is therefore "same
request stream, same final result", proven against recorded real traffic.

This module is that gate's transport.  It never touches ``services/`` job code
(Rule 10): the extractor keeps calling its own ``new_session`` /
``build_chatgpt_session``, and the test patches ``new_session`` to hand back
either a recording or a replay session.

Two levels of matching
---------------------
* **Replay matching** (driving the flow) is *ordered* and loose -- method + URL
  path only, because the recorded values of client-generated ids differ from the
  frozen ones.  A mismatch or exhaustion is a hard failure: it means the code
  asked for something the recording does not contain.
* **Cross-run fingerprint** (proving HEAD == parameterised) is *exact* -- method,
  canonical URL and canonical body.  ``freeze_determinism`` makes every
  client-side value reproducible, so any difference here is a real behaviour
  change, not noise.

``dump_http`` is deliberately left alone: it is an operator-facing diagnostic
and is lossy (no request headers, no cookies, no redirect chain).  Replay needs
the full exchange.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import random
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "RecordedExchange",
    "RecordingError",
    "ReplayCursor",
    "ReplayExhausted",
    "ReplayMismatch",
    "ReplayResponse",
    "ReplaySession",
    "RecordingSession",
    "canonical_body",
    "canonical_url",
    "freeze_determinism",
    "install_recording",
    "install_replay",
    "load_recording",
    "request_fingerprint",
    "save_recording",
]


class RecordingError(RuntimeError):
    """Raised for malformed recording files."""


class ReplayMismatch(AssertionError):
    """The code requested a different endpoint than the recording holds."""


class ReplayExhausted(AssertionError):
    """The code made more requests than the recording contains."""


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EPOCH_RE = re.compile(r"\b1[6-9]\d{8}(?:\d{1,3})?\b")


def _mask(value: str) -> str:
    value = _UUID_RE.sub("<uuid>", value)
    return _EPOCH_RE.sub("<ts>", value)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        return _mask(value)
    return value


def canonical_url(url: Any) -> str:
    """Scheme + host + path + *sorted* query, no fragment."""
    parts = urlsplit(str(url or ""))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def canonical_body(value: Any) -> str:
    """Deterministic JSON for a form-mapping, a JSON body or a plain value."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        items = sorted(((str(key), _canonical_value(val)) for key, val in value.items()), key=lambda kv: kv[0])
        return json.dumps(dict(items), ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (list, tuple)) and len(item) == 2 for item in value):
            return json.dumps([[str(a), _canonical_value(b)] for a, b in value], ensure_ascii=False)
        return json.dumps([_canonical_value(item) for item in value], ensure_ascii=False)
    return json.dumps(_canonical_value(value), ensure_ascii=False)


def request_fingerprint(method: Any, url: Any, kwargs: Mapping[str, Any] | None = None) -> tuple[str, str, str]:
    """Exact ``(method, canonical_url, canonical_body)`` for cross-run comparison."""
    kwargs = kwargs or {}
    body = kwargs.get("json") if kwargs.get("json") is not None else kwargs.get("data")
    return (str(method).upper(), canonical_url(url), canonical_body(body))


@dataclass
class ReplayResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    url: str = ""

    @property
    def text(self) -> str:
        return self.body

    @property
    def content(self) -> bytes:
        return self.body.encode("utf-8")

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 400

    @property
    def reason(self) -> str:
        return ""

    def json(self) -> Any:
        return json.loads(self.body) if self.body else {}


@dataclass
class RecordedExchange:
    method: str
    url: str
    response: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecordedExchange":
        request = value.get("request") or {}
        response = value.get("response") or {}
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            raise RecordingError("exchange must carry request/response objects")
        return cls(str(request.get("method") or ""), str(request.get("url") or ""), dict(response))


class ReplayCursor:
    """Shared ordered cursor: one recording, many sessions, one stream.

    ``strict`` (default) turns a method/URL mismatch into a failure; tests that
    only care about *how many* requests were made can switch it off.
    """

    def __init__(self, exchanges: Iterable[RecordedExchange], *, strict: bool = True) -> None:
        self._exchanges = list(exchanges)
        self._index = 0
        self.strict = strict
        self.served: list[tuple[str, str, str]] = []

    def next(self, method: Any, url: Any, kwargs: Mapping[str, Any] | None = None) -> ReplayResponse:
        if self._index >= len(self._exchanges):
            raise ReplayExhausted(
                f"recording exhausted after {self._index} requests; code still requested {method} {url}"
            )
        exchange = self._exchanges[self._index]
        if self.strict and (
            str(exchange.method).upper() != str(method).upper() or canonical_url(exchange.url) != canonical_url(url)
        ):
            raise ReplayMismatch(
                f"request #{self._index}: expected {exchange.method} {exchange.url}, got {method} {url}"
            )
        self._index += 1
        self.served.append(request_fingerprint(method, url, kwargs))
        response = exchange.response
        return ReplayResponse(
            status_code=int(response.get("status_code") or 0),
            headers=dict(response.get("headers") or {}),
            body=str(response.get("body") or ""),
            url=str(response.get("url") or url),
        )

    @property
    def consumed(self) -> int:
        return self._index

    @property
    def remaining(self) -> int:
        return len(self._exchanges) - self._index


class ReplaySession:
    """Minimal session stand-in that serves recorded responses in order."""

    def __init__(self, cursor: ReplayCursor, headers: Mapping[str, str] | None = None) -> None:
        self._cursor = cursor
        self.headers: dict[str, str] = dict(headers or {})
        self.cookies: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = False

    def request(self, method: str, url: str, **kwargs: Any) -> ReplayResponse:
        return self._cursor.next(method, url, kwargs)

    def get(self, url: str, **kwargs: Any) -> ReplayResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> ReplayResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> ReplayResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> ReplayResponse:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> ReplayResponse:
        return self.request("HEAD", url, **kwargs)

    def close(self) -> None:
        return None


class RecordingSession:
    """Transparent wrapper that appends full exchanges to ``sink``."""

    def __init__(self, session: Any, sink: list[dict[str, Any]]) -> None:
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_sink", sink)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_session"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_session"), name, value)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        session = object.__getattribute__(self, "_session")
        response = session.request(method, url, **kwargs)
        object.__getattribute__(self, "_sink").append(
            {
                "request": {
                    "method": str(method).upper(),
                    "url": str(url),
                    "headers": dict(getattr(session, "headers", {}) or {}),
                    "cookies": dict(getattr(session, "cookies", {}) or {}),
                    "data": kwargs.get("data") if isinstance(kwargs.get("data"), (dict, list)) else None,
                    "json": kwargs.get("json"),
                },
                "response": {
                    "status_code": int(getattr(response, "status_code", 0) or 0),
                    "headers": dict(getattr(response, "headers", {}) or {}),
                    "body": str(getattr(response, "text", "") or ""),
                    "url": str(getattr(response, "url", url) or url),
                },
            }
        )
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Any:
        return self.request("HEAD", url, **kwargs)


def load_recording(path: str | Path) -> list[RecordedExchange]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        data = data.get("exchanges") or []
    if not isinstance(data, list):
        raise RecordingError(f"recording must be a list of exchanges: {path}")
    return [RecordedExchange.from_dict(item) for item in data]


def save_recording(path: str | Path, exchanges: Iterable[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": Windows text mode would write CRLF and make the fixture
    # differ across machines (same lesson as the parity baseline).
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(list(exchanges), ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def install_replay(module: Any, cursor: ReplayCursor):
    """Patch ``module.new_session`` so every session replays from ``cursor``."""
    return patch.object(module, "new_session", lambda proxy="", use_pre_proxy=True: ReplaySession(cursor))


def install_recording(module: Any, sink: list[dict[str, Any]]):
    """Patch ``module.new_session`` so every session records into ``sink``."""
    real_factory = module.new_session

    def factory(proxy: str = "", use_pre_proxy: bool = True) -> RecordingSession:
        return RecordingSession(real_factory(proxy, use_pre_proxy), sink)

    return patch.object(module, "new_session", factory)


@contextlib.contextmanager
def freeze_determinism(*, start_time: float = 1_700_000_000.0, seed: int = 0):
    """Make every client-side value reproducible across two runs.

    Patches ``uuid.uuid4``, the ``random`` draw functions, and
    ``time.time`` / ``time.strftime`` / ``time.sleep``.  ``sleep`` becomes a
    no-op so poll/approve loops do not burn real seconds; the recording's
    responses are what terminate those loops.
    """
    counter = itertools.count(1)

    def _uuid4() -> uuid.UUID:
        return uuid.UUID(int=next(counter))

    rng = random.Random(seed)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(uuid, "uuid4", _uuid4))
        stack.enter_context(patch.object(random, "randint", lambda a, b: rng.randint(a, b)))
        stack.enter_context(patch.object(random, "choice", lambda seq: rng.choice(seq)))
        stack.enter_context(patch.object(random, "uniform", lambda a, b: rng.uniform(a, b)))
        stack.enter_context(patch.object(random, "random", lambda: rng.random()))
        stack.enter_context(patch.object(time, "time", lambda: start_time))
        stack.enter_context(patch.object(time, "strftime", lambda fmt: "20260101-000000"))
        stack.enter_context(patch.object(time, "sleep", lambda _seconds: None))
        yield

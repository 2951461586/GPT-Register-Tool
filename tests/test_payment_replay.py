"""Unit tests for the stage-2 record/replay harness (``tests/payment_replay.py``).

The harness is a single point of failure for stage 2: if it over-normalises, a
real behaviour change looks "identical"; if it under-normalises, frozen
nondeterminism looks like drift.  These tests pin both directions and the
record->replay round trip, so a broken predicate cannot pass vacuously.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import payment_replay as PR  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, body="{}", url="https://api.stripe.com/v1/x", headers=None):
        self.status_code = status_code
        self._body = body
        self.url = url
        self.headers = headers or {"content-type": "application/json"}

    @property
    def text(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.headers = {"User-Agent": "ua"}
        self.cookies = {"a": "b"}
        self.proxies = {}
        self.trust_env = True
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def test_canonical_url_sorts_query_and_drops_fragment():
    assert PR.canonical_url("HTTPS://Api.Stripe.com/v1/x?b=2&a=1#frag") == "https://api.stripe.com/v1/x?a=1&b=2"


def test_canonical_body_sorts_keys_and_masks_dynamic_values():
    body = {"z": 1, "a": "id 11111111-1111-1111-1111-111111111111", "t": "1700000000"}
    out = PR.canonical_body(body)
    assert '"a": "id <uuid>"' in out
    assert '"t": "<ts>"' in out
    assert out.index('"a"') < out.index('"t"') < out.index('"z"')


def test_canonical_body_keeps_form_lists_in_order():
    form = [("b", "2"), ("a", "1"), ("a", "3")]
    assert PR.canonical_body(form) == '[["b", "2"], ["a", "1"], ["a", "3"]]'


def test_request_fingerprint_prefers_json_over_data():
    fp = PR.request_fingerprint("post", "https://h/p", {"json": {"b": 1, "a": 2}, "data": {"ignored": 1}})
    assert fp[0] == "POST"
    assert fp[1] == "https://h/p"
    assert fp[2] == '{"a": 2, "b": 1}'


# --------------------------------------------------------------------------
# record -> replay round trip
# --------------------------------------------------------------------------


def test_recording_session_captures_and_replays_in_order(tmp_path):
    responses = [FakeResponse(body='{"id": "pm_1"}'), FakeResponse(status_code=201, body='{"id": "pm_2"}')]
    sink: list[dict] = []
    wrapped = PR.RecordingSession(FakeSession(list(responses)), sink)
    wrapped.headers.update({"X-Test": "1"})
    wrapped.proxies = {"http": "http://p"}
    assert wrapped.request("POST", "https://api.stripe.com/v1/a", data={"k": "v"}).status_code == 200
    assert wrapped.request("POST", "https://api.stripe.com/v1/b", data={"k": "w"}).status_code == 201
    assert wrapped.proxies == {"http": "http://p"}
    assert wrapped.headers["X-Test"] == "1"
    assert len(sink) == 2

    path = tmp_path / "scenario.json"
    PR.save_recording(path, sink)
    assert b"\r\n" not in path.read_bytes()
    cursor = PR.ReplayCursor(PR.load_recording(path))
    session = PR.ReplaySession(cursor)
    first = session.post("https://api.stripe.com/v1/a", data={"k": "v"})
    second = session.post("https://api.stripe.com/v1/b", data={"k": "w"})
    assert (first.status_code, first.json()) == (200, {"id": "pm_1"})
    assert (second.status_code, second.json()) == (201, {"id": "pm_2"})
    assert cursor.remaining == 0
    assert cursor.served[0] == PR.request_fingerprint("POST", "https://api.stripe.com/v1/a", {"data": {"k": "v"}})


def test_replay_strict_mismatch_raises():
    sink = [
        {
            "request": {"method": "POST", "url": "https://h/a"},
            "response": {"status_code": 200, "body": "{}"},
        }
    ]
    cursor = PR.ReplayCursor(PR.load_recording(_write(tmp_file(sink))))
    with pytest.raises(PR.ReplayMismatch):
        cursor.next("GET", "https://h/a", {})


def test_replay_exhaustion_raises():
    cursor = PR.ReplayCursor([])
    with pytest.raises(PR.ReplayExhausted):
        cursor.next("GET", "https://h/a", {})


def test_replay_non_strict_serves_any_endpoint():
    sink = [
        {
            "request": {"method": "POST", "url": "https://h/a"},
            "response": {"status_code": 204, "body": ""},
        }
    ]
    cursor = PR.ReplayCursor(PR.load_recording(_write(tmp_file(sink))), strict=False)
    assert cursor.next("GET", "https://h/other", {}).status_code == 204


# --------------------------------------------------------------------------
# determinism freezing
# --------------------------------------------------------------------------


def test_freeze_determinism_is_reproducible():
    with PR.freeze_determinism():
        first = (str(uuid.uuid4()), __import__("random").randint(1, 10), time.time(), time.strftime("%Y"))
        __import__("time").sleep(5)
    with PR.freeze_determinism():
        second = (str(uuid.uuid4()), __import__("random").randint(1, 10), time.time(), time.strftime("%Y"))
    assert first == second
    assert first[2] == 1_700_000_000.0
    assert first[3] == "20260101-000000"


# --------------------------------------------------------------------------
# install_* patching
# --------------------------------------------------------------------------


def test_install_replay_patches_new_session():
    class Mod:
        def new_session(self, proxy="", use_pre_proxy=True):
            raise AssertionError("real session must not be built during replay")

        def build(self):
            return self.new_session("p")

    module = Mod()
    cursor = PR.ReplayCursor([])
    with PR.install_replay(module, cursor):
        assert isinstance(module.build(), PR.ReplaySession)


def test_install_recording_wraps_the_real_factory():
    from typing import Any

    class Mod:
        def new_session(self, proxy: str = "", use_pre_proxy: bool = True) -> Any:
            return FakeSession([FakeResponse()])

    module = Mod()
    sink: list[dict] = []
    with PR.install_recording(module, sink):
        session = module.new_session("p")
        session.post("https://h/a", data={"k": "v"})
    assert isinstance(session, PR.RecordingSession)
    assert len(sink) == 1


# -- small helpers ----------------------------------------------------------


def tmp_file(payload):
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


def _write(path: Path) -> Path:
    return path

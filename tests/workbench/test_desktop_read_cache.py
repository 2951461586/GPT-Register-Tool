"""Row-cache properties of the resident desktop read path.

Why this exists
---------------
Two caches hold every parsed and sanitized session for the resident channel. They
were bounded by entry count but not by *memory shape*:

* the key was the raw ``raw_json`` string, so a full cache pinned a second copy of
  every session (5–20 KB each) on top of the parsed trees — the largest
  process-lifetime allocation in the package, and it was in no inventory;
* overflow called ``clear()``, so one entry past the bound re-parsed the whole
  pool on the next refresh — a latency spike exactly when the pool was largest.

These tests pin the fix: digest keys, LRU eviction, and a hit that does not
re-parse or re-sanitize.
"""

from __future__ import annotations

import json

import pytest

from sms_tool import desktop_read


@pytest.fixture(autouse=True)
def _clean_caches():
    desktop_read._SESSION_PARSE_CACHE.clear()
    desktop_read._SESSION_SANITIZE_CACHE.clear()
    yield
    desktop_read._SESSION_PARSE_CACHE.clear()
    desktop_read._SESSION_SANITIZE_CACHE.clear()


def test_the_cache_key_does_not_retain_the_raw_json():
    payload = json.dumps({"access_token": "at_secret", "pad": "x" * 5000})
    desktop_read._parsed_session(payload)
    keys = list(desktop_read._SESSION_PARSE_CACHE)
    assert keys and all(isinstance(key, bytes) for key in keys)
    assert all(key != payload for key in keys)
    assert len(keys[0]) == 16, "digest keys must stay small and fixed-size"


def test_a_hit_does_not_reparse(monkeypatch):
    calls: list[str] = []
    real_loads = json.loads

    def counting_loads(text, *args, **kwargs):
        calls.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(desktop_read.json, "loads", counting_loads)
    payload = json.dumps({"a": 1})
    first = desktop_read._parsed_session(payload)
    second = desktop_read._parsed_session(payload)
    assert first is second
    assert len(calls) == 1


def test_a_hit_does_not_resanitize(monkeypatch):
    calls: list[object] = []

    def counting_sanitize(value):
        calls.append(value)
        return {"sanitized": True}

    monkeypatch.setattr(desktop_read, "sanitize", counting_sanitize)
    payload = json.dumps({"a": 1})
    session = desktop_read._parsed_session(payload)
    desktop_read._sanitized_session(payload, session)
    desktop_read._sanitized_session(payload, session)
    assert len(calls) == 1


def test_overflow_evicts_the_oldest_entry_instead_of_clearing(monkeypatch):
    monkeypatch.setattr(desktop_read, "_SESSION_CACHE_LIMIT", 3)
    for index in range(4):
        desktop_read._parsed_session(json.dumps({"n": index}))

    assert len(desktop_read._SESSION_PARSE_CACHE) == 3
    oldest = desktop_read._session_cache_key(json.dumps({"n": 0}))
    kept = desktop_read._session_cache_key(json.dumps({"n": 1}))
    assert oldest not in desktop_read._SESSION_PARSE_CACHE, "the oldest entry must be the one evicted"
    assert kept in desktop_read._SESSION_PARSE_CACHE


def test_lookup_promotes_an_entry_to_the_newest_position(monkeypatch):
    monkeypatch.setattr(desktop_read, "_SESSION_CACHE_LIMIT", 2)
    a, b = json.dumps({"n": "a"}), json.dumps({"n": "b"})
    desktop_read._parsed_session(a)
    desktop_read._parsed_session(b)
    desktop_read._parsed_session(a)  # promote a
    desktop_read._parsed_session(json.dumps({"n": "c"}))  # evicts b, not a

    key_a = desktop_read._session_cache_key(a)
    key_b = desktop_read._session_cache_key(b)
    assert key_a in desktop_read._SESSION_PARSE_CACHE
    assert key_b not in desktop_read._SESSION_PARSE_CACHE

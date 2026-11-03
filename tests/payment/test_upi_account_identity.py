"""P0-1: the UPI lane must present the *account's* identity, not a fresh UUID.

Background
----------
``auth_context`` has been listed in ``UPI_CALL_OPTIONS`` and forwarded by every
entry point since the UPI lane was added, but no stage ever read it:
``pipeline._resolve_upi_runtime`` minted ``uuid.uuid4()`` for the device id and
left ``session_token`` empty.  Each run therefore presented a brand-new device
plus a synthetic ``Cookie: oai-did=<uuid>``.

That regression is invisible to the functional suite (the flow still returns a
link), so it is pinned here from four angles:

1. ``identity.resolve_account_identity`` -- the pure merge rules;
2. ``pipeline._resolve_upi_runtime`` -- the dead parameter is now read;
3. ``session._upi_new_chatgpt_session`` -- the account cookie replaces the
   synthetic ``oai-did``;
4. an AST ratchet -- ``auth_context`` is referenced *inside* the stage body, so
   re-orphaning the parameter fails the suite instead of silently regressing.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest

from sms_tool.payment import upi_identity as identity
from sms_tool.upi_link import pipeline, session
from sms_tool.payment.upi_identity import (
    AccountIdentity,
    cookie_pairs,
    cookie_value,
    resolve_account_identity,
    with_cookie,
)

ROOT = Path(__file__).resolve().parents[2]
UPI_PACKAGE = ROOT / "sms_tool" / "upi_link"

#: The canonical shape ``payment/qualification.py:_auth_context`` produces.
ACCOUNT_CONTEXT = {
    "device_id": "11111111-2222-3333-4444-555555555555",
    "oai_did": "11111111-2222-3333-4444-555555555555",
    "cookie_header": (
        "oai-did=11111111-2222-3333-4444-555555555555; "
        "__Secure-next-auth.session-token=eyJhbGciOi.session; "
        "_cfuvid=cf-1"
    ),
}


# ── 1. pure merge rules ─────────────────────────────────────────────────────
def test_cookie_parsing_accepts_header_string_and_mapping():
    header = 'a=1; b=2 ; c="3"'
    assert cookie_pairs(header) == [("a", "1"), ("b", "2"), ("c", "3")]
    assert cookie_value(header, "B") == "2"
    assert cookie_value({"a": "1", "b": "2"}, "b") == "2"
    assert cookie_value([{"name": "b", "value": "2"}], "b") == "2"
    assert cookie_value(header, "missing") == ""


def test_with_cookie_replaces_in_place_and_appends_when_absent():
    assert with_cookie("a=1; b=2", "b", "9") == "a=1; b=9"
    assert with_cookie("a=1", "b", "9") == "a=1; b=9"
    # A no-op keeps the account's own byte order.
    assert with_cookie("a=1; b=2", "b", "2") == "a=1; b=2"


def test_identity_is_empty_when_nothing_is_supplied():
    """No credential is ever invented: absent stays absent."""
    assert resolve_account_identity(None) == AccountIdentity()
    assert resolve_account_identity({}) == AccountIdentity()


def test_identity_reads_the_canonical_auth_context():
    resolved = resolve_account_identity(ACCOUNT_CONTEXT)
    assert resolved.device_id == ACCOUNT_CONTEXT["device_id"]
    # The token travels *inside* the forwarded cookie, so the field stays empty
    # (callers append that field to the same header -- a second copy would be a
    # duplicate credential).
    assert resolved.session_token == ""
    assert identity.session_token_from_cookie(resolved.cookie_header) == "eyJhbGciOi.session"
    assert "_cfuvid=cf-1" in resolved.cookie_header
    assert resolved.has_cookie is True


def test_cookie_evidence_wins_over_a_stale_explicit_device_id():
    """The Sentinel pair is device-bound, so the cookie's ``oai-did`` decides."""
    context = {**ACCOUNT_CONTEXT, "cookie_header": "oai-did=cookie-did; _cfuvid=cf-1"}
    resolved = resolve_account_identity(context, device_id="explicit-did")
    assert resolved.device_id == "cookie-did"
    assert "oai-did=cookie-did" in resolved.cookie_header
    assert "explicit-did" not in resolved.cookie_header


def test_cookie_without_device_id_gets_the_device_appended():
    context = {"device_id": "did-1", "cookie_header": "_cfuvid=cf-1"}
    resolved = resolve_account_identity(context)
    assert resolved.device_id == "did-1"
    assert resolved.cookie_header == "_cfuvid=cf-1; oai-did=did-1"


def test_session_token_is_written_into_the_cookie_header():
    resolved = resolve_account_identity({"session_token": "tok-1", "cookie_header": "_cfuvid=cf-1"})
    assert resolved.session_token == "tok-1"
    assert cookie_value(resolved.cookie_header, identity.COOKIE_SESSION_TOKEN) == "tok-1"


def test_account_id_prefers_account_id_over_email():
    assert resolve_account_identity({"account_id": "acc", "email": "a@b.c"}).account_id == "acc"
    assert resolve_account_identity({"email": "a@b.c"}).account_id == "a@b.c"


# ── 2. the runtime resolves the identity ────────────────────────────────────
def _resolve(auth_context=None, *, device_id="", session_token=""):
    return pipeline._resolve_upi_runtime(
        access_token="at",
        proxy="",
        checkout_proxy="http://p",
        provider_proxy="",
        approve_proxy="",
        target_country=None,
        checkout_country=None,
        payment_country=None,
        require_zero=None,
        runtime_config={"upi": {"billing_regions": ["IN"]}},
        device_id=device_id,
        session_token=session_token,
        auth_context=auth_context,
    )


def test_runtime_uses_the_auth_context_device_and_session_token():
    """The regression this whole change exists for: the parameter was ignored."""
    rt = _resolve(ACCOUNT_CONTEXT)
    assert rt.device_id == ACCOUNT_CONTEXT["device_id"]
    assert identity.session_token_from_cookie(rt.cookie_header) == "eyJhbGciOi.session"
    assert rt.cookie_header.startswith("oai-did=")
    assert rt.account_id == ""


def test_runtime_still_falls_back_to_a_fresh_uuid_without_a_context():
    """No account context keeps the historical behaviour (documented fallback)."""
    rt = _resolve(None)
    uuid.UUID(rt.device_id)  # raises if it is not a UUID
    assert rt.session_token == ""
    assert rt.cookie_header == ""


def test_runtime_explicit_arguments_still_win_over_nothing():
    rt = _resolve(None, device_id="explicit", session_token="tok")
    assert rt.device_id == "explicit"
    assert rt.session_token == "tok"


# ── 3. the session presents the account cookie ──────────────────────────────
class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


@pytest.fixture
def fake_transport(monkeypatch):
    monkeypatch.setattr(session, "_new_session", lambda proxy: _FakeSession())
    # The real upgrade branch only fires for genuine ``requests.Session``
    # instances, so a local fake already stays offline.
    return _FakeSession


def test_session_presents_the_account_cookie_instead_of_a_synthetic_one(fake_transport):
    built = session._upi_new_chatgpt_session(
        "http://proxy:1",
        {"user_agent": "UA", "locale": "en-IN"},
        ACCOUNT_CONTEXT["device_id"],
        "eyJhbGciOi.session",
        ACCOUNT_CONTEXT["cookie_header"],
    )
    cookie = built.headers["Cookie"]
    assert "_cfuvid=cf-1" in cookie
    assert cookie_value(cookie, identity.COOKIE_SESSION_TOKEN) == "eyJhbGciOi.session"
    # The synthetic header used to be exactly this, and nothing else:
    assert cookie != f"oai-did={ACCOUNT_CONTEXT['device_id']}"
    assert built.headers["oai-device-id"] == ACCOUNT_CONTEXT["device_id"]


def test_session_keeps_the_synthetic_cookie_when_no_account_cookie_exists(fake_transport):
    built = session._upi_new_chatgpt_session("", {"user_agent": "UA"}, "did-1", "tok-1")
    assert cookie_value(built.headers["Cookie"], "oai-did") == "did-1"
    assert cookie_value(built.headers["Cookie"], identity.COOKIE_SESSION_TOKEN) == "tok-1"


def test_session_aligns_a_conflicting_cookie_device_id(fake_transport):
    built = session._upi_new_chatgpt_session("", {}, "did-1", "", "oai-did=other; keep=me")
    cookie = built.headers["Cookie"]
    assert cookie_value(cookie, "oai-did") == "did-1"
    assert "keep=me" in cookie


# ── 4. ratchet: the parameter cannot be orphaned again ─────────────────────
def _stage_function() -> ast.FunctionDef:
    tree = ast.parse((UPI_PACKAGE / "stages.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_upi_qr_link_once":
            return node
    raise AssertionError("run_upi_qr_link_once not found")


def test_stage_body_actually_reads_auth_context():
    """``auth_context`` must be *used*, not just declared and forwarded.

    Before P0-1 it appeared exactly twice in the whole package: as a parameter
    and in a forwarding call -- i.e. every caller believed it was forwarding the
    account identity while the lane ran on a random UUID.
    """
    func = _stage_function()
    reads = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and node.id == "auth_context" and isinstance(node.ctx, ast.Load)
    ]
    assert reads, "run_upi_qr_link_once declares auth_context but never reads it"


def test_every_session_builder_call_forwards_the_cookie_header():
    """Both ChatGPT sessions (checkout + approve) must carry the account cookie."""
    source = (UPI_PACKAGE / "stages.py").read_text(encoding="utf-8")
    calls = re.findall(r"ops\._upi_new_chatgpt_session\((.*?)\n\s*\)", source, re.S)
    assert len(calls) == 2, calls
    for call in calls:
        assert "state.cookie_header" in call, call


# ── 5. chunked next-auth session cookie (measured on real accounts) ─────────
CHUNKED_COOKIE = (
    "_cfuvid=cf-1; oai-did=11111111-2222-3333-4444-555555555555; "
    "__Secure-next-auth.session-token.0=eyJhbGciOi.part0; "
    "__Secure-next-auth.session-token.1=part1.sig"
)


def test_chunked_session_token_is_joined_in_order():
    assert "part0" in identity.session_token_from_cookie(CHUNKED_COOKIE)
    assert identity.session_token_from_cookie(CHUNKED_COOKIE).startswith("eyJhbGciOi.part0")
    assert identity.session_token_from_cookie(CHUNKED_COOKIE).endswith("part1.sig")


def test_a_cookie_that_already_carries_the_token_does_not_get_a_second_copy():
    resolved = resolve_account_identity({"cookie_header": CHUNKED_COOKIE}, session_token="explicit-token")
    # The header is forwarded verbatim: no plain `__Secure-next-auth.session-token=`
    # entry is added next to the chunks, and no chunk is rewritten.
    assert resolved.cookie_header == CHUNKED_COOKIE
    assert "__Secure-next-auth.session-token.0=eyJhbGciOi.part0" in resolved.cookie_header


def test_session_token_is_reported_from_the_cookie_when_chunked():
    resolved = resolve_account_identity({"cookie_header": CHUNKED_COOKIE})
    assert resolved.session_token == ""


def test_session_builder_does_not_duplicate_a_chunked_token(fake_transport):
    built = session._upi_new_chatgpt_session(
        "",
        {"user_agent": "UA"},
        "did-1",
        "explicit-token",
        CHUNKED_COOKIE,
    )
    cookie = built.headers["Cookie"]
    assert "__Secure-next-auth.session-token.1" in cookie
    assert "explicit-token" not in cookie

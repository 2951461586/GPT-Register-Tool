"""Batch-10 differential: ``common/session.py`` vs the pre-merge bodies.

Stage 1 (batch 10) of
``docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md``
moved ``new_session`` (19 lines) and ``fetch_redirect_page`` (14 lines) out of the
three QR extractors. The consolidation plan (§5) forbids accepting an extraction
on "the diff looks right": the caller keeps seams, so the extraction has to be
driven with the same inputs on both sides and compared.

What this file compares
-----------------------
``tests/payment/stage1_batch10_head.py`` holds the **pre-merge** bodies, captured
verbatim apart from the mechanical seams transformation (the same technique as
``tests/payment/stage2_checkout_session_head.py``). Each module under test then
runs its new thin wrapper with the *same* inputs and the *same* fakes, and every
observable is compared:

* the session's ``proxies`` / ``trust_env`` and the order in which the redaction
  registry and ``set_proxy`` were touched;
* the exception type **and message** on the two pre-proxy error paths (operators
  read those strings);
* for ``fetch_redirect_page``: the returned URL list, the hop count and the
  ``dump_http`` stage names.

The extraction is only safe while those agree, so a future edit that drops a seam
(blik's ``IDEAL_USE_LOCAL_PROXY_ONLY`` gate is the one real difference) fails here
instead of on a live link. ``test_the_blik_gate_is_still_live`` is the mutation
proof: it asserts the two gate positions produce *different* sessions, so the
comparison above cannot pass vacuously.

No network, no proxy and no payment session is started.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage1_batch10_head as HEAD  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "services" / "protocol-payment"

PROVIDERS = {
    "ideal": ("ideal", "ideal_qr_extract"),
    "twint": ("twint", "twint_extract"),
    "blik": ("blik", "blik_qr_extract"),
}

FOLLOW_FLAG = {"ideal": "IDEAL_FOLLOW_REDIRECT", "twint": "TWINT_FOLLOW_REDIRECT", "blik": "IDEAL_FOLLOW_REDIRECT"}

#: Pre-merge body per provider: blik carries the extra ``IDEAL_USE_LOCAL_PROXY_ONLY``
#: gate, ideal/twint share one body.
FROZEN_NEW_SESSION = {
    "ideal": HEAD.new_session,
    "twint": HEAD.new_session,
    "blik": HEAD.new_session_blik,
}


def _load(provider: str):
    package, module_file = PROVIDERS[provider]
    directory = str(PROTOCOL_ROOT / package)
    for candidate in (directory, str(PROTOCOL_ROOT)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    return importlib.import_module(module_file)


class FakeSession:
    """``requests.Session`` stand-in: records calls and exposes the observed seam."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = True
        self.calls: list[tuple[str, float, bool]] = []
        self._responses = list(responses or [])

    def get(self, url: str, *, timeout: float, allow_redirects: bool) -> Any:
        self.calls.append((url, timeout, allow_redirects))
        return self._responses.pop(0)


class FakeResponse:
    def __init__(self, *, headers: dict[str, str] | None = None, text: str = "") -> None:
        self.headers = headers or {}
        self.text = text
        self.status_code = 200


@pytest.fixture(params=sorted(PROVIDERS))
def module(request: pytest.FixtureRequest) -> Any:
    return _load(request.param)


def _provider_of(module: Any) -> str:
    return module.__name__.split("_")[0]


# --------------------------------------------------------------------------
# new_session
# --------------------------------------------------------------------------


def test_new_session_matches_the_frozen_body(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_of(module)

    # Force the ``requests`` fallback on both sides: curl_cffi is installed in
    # this checkout, and the branch worth comparing is the one that builds a real
    # session object we can inspect field by field.
    monkeypatch.setattr("common.session.CurlCffiSession", None)
    monkeypatch.setattr("common.session.CurlOpt", None)
    monkeypatch.setattr(module, "pre_proxy_url", lambda: "")
    monkeypatch.setattr(module, "env_bool", lambda name, default=False: default)

    registered: list[str] = []
    applied: list[str] = []
    monkeypatch.setattr(module, "register_proxy_for_redaction", registered.append)
    monkeypatch.setattr(module, "set_proxy", lambda session, proxy: applied.append(proxy))

    new_session = module.new_session("http://user:pw@example.test:8080")
    new_state = (dict(new_session.proxies), new_session.trust_env)

    old_registered: list[str] = []
    old_applied: list[str] = []
    old_session = FROZEN_NEW_SESSION[provider](
        "http://user:pw@example.test:8080",
        True,
        seams=HEAD.Seams(
            pre_proxy_url=lambda: "",
            register=old_registered.append,
            set_proxy=lambda session, proxy: old_applied.append(proxy),
            curl_session=None,
            curl_opt=None,
            request_session=requests.Session,
            env_bool=lambda name, default=False: default,
        ),
    )
    old_state = (dict(old_session.proxies), old_session.trust_env)

    assert new_state == old_state
    assert registered == old_registered
    assert applied == old_applied == ["http://user:pw@example.test:8080"]


@pytest.mark.parametrize("proxy", ["", "http://user:pw@example.test:8080"])
def test_set_proxy_is_only_called_for_a_non_empty_proxy(
    module: Any, monkeypatch: pytest.MonkeyPatch, proxy: str
) -> None:
    monkeypatch.setattr("common.session.CurlCffiSession", None)
    monkeypatch.setattr(module, "pre_proxy_url", lambda: "")
    monkeypatch.setattr(module, "env_bool", lambda name, default=False: default)
    monkeypatch.setattr(module, "register_proxy_for_redaction", lambda value: None)
    applied: list[str] = []
    monkeypatch.setattr(module, "set_proxy", lambda session, value: applied.append(value))

    module.new_session(proxy)

    assert applied == ([proxy] if proxy else [])


def test_pre_proxy_error_contract_is_unchanged(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators match on these two messages, so the extraction must not reword."""
    provider = _provider_of(module)
    monkeypatch.setattr("common.session.CurlCffiSession", None)
    monkeypatch.setattr(module, "pre_proxy_url", lambda: "socks5h://127.0.0.1:7897")
    monkeypatch.setattr(module, "env_bool", lambda name, default=False: default)
    monkeypatch.setattr(module, "register_proxy_for_redaction", lambda value: None)
    monkeypatch.setattr(module, "set_proxy", lambda session, value: None)

    with pytest.raises(RuntimeError) as new_error:
        module.new_session("")

    with pytest.raises(RuntimeError) as old_error:
        FROZEN_NEW_SESSION[provider](
            "",
            True,
            seams=HEAD.Seams(
                pre_proxy_url=lambda: "socks5h://127.0.0.1:7897",
                register=lambda value: None,
                set_proxy=lambda session, value: None,
                curl_session=None,
                curl_opt=None,
                request_session=requests.Session,
                env_bool=lambda name, default=False: default,
            ),
        )

    assert str(new_error.value) == str(old_error.value)
    assert "curl_cffi" in str(new_error.value)


def test_the_blik_gate_is_still_live(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``IDEAL_USE_LOCAL_PROXY_ONLY`` must still short-circuit blik's pre-proxy.

    Mutation proof for the comparison above: if the gate were dropped when the
    body moved to ``common/session.py``, both gate positions would resolve the
    pre-proxy and ``test_new_session_matches_the_frozen_body`` would still pass
    while production behaviour changed.
    """
    if module.__name__ != "blik_qr_extract":
        pytest.skip("only blik carries the gate")

    built: list[dict[str, Any]] = []

    class FakeCurlSession:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            self.proxies: dict[str, str] = {}
            self.trust_env = True

    class FakeCurlOpt:
        PRE_PROXY = "PRE_PROXY"

    monkeypatch.setattr("common.session.CurlCffiSession", FakeCurlSession)
    monkeypatch.setattr("common.session.CurlOpt", FakeCurlOpt)
    monkeypatch.setattr(module, "pre_proxy_url", lambda: "socks5h://127.0.0.1:7897")
    monkeypatch.setattr(module, "set_proxy", lambda session, value: None)

    registered: list[str] = []
    monkeypatch.setattr(module, "register_proxy_for_redaction", registered.append)

    monkeypatch.setattr(module, "env_bool", lambda name, default=False: True)
    module.new_session("")
    gated = list(registered)

    monkeypatch.setattr(module, "env_bool", lambda name, default=False: False)
    module.new_session("")

    assert gated == [""], "the gate must suppress the pre-proxy before it is registered"
    assert registered == ["", "socks5h://127.0.0.1:7897"]
    assert "curl_options" not in built[0], "the gated run must not install a PRE_PROXY"
    assert built[1]["curl_options"] == {"PRE_PROXY": "socks5h://127.0.0.1:7897"}


# --------------------------------------------------------------------------
# fetch_redirect_page
# --------------------------------------------------------------------------


def test_fetch_redirect_page_matches_the_frozen_body(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import urljoin

    from common.timeouts import DEFAULT_TIMEOUT

    provider = _provider_of(module)
    pages = [
        ({"Location": "/next"}, "<a>https://pay.example/qr1</a>"),
        ({"location": "https://pay.example/final"}, "https://pay.example/qr2"),
        ({}, ""),
    ]

    stages: list[str] = []
    monkeypatch.setattr(module, "env_bool", lambda name, default=True: default)
    monkeypatch.setattr(module, "dump_http", lambda resp, stage, *a, **k: stages.append(stage))
    monkeypatch.setattr(module, "extract_qr_candidates", lambda text: [text] if "qr" in text else [])

    new_session = FakeSession([FakeResponse(headers=h, text=t) for h, t in pages])
    new_urls = module.fetch_redirect_page(new_session, "https://pay.example/start")
    new_stages = list(stages)

    old_stages: list[str] = []
    old_session = FakeSession([FakeResponse(headers=h, text=t) for h, t in pages])
    old_urls = HEAD.fetch_redirect_page(
        old_session,
        "https://pay.example/start",
        seams=HEAD.Seams(
            pre_proxy_url=lambda: "",
            register=lambda value: None,
            set_proxy=lambda session, value: None,
            curl_session=None,
            curl_opt=None,
            env_bool=lambda name, default=True: default,
            dump_http=lambda resp, stage, *a, **k: old_stages.append(stage),
            extract_qr_candidates=lambda text: [text] if "qr" in text else [],
            follow_flag=FOLLOW_FLAG[provider],
            timeout_default=DEFAULT_TIMEOUT,
            urljoin=urljoin,
        ),
    )

    assert new_urls == old_urls
    assert new_session.calls == old_session.calls
    assert new_stages == old_stages == ["redirect_hop_1", "redirect_hop_2", "redirect_hop_3"]


def test_fetch_redirect_page_honours_the_follow_flag(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "env_bool", lambda name, default=True: False)
    monkeypatch.setattr(module, "dump_http", lambda *a, **k: pytest.fail("must not fetch when disabled"))
    monkeypatch.setattr(module, "extract_qr_candidates", lambda text: [])
    session = FakeSession([])

    assert module.fetch_redirect_page(session, "https://pay.example/start") == []
    assert session.calls == []


def test_follow_flag_name_is_the_providers_own(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared body must receive the module's literal, not a copy of ideal's."""
    seen: list[str] = []
    monkeypatch.setattr(module, "env_bool", lambda name, default=True: seen.append(name) or False)
    monkeypatch.setattr(module, "dump_http", lambda *a, **k: None)
    monkeypatch.setattr(module, "extract_qr_candidates", lambda text: [])

    module.fetch_redirect_page(FakeSession([]), "https://pay.example/start")

    assert seen == [FOLLOW_FLAG[_provider_of(module)]]

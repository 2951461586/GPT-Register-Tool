"""Pin the protocol transport seam (``sms_tool.http_backend``).

Three promises are worth a gate here, and each of them has already been a bug
shape elsewhere in this repository:

* **the default does not move** -- ``curl_cffi`` stays the transport until a
  controlled comparison says otherwise, because every measured HAR and every
  ``sec_ch_ua`` literal was produced by it;
* **a broken selection fails closed** -- ``wreq`` chosen on a machine without it,
  or with a profile wreq cannot run, raises a stable token *before* a mailbox is
  claimed, never a silent downgrade to the other stack;
* **the adapter drops nothing** -- a request kwarg ``wreq`` cannot express raises
  instead of being ignored (an ignored ``allow_redirects=False`` changes what the
  auth flow sees).

No test here touches the network or requires ``wreq`` to be installed: the
transport translation runs against a fake module, and the tests that do want the
real preset list skip when it is absent.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from sms_tool import http_backend


class _FakeTimeout(Exception):
    pass


class _FakeRequestError(Exception):
    pass


class _FakeStatus:
    def __init__(self, value: int) -> None:
        self._value = value

    def as_int(self) -> int:
        return self._value


class _FakeRaw:
    """The subset of ``wreq``'s response this adapter reads."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = _FakeStatus(status)
        self.url = "https://auth.openai.com/api/accounts/authorize"
        self.headers = [(b"content-type", b"application/json")]
        self._body = body

    def bytes(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, str, dict]] = []
        self.response = _FakeRaw()
        self.closed = False

    def request(self, method, url, **options):
        self.calls.append((method, url, options))
        return self.response

    def __exit__(self, *_exc):
        self.closed = True
        return False


class _FakeJar:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []

    def add(self, cookie, origin) -> None:
        self.added.append((cookie.name, origin))

    def get_all(self):
        return []

    def clear(self) -> None:
        self.added.clear()


class _FakeCookie:
    def __init__(self, name, value, *, domain=None, path="/", secure=False) -> None:
        self.name, self.value = name, value
        self.domain, self.path, self.secure = domain, path, secure
        self.http_only = False


class _FakeModule:
    """Stand-in for the ``wreq`` module: shapes only, no transport."""

    TimeoutError = _FakeTimeout
    RequestError = _FakeRequestError

    class Profile:
        Chrome152 = object()
        Firefox144 = object()

    class Method:
        GET = "GET"
        POST = "POST"

    class redirect:  # noqa: N801 - mirrors wreq's attribute name
        class Policy:
            @staticmethod
            def limited(count):  # noqa: ANN001, ANN205
                return ("limited", count)

            @staticmethod
            def none():  # noqa: ANN205
                return ("none",)

    class Proxy:
        @staticmethod
        def all(url):  # noqa: ANN001, ANN205
            return ("all", url)

    Cookie = _FakeCookie
    Jar = _FakeJar
    Client = _FakeClient


@pytest.fixture()
def fake_wreq(monkeypatch):
    module = _FakeModule()
    monkeypatch.setattr(http_backend, "wreq_module", lambda: module)
    monkeypatch.setattr(http_backend, "wreq_client_class", lambda: _FakeClient)
    monkeypatch.setattr(http_backend, "wreq_profile", lambda name: module.Profile.Chrome152)
    monkeypatch.setattr(http_backend, "wreq_profile_map", lambda: {"chrome152": "Chrome152"})
    return module


def _session(monkeypatch, **kwargs):
    module = _FakeModule()
    monkeypatch.setattr(http_backend, "wreq_module", lambda: module)
    monkeypatch.setattr(http_backend, "wreq_client_class", lambda: _FakeClient)
    monkeypatch.setattr(http_backend, "wreq_profile", lambda name: module.Profile.Chrome152)
    monkeypatch.setattr(http_backend, "auth_impersonate", lambda: "chrome152", raising=False)
    session = http_backend.WreqSession(**kwargs)
    return session, module


# ─────────────────────────── selection ───────────────────────────


def test_default_backend_is_curl_cffi(monkeypatch) -> None:
    monkeypatch.delenv(http_backend.BACKEND_ENV, raising=False)
    with patch.object(http_backend, "_section", lambda: {}):
        assert http_backend.configured_backend() == "curl_cffi"


def test_env_wins_over_config(monkeypatch) -> None:
    monkeypatch.setenv(http_backend.BACKEND_ENV, "WREQ")
    with patch.object(http_backend, "_section", lambda: {http_backend.BACKEND_KEY: "curl_cffi"}):
        assert http_backend.configured_backend() == "wreq"


def test_config_selects_wreq(monkeypatch) -> None:
    monkeypatch.delenv(http_backend.BACKEND_ENV, raising=False)
    with patch.object(http_backend, "_section", lambda: {http_backend.BACKEND_KEY: "wreq"}):
        assert http_backend.configured_backend() == "wreq"


def test_unknown_backend_is_rejected_not_ignored(monkeypatch) -> None:
    """A typo must not leave the default stack running under a new label."""
    monkeypatch.setenv(http_backend.BACKEND_ENV, "curl-cffi-typo")
    with pytest.raises(http_backend.ProtocolBackendError, match="protocol_http_backend_unknown"):
        http_backend.configured_backend()


def test_new_session_defaults_to_curl_cffi(monkeypatch) -> None:
    monkeypatch.delenv(http_backend.BACKEND_ENV, raising=False)
    with patch.object(http_backend, "_section", lambda: {}):
        session = http_backend.new_session()
    assert hasattr(session, "curl_options"), "the lane installs TLS options on this object"
    assert getattr(session, "_sms_http_backend", "curl_cffi") == "curl_cffi"
    session.close()


def test_wreq_selected_without_the_package_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(http_backend.BACKEND_ENV, "wreq")
    monkeypatch.setattr(http_backend, "wreq_module", lambda: None)
    with pytest.raises(http_backend.ProtocolBackendError, match="missing:wreq"):
        http_backend.new_session()


def test_wreq_rejects_a_profile_it_cannot_run(monkeypatch) -> None:
    monkeypatch.setenv(http_backend.BACKEND_ENV, "wreq")
    monkeypatch.setattr(http_backend, "wreq_module", lambda: _FakeModule())
    monkeypatch.setattr(http_backend, "wreq_profile", lambda name: None)
    with pytest.raises(http_backend.ProtocolBackendError, match="profile_unsupported"):
        http_backend.new_session()


# ─────────────────────────── profile mapping ───────────────────────────


def test_safari_tokens_expand_to_wreq_spellings() -> None:
    assert http_backend._safari_candidates("18_0") == ("safari18",)
    assert http_backend._safari_candidates("184") == ("safari18_4", "safari18")
    assert http_backend._safari_candidates("260") == ("safari26",)


def test_chrome_tokens_are_exact_only() -> None:
    assert http_backend._profile_candidates("chrome152") == ("chrome152",)
    assert http_backend._profile_candidates("firefox155") == ("firefox155",), "no version aliasing"


def test_wreq_profile_map_requires_the_package(monkeypatch) -> None:
    monkeypatch.setattr(http_backend, "wreq_module", lambda: None)
    assert http_backend.wreq_profile_map() == {}
    assert http_backend.wreq_profile("chrome152") is None


# ─────────────────────────── request translation ───────────────────────────


def test_request_kwargs_are_translated(monkeypatch) -> None:
    session, module = _session(monkeypatch)
    session.proxies = {"https": "http://proxy:8080"}
    session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers={"accept": "application/json"},
        json={"email": "a@b.c"},
        params={"x": "1"},
        data={"code": "123"},
        timeout=12,
        allow_redirects=False,
    )
    method, _url, options = session._client.calls[-1]
    assert method == "POST"
    assert options["headers"] == {"accept": "application/json"}
    assert options["json"] == {"email": "a@b.c"}
    assert options["query"] == {"x": "1"}
    assert options["form"] == {"code": "123"}
    assert options["redirect"] == ("none",)
    assert options["timeout"].total_seconds() == 12
    assert options["proxy"] == ("all", "http://proxy:8080")


def test_an_unsupported_kwarg_raises_instead_of_being_dropped(monkeypatch) -> None:
    session, _module = _session(monkeypatch)
    with pytest.raises(TypeError, match="verify"):
        session.get("https://auth.openai.com/", verify=False)


def test_response_maps_onto_the_requests_shape(monkeypatch) -> None:
    session, module = _session(monkeypatch)
    session._client.response = _FakeRaw(status=422, body=b'{"error":"bad"}')
    response = session.get("https://auth.openai.com/api/accounts/authorize")
    assert response.status_code == 422
    assert response.reason == "Unprocessable Entity"
    assert response.json() == {"error": "bad"}
    assert response.headers["content-type"] == "application/json"
    assert response.url.startswith("https://auth.openai.com/")
    assert response.elapsed is not None


def test_transport_errors_become_requests_exceptions(monkeypatch) -> None:
    from requests import exceptions

    session, module = _session(monkeypatch)

    def boom(*_args, **_kwargs):
        raise _FakeTimeout("slow")

    module.Client.request = boom
    session._client.request = boom
    with pytest.raises(exceptions.Timeout):
        session.get("https://auth.openai.com/")


def test_closed_session_refuses_to_send(monkeypatch) -> None:
    session, _module = _session(monkeypatch)
    session.close()
    with pytest.raises(http_backend.ProtocolBackendError, match="closed"):
        session.get("https://auth.openai.com/")


# ─────────────────────────── cookies ───────────────────────────


def test_cookie_set_reaches_the_native_jar(monkeypatch) -> None:
    session, _module = _session(monkeypatch)
    session.cookies.set("oai-did", "device-1", domain=".openai.com", path="/")
    assert session._jar.added == [("oai-did", "http://openai.com/")]


def test_cookie_without_domain_is_refused(monkeypatch) -> None:
    session, _module = _session(monkeypatch)
    with pytest.raises(ValueError, match="domain"):
        session.cookies.set("oai-did", "device-1")


def test_jar_set_cookie_writes_through(monkeypatch) -> None:
    """``registration/checkpoint.py`` restores through ``cookies.jar``."""
    from requests.cookies import create_cookie

    session, _module = _session(monkeypatch)
    session.cookies.jar.set_cookie(create_cookie("oai-did", "device-2", domain=".openai.com", path="/"))
    assert session._jar.added == [("oai-did", "http://openai.com/")]


def test_host_only_cookie_is_kept_but_not_mirrored(monkeypatch) -> None:
    """No domain means no origin; inventing one would place the cookie wrong."""
    from requests.cookies import create_cookie

    session, _module = _session(monkeypatch)
    http_backend._mirror_cookie(session._jar, session._module, create_cookie("a", "b"))
    assert session._jar.added == []


# ─────────────────────────── wiring ───────────────────────────


def test_the_registration_factory_uses_the_seam() -> None:
    from sms_tool.registration import protocol_helpers

    sentinel = Mock(name="session")  # a real object: the factory still installs TLS options
    factory = Mock(return_value=sentinel)
    with patch("sms_tool.http_backend.new_session", factory):
        assert protocol_helpers._new_registration_session(proxy="http://p:1") is sentinel
    assert factory.call_args.kwargs == {"proxy": "http://p:1"}


def test_tls_permutation_reports_that_it_cannot_apply_to_wreq() -> None:
    from sms_tool import tls_fingerprint

    class _WreqLike:
        _sms_http_backend = "wreq"

    fingerprint = tls_fingerprint.apply_to_session(_WreqLike(), config={"enabled": True, "mode": "seed"})
    if fingerprint.active:  # a config that drives a permutation must not claim success
        assert fingerprint.reason.startswith("backend-without-curl-options:wreq")


def test_capabilities_never_raises(monkeypatch) -> None:
    monkeypatch.setenv(http_backend.BACKEND_ENV, "nonsense")
    report = http_backend.capabilities()
    assert report["available"] is False
    assert "unknown" in str(report["reason"])


def test_curl_backend_lists_only_installed_profiles(monkeypatch) -> None:
    monkeypatch.delenv(http_backend.BACKEND_ENV, raising=False)
    with patch.object(http_backend, "_section", lambda: {}):
        supported = http_backend.supported_impersonations()
    from sms_tool.auth_headers import AUTH_FINGERPRINT_PROFILES

    assert supported <= set(AUTH_FINGERPRINT_PROFILES)
    assert "firefox144" in supported, "the default profile must always be usable"


def test_real_wreq_preset_mapping_round_trips() -> None:
    """Only when ``wreq`` is installed: our names must resolve to real presets."""
    pytest.importorskip("wreq")
    assert http_backend.wreq_profile("chrome152") is not None
    assert http_backend.wreq_profile("firefox144") is not None
    assert http_backend.wreq_profile("safari18_0") is not None, "Safari 18.0 is wreq's Safari18"


# ─────────────────────────── wreq API shape ───────────────────────────


def test_the_blocking_client_is_the_one_used() -> None:
    """``wreq.Client`` is asyncio: its request() returns a coroutine.

    Measured on wreq 0.13.0 (2026-11-19): selecting the top-level ``Client``
    fails at the first real request with "'wreq.Coroutine' object does not
    support the context manager protocol".  The adapter must therefore take
    ``wreq.blocking.Client`` -- pinned here against the real package, because a
    fake module cannot show which import a reader would pick.
    """
    wreq = pytest.importorskip("wreq")
    from wreq import blocking

    assert http_backend.wreq_client_class() is blocking.Client
    assert http_backend.wreq_client_class() is not wreq.Client


def test_platform_caveat_is_logged_once(monkeypatch, caplog) -> None:
    """A Windows-profile name over a macOS-captured preset deserves one line."""
    http_backend._WARNED.clear()
    module = _FakeModule()
    monkeypatch.setattr(http_backend, "wreq_module", lambda: module)
    monkeypatch.setattr(http_backend, "wreq_client_class", lambda: _FakeClient)
    monkeypatch.setattr(http_backend, "wreq_profile", lambda name: module.Profile.Chrome152)
    with caplog.at_level("WARNING"):
        first = http_backend.WreqSession(impersonate="chrome152")
        second = http_backend.WreqSession(impersonate="chrome152")
    messages = [record.message for record in caplog.records if "macOS capture" in record.message]
    assert len(messages) == 1, "the caveat must not repeat per session"
    first.close()
    second.close()
    http_backend._WARNED.clear()

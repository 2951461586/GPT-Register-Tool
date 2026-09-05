"""Shared fakes for sms_tool/paypal_reverse.py (zero-coverage module, 1146 lines).

This module replaces every external boundary the reverse-engineered PayPal
client touches -- HTTP session, cookies, proxy -- with in-memory doubles.

Why a shared module instead of per-test MagicMocks
--------------------------------------------------
The value of testing ``paypal_reverse`` is its *branch selection*:
which retry happens, when CAPTCHA escalates to browser fallback, when a
redirect chain is considered terminal. That means fakes must be able to

1. express failure shapes (not just the happy path), and
2. **record** calls so a test can prove "this request was NOT sent".

Nothing here touches the network, a browser, or real money.
"""

from __future__ import annotations


class FakeCookieJar:
    """Mimics ``requests.RequestsCookieJar`` closely enough for ``_cookie_dict``.

    Deliberately exposes ``get_dict()`` -- ``_cookie_dict`` branches on
    ``hasattr(jar, "get_dict")``, so a plain dict would silently exercise the
    *other* branch (the curl_cffi one).
    """

    def __init__(self, initial: dict[str, str] | None = None):
        self._cookies: dict[str, str] = dict(initial or {})

    def set(self, name, value, domain=None, path=None):  # noqa: D102 - signature parity
        self._cookies[name] = value

    def get_dict(self) -> dict[str, str]:
        return dict(self._cookies)

    def __iter__(self):
        return iter(self._cookies)

    def __len__(self):
        return len(self._cookies)


class FakeResponse:
    """Stand-in for ``requests.Response`` / ``curl_cffi`` response."""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        url: str = "https://www.paypal.com/cgi-bin/webscr",
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        json_data: object = None,
        set_cookie: str | None = None,
        raise_on_json: BaseException | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = dict(headers or {})
        self.cookies = FakeCookieJar(cookies)
        self._json_data = json_data
        self._raise_on_json = raise_on_json
        if set_cookie is not None:
            self.headers["Set-Cookie"] = set_cookie

    def json(self):
        if self._raise_on_json is not None:
            raise self._raise_on_json
        return self._json_data


class FakeSession:
    """Records every request so tests can assert on the call log.

    Responses are supplied as a scripted list, or as a ``{method_url: response}``
    map. A response entry may be an ``Exception`` *instance* -- it is raised
    instead of returned, which is how network failure is modelled.
    """

    def __init__(self, responses: list | dict | None = None):
        self.cookies = FakeCookieJar()
        self.calls: list[dict] = []
        self.closed = False
        self.proxies: dict[str, str] | None = None
        self.headers: dict[str, str] = {}
        self._scripted = list(responses) if isinstance(responses, (list, tuple)) else None
        self._mapped = dict(responses) if isinstance(responses, dict) else None
        self._cursor = 0

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._next(method, url)
        if isinstance(response, BaseException):
            raise response
        return response

    def _next(self, method, url):
        if self._mapped is not None:
            key = f"{method} {url}"
            return self._mapped.get(key, FakeResponse(200, "", url))
        if self._scripted:
            item = self._scripted[min(self._cursor, len(self._scripted) - 1)]
            self._cursor += 1
            # Accept callables so a fake can react to the request.
            return item() if callable(item) else item
        return FakeResponse(200, "", url)

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def close(self):
        self.closed = True


def make_client(redirect_url: str = "https://www.paypal.com/x?token=EC-1", **overrides):
    """Build a PayPalReverseClient wired for offline use.

    Imported lazily so importing this fakes module never pulls in curl_cffi.
    """
    from sms_tool.paypal_reverse import PayPalReverseClient

    kwargs = dict(
        redirect_url=redirect_url,
        card={"number": "4111111111111111", "exp": "12/30", "cvc": "123"},
        address={"line1": "1 Main St", "city": "Tokyo", "postal_code": "100-0001"},
        first_name="Taro",
        last_name="Yamada",
        alias_email="taro@example.com",
        password="Str0ngPass!",
        phone="+817000000000",
        sms_cfg={},
    )
    kwargs.update(overrides)
    return PayPalReverseClient(**kwargs)

"""Behaviour tests for ``services/protocol-payment/common/proxy_bookkeeping.py``.

These pin the contract the extractors now delegate to.  The module was created
by extraction (2026-09-17), and the extraction reproduced one body *wrongly* on
the first attempt -- `token_key_name` was hand-tidied into "strip `_` and `-`"
when the original strips every non-alphanumeric.  The tidy version looked better
and changed behaviour for keys like `api.key`.  So the tests below are written
against the ORIGINAL semantics, with the divergence called out explicitly.

The normaliser is injected rather than imported, so every test supplies its own
stub.  That is deliberate: it is the only way to prove the function delegates to
the caller's normaliser instead of some module-local default.
"""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "proxy_bookkeeping.py"
)
SPEC = importlib.util.spec_from_file_location("proxy_bookkeeping", MODULE_PATH)
BOOKKEEPING = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = BOOKKEEPING
SPEC.loader.exec_module(BOOKKEEPING)


def identity(value: str) -> str:
    return value


class TokenKeyNameTests(unittest.TestCase):
    def test_drops_every_non_alphanumeric_character(self):
        """The original predicate is ``re.sub(r"[^a-z0-9]+", "", ...)``.

        Regression guard for the transcription bug: a version that only strips
        ``_`` and ``-`` passes the first four cases below but fails the rest.
        """
        for raw in ("accessToken", "access_token", "access-token", "accesstoken"):
            self.assertEqual(BOOKKEEPING.token_key_name(raw), "accesstoken", raw)

        # these are the cases the "strip _ and -" rewrite got WRONG
        self.assertEqual(BOOKKEEPING.token_key_name("api.key"), "apikey")
        self.assertEqual(BOOKKEEPING.token_key_name("xsrf token"), "xsrftoken")
        self.assertEqual(BOOKKEEPING.token_key_name("csrftoken!"), "csrftoken")
        self.assertEqual(BOOKKEEPING.token_key_name("a/b"), "ab")

    def test_lowercases_before_stripping(self):
        self.assertEqual(BOOKKEEPING.token_key_name("ACCESS_TOKEN"), "accesstoken")

    def test_non_string_and_empty_inputs_are_safe(self):
        self.assertEqual(BOOKKEEPING.token_key_name(None), "")
        self.assertEqual(BOOKKEEPING.token_key_name(""), "")
        self.assertEqual(BOOKKEEPING.token_key_name("___"), "")
        self.assertEqual(BOOKKEEPING.token_key_name("..."), "")
        self.assertEqual(BOOKKEEPING.token_key_name(123), "123")


class ProxyLabelTests(unittest.TestCase):
    def test_short_is_direct_when_normaliser_returns_empty(self):
        self.assertEqual(BOOKKEEPING.proxy_short("", identity), "direct")
        self.assertEqual(BOOKKEEPING.proxy_short("x", lambda _v: ""), "direct")

    def test_short_is_a_stable_truncated_digest(self):
        first = BOOKKEEPING.proxy_short("http://a:1", identity)
        second = BOOKKEEPING.proxy_short("http://a:1", identity)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("proxy#"))
        self.assertEqual(len(first), len("proxy#") + 10)

    def test_short_delegates_to_the_injected_normaliser(self):
        """The whole point of the parameter: the caller's convention wins."""
        seen = []

        def spy(value):
            seen.append(value)
            return "NORMALISED"

        BOOKKEEPING.proxy_short("raw-input", spy)
        self.assertEqual(seen, ["raw-input"])

        # two different normalisers must yield two different labels for one input
        host_first = BOOKKEEPING.proxy_short("x", lambda _v: "host:1:u:p")
        user_first = BOOKKEEPING.proxy_short("x", lambda _v: "u:p:host:1")
        self.assertNotEqual(host_first, user_first)

    def test_label_is_an_alias_of_short(self):
        for raw in ("", "http://a:1", "1.2.3.4:8080"):
            self.assertEqual(
                BOOKKEEPING.proxy_label(raw, identity),
                BOOKKEEPING.proxy_short(raw, identity),
                raw,
            )

    def test_key_is_a_full_digest_or_empty(self):
        self.assertEqual(BOOKKEEPING.proxy_key("", identity), "")
        self.assertEqual(BOOKKEEPING.proxy_key("x", lambda _v: ""), "")
        digest = BOOKKEEPING.proxy_key("http://a:1", identity)
        self.assertEqual(len(digest), 64)
        # a key must be a prefix-extension of the short label's digest
        short = BOOKKEEPING.proxy_short("http://a:1", identity)
        self.assertTrue(digest.startswith(short[len("proxy#"):]))


class KnownStaticHostTests(unittest.TestCase):
    def test_recognises_the_stripe_cdn_hosts(self):
        for host in (
            "stripe-camo.global.ssl.fastly.net",
            "files.stripe.com",
            "js.stripe.com",
            "m.stripe.network",
            "q.stripe.com",
        ):
            self.assertTrue(BOOKKEEPING.is_known_static_host(f"https://{host}/x"), host)

    def test_is_case_insensitive_on_the_netloc(self):
        self.assertTrue(BOOKKEEPING.is_known_static_host("https://JS.STRIPE.COM/v3/"))

    def test_rejects_lookalikes_and_non_static_hosts(self):
        for url in (
            "https://checkout.stripe.com/pay",
            "https://evil.example/js.stripe.com",
            "https://notstripe.com",
            "",
            "not a url",
        ):
            self.assertFalse(BOOKKEEPING.is_known_static_host(url), url)


class FindNamedTokenTests(unittest.TestCase):
    def test_matches_snake_and_camel_aliases(self):
        self.assertEqual(
            BOOKKEEPING.find_named_token({"access_token": "abc"}, ("accessToken",)),
            "abc",
        )
        self.assertEqual(
            BOOKKEEPING.find_named_token({"accessToken": "xyz"}, ("access_token",)),
            "xyz",
        )

    def test_reads_cookie_shaped_entries(self):
        payload = {"name": "access_token", "value": "from-cookie"}
        self.assertEqual(BOOKKEEPING.find_named_token(payload, ("access_token",)), "from-cookie")

    def test_searches_nested_structures_depth_first(self):
        self.assertEqual(
            BOOKKEEPING.find_named_token({"a": {"b": {"access_token": "deep"}}}, ("access_token",)),
            "deep",
        )
        self.assertEqual(
            BOOKKEEPING.find_named_token([{"x": 1}, {"access_token": "inlist"}], ("access_token",)),
            "inlist",
        )

    def test_accepts_numeric_values_and_skips_blank_ones(self):
        self.assertEqual(BOOKKEEPING.find_named_token({"access_token": 12345}, ("access_token",)), "12345")
        self.assertEqual(BOOKKEEPING.find_named_token({"access_token": "   "}, ("access_token",)), "")

    def test_returns_empty_when_absent_or_input_is_not_a_container(self):
        self.assertEqual(BOOKKEEPING.find_named_token({"other": "v"}, ("access_token",)), "")
        self.assertEqual(BOOKKEEPING.find_named_token(None, ("access_token",)), "")
        self.assertEqual(BOOKKEEPING.find_named_token(42, ("access_token",)), "")
        self.assertEqual(BOOKKEEPING.find_named_token("text", ("access_token",)), "")

    def test_uses_the_corrected_key_normalisation(self):
        """A dotted alias only matches if every non-alphanumeric is stripped."""
        self.assertEqual(
            BOOKKEEPING.find_named_token({"api.key": "dotted"}, ("api.key",)),
            "dotted",
        )


class ModuleContractTests(unittest.TestCase):
    def test_stays_free_of_the_host_package(self):
        """architecture.md Rule 10: protocol-payment must not import sms_tool.

        Checks *imports*, not the text of the file.  A naive substring search
        fails here because the module docstring discusses `sms_tool` in prose --
        a guard that trips on documentation is worse than no guard, since the
        obvious "fix" is to delete the explanation.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        self.assertNotIn("sms_tool", imported)

    def test_only_imports_stdlib(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        tops = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
        self.assertEqual(tops, {"__future__", "hashlib", "re", "typing", "urllib"})

    def test_exports_exactly_the_six_extracted_functions(self):
        self.assertEqual(
            sorted(BOOKKEEPING.__all__),
            sorted([
                "proxy_short", "proxy_label", "proxy_key",
                "is_known_static_host", "find_named_token", "token_key_name",
            ]),
        )


if __name__ == "__main__":
    unittest.main()

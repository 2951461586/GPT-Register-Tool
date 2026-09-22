"""Behaviour tests for ``services/protocol-payment/common/proxy_selection.py``.

Batch 5 of the extractor consolidation.  Scope, per the module docstring:

  proxy_for_country    ideal / twint / blik.  kakao's also tags the sticky sid
                       with the country and derives the target country without
                       ``normalize_country``, so it is a different contract.
  pick_random_proxies  ideal / twint / blik.
  is_preferred_proxy   ideal / twint only.  blik's keys records by group and
                       honours ``zero_ok``, so its rule is different.
"""

import ast
import importlib.util
import random
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import quote, unquote

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "proxy_selection.py"
)
SPEC = importlib.util.spec_from_file_location("proxy_selection", MODULE_PATH)
SELECT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SELECT
SPEC.loader.exec_module(SELECT)

PROTOCOL_PAYMENT = Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
EXTRACTORS = {
    "ideal": PROTOCOL_PAYMENT / "ideal" / "ideal_qr_extract.py",
    "twint": PROTOCOL_PAYMENT / "twint" / "twint_extract.py",
    "blik": PROTOCOL_PAYMENT / "blik" / "blik_qr_extract.py",
    "kakao": PROTOCOL_PAYMENT / "kakao" / "kakao_extract.py",
}

SELECTOR = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)


def _country(value):
    """A stand-in for the per-extractor ``normalize_country``."""
    return (value or "nl").upper()


def _normalize(value):
    """A stand-in for the per-extractor ``normalize_proxy_url`` -- including
    its empty-input behaviour, which is what makes the "no proxy" error
    reachable at all."""
    if not value:
        return ""
    return value if "://" in value else "http://%s" % value


class ProxyForCountryTests(unittest.TestCase):
    def _derive(self, proxy, country="US", **overrides):
        calls = {"register": []}
        kwargs = {
            "normalize": _normalize,
            "country_normalizer": _country,
            "selector": SELECTOR,
            "label": lambda value: "proxy#" + value,
            "register": calls["register"].append,
        }
        kwargs.update(overrides)
        try:
            out = SELECT.proxy_for_country(proxy, country, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            return ("RAISED:" + type(exc).__name__, str(exc)), calls
        return ("ok", out), calls

    def test_rewrites_the_selector_in_the_username(self):
        (status, out), _ = self._derive("http://user-country-de:pass@1.2.3.4:8080")
        self.assertEqual(status, "ok")
        self.assertIn("user-country-us", unquote(out))

    def test_rewrites_the_selector_in_the_password(self):
        (status, out), _ = self._derive("http://user:pass-region-de@1.2.3.4:8080")
        self.assertEqual(status, "ok")
        self.assertIn("pass-region-us", unquote(out))

    def test_preserves_the_case_of_the_original_value(self):
        """``de`` -> ``us`` but ``DE`` -> ``US``; the case is a provider-side
        convention that must survive the rewrite."""
        (_, lower), _ = self._derive("http://u-country-de:p@1.2.3.4:8080")
        (_, upper), _ = self._derive("http://u-country-DE:p@1.2.3.4:8080")
        self.assertIn("country-us", unquote(lower))
        self.assertIn("country-US", unquote(upper))

    def test_keeps_everything_that_carries_the_sticky_session(self):
        (status, out), _ = self._derive(
            "http://user-sid-abc-country-de:pass@1.2.3.4:8080")
        self.assertEqual(status, "ok")
        self.assertIn("user-sid-abc-country-us", unquote(out))
        self.assertIn("1.2.3.4:8080", out)

    def test_retains_path_query_and_fragment(self):
        (status, out), _ = self._derive(
            "http://u-country-de:p@1.2.3.4:8080/keep?a=1#frag")
        self.assertEqual(status, "ok")
        self.assertIn("/keep?a=1#frag", out)

    def test_ipv6_host_is_bracketed_and_the_port_kept(self):
        (status, out), _ = self._derive("http://u-country-de:p@[2001:db8::1]:8080")
        self.assertEqual(status, "ok")
        self.assertIn("[2001:db8::1]:8080", out)

    def test_percent_encoded_credentials_survive(self):
        """The username is unquoted for the rewrite and re-quoted afterwards,
        so a literal ``@`` in the password stays encoded."""
        (status, out), _ = self._derive("http://u-country-de:p%40ss@1.2.3.4:8080")
        self.assertEqual(status, "ok")
        self.assertIn("p%40ss", out)
        self.assertNotIn("p@ss@", out.split("@")[0] + "@")

    def test_empty_proxy_raises(self):
        """The two failure modes are distinct on purpose: "no proxy at all" and
        "no selector to rewrite" need different operator messages.  Asserting
        only the exception type left an ``if not proxy`` removal mutant alive."""
        (status, message), calls = self._derive("")
        self.assertEqual(status, "RAISED:RuntimeError")
        self.assertIn("代理为空", message)
        self.assertEqual(calls["register"], [], "nothing to register")

    def test_missing_selector_raises_with_the_labelled_proxy(self):
        (status, message), _ = self._derive("http://user:pass@1.2.3.4:8080")
        self.assertEqual(status, "RAISED:RuntimeError")
        self.assertIn("proxy#http://user:pass@1.2.3.4:8080", message,
                      "the error must not leak the raw proxy")

    def test_the_derived_proxy_is_registered_for_redaction(self):
        """The derived proxy is a NEW credential.  If it is not registered, the
        first log line mentioning it leaks it."""
        (status, out), calls = self._derive("http://u-country-de:p@1.2.3.4:8080")
        self.assertEqual(status, "ok")
        self.assertEqual(calls["register"], [out])

    def test_normalize_is_injected_not_defaulted(self):
        seen = []

        def normalize(value):
            seen.append(value)
            return "http://normalized-country-de:x@1.2.3.4:8080"

        (status, out), _ = self._derive("anything", normalize=normalize)
        self.assertEqual(seen, ["anything"])
        self.assertIn("country-us", unquote(out))

    def test_country_normalizer_is_injected_not_defaulted(self):
        """ideal falls back to NL, twint to CH, blik to a configured default --
        a shared default would silently pick one."""
        seen = []

        def country_normalizer(value):
            seen.append(value)
            return "PL"

        (status, out), _ = self._derive(
            "http://u-country-de:p@1.2.3.4:8080", country="XX",
            country_normalizer=country_normalizer)
        self.assertEqual(seen, ["XX"])
        self.assertIn("country-pl", unquote(out))

    def test_selector_is_injected(self):
        other = re.compile(r"(?i)(?P<name>zone)(?P<separator>[-_=])(?P<value>[a-z]{2})")
        (status, out), _ = self._derive("http://u-zone-de:p@1.2.3.4:8080", selector=other)
        self.assertEqual(status, "ok")
        self.assertIn("zone-us", unquote(out))


class IsPreferredProxyTests(unittest.TestCase):
    def _call(self, group, proxy, state, env=None, score_env="X_PROXY_SCORE",
              key=lambda proxy: "K:" + proxy):
        def env_bool(name, default=False):
            return (env or {}).get(name, default)

        return SELECT.is_preferred_proxy(
            group, proxy,
            score_env=score_env,
            env_bool=env_bool,
            load_state=lambda: state,
            key=key,
        )

    def test_empty_group_is_never_preferred(self):
        self.assertFalse(self._call("", "http://a", {"seed": {"K:http://a": {"success": 3}}}))

    def test_switch_off_means_nothing_is_preferred(self):
        self.assertFalse(
            self._call("seed", "http://a", {"seed": {"K:http://a": {"success": 3}}},
                       env={"X_PROXY_SCORE": False}))

    def test_switch_defaults_to_on(self):
        self.assertTrue(
            self._call("seed", "http://a", {"seed": {"K:http://a": {"success": 1}}}))

    def test_success_above_zero_is_preferred(self):
        state = {"seed": {"K:http://a": {"success": 1}, "K:http://b": {"success": 0}}}
        self.assertTrue(self._call("seed", "http://a", state))
        self.assertFalse(self._call("seed", "http://b", state))

    def test_unknown_proxy_is_not_preferred(self):
        self.assertFalse(self._call("seed", "http://zzz", {"seed": {}}))

    def test_malformed_state_is_not_preferred(self):
        for state in ({"seed": "notadict"}, {"seed": {"K:http://a": "notadict"}}, {}):
            self.assertFalse(self._call("seed", "http://a", state), state)

    def test_score_env_name_is_the_only_switch_read(self):
        """ideal and twint read different switches; neither may answer to the
        other's."""
        seen = []

        def env_bool(name, default=False):
            seen.append(name)
            return default

        SELECT.is_preferred_proxy(
            "seed", "http://a", score_env="IDEAL_PROXY_SCORE",
            env_bool=env_bool, load_state=lambda: {}, key=lambda p: "K:" + p)
        self.assertEqual(seen, ["IDEAL_PROXY_SCORE"])

    def test_key_is_injected(self):
        """blik keys by ``(group, proxy)`` and ideal/twint by proxy alone; the
        shared rule must not assume either."""
        state = {"seed": {"seed|http://a": {"success": 2}}}
        self.assertTrue(
            self._call("seed", "http://a", state,
                       key=lambda proxy: "seed|" + proxy))


class PickRandomProxiesTests(unittest.TestCase):
    def _call(self, proxies, limit, group="seed", preferred=(), order=None):
        return SELECT.pick_random_proxies(
            list(proxies), limit, group,
            order_group=order or (lambda g, p: list(p)),
            is_preferred=lambda g, p: p in preferred,
        )

    def test_preferred_come_first_and_are_never_shuffled_away(self):
        random.seed(7)
        out = self._call(["a", "b", "c", "d"], 2, preferred=("c", "a"))
        self.assertEqual(out[:2], ["a", "c"])

    def test_limit_at_or_above_the_pool_returns_everything(self):
        random.seed(7)
        out = self._call(["a", "b", "c"], 3, preferred=("b",))
        self.assertEqual(sorted(out), ["a", "b", "c"])
        self.assertEqual(out[0], "b", "preferred still leads")

    def test_limit_below_the_pool_returns_exactly_limit(self):
        random.seed(7)
        out = self._call(["a", "b", "c", "d", "e"], 3)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(set(out)), 3, "no duplicates")

    def test_tail_is_randomised(self):
        """The whole point of the function: the non-preferred tail must not
        come back in input order every time."""
        seen = set()
        for seed in range(25):
            random.seed(seed)
            seen.add(tuple(self._call(["a", "b", "c", "d", "e", "f"], 6)))
        self.assertGreater(len(seen), 1, "tail was never reshuffled")

    def test_empty_group_skips_ordering(self):
        calls = []

        def order(group, proxies):
            calls.append(group)
            return list(proxies)

        random.seed(7)
        self._call(["a", "b"], 2, group="", order=order)
        self.assertEqual(calls, [], "no group means no group ordering")

    def test_order_group_receives_the_group(self):
        calls = []

        def order(group, proxies):
            calls.append((group, list(proxies)))
            return sorted(proxies)

        random.seed(7)
        self._call(["c", "a"], 2, group="checkout", order=order)
        self.assertEqual(calls, [("checkout", ["c", "a"])])

    def test_empty_pool(self):
        random.seed(7)
        self.assertEqual(self._call([], 5), [])

    def test_the_whole_pool_branch_is_the_limit_equal_to_len_case(self):
        """`limit >= len(proxies)` vs `limit > len(proxies)` is an EQUIVALENT
        mutant -- probed across 300 seeds: same items, preferred always first,
        both uniformly cover all 6 tail permutations; only the concrete random
        permutation differs, and that is random by contract.  Recorded here so
        nobody "fixes" it again; this test pins the part that IS observable.
        """
        random.seed(3)
        out = self._call(["a", "b", "c"], 3, preferred=("b",))
        self.assertEqual(sorted(out), ["a", "b", "c"])
        self.assertEqual(out[0], "b")
        random.seed(3)
        huge = self._call(["a", "b", "c"], 10 ** 6, preferred=("c",))
        self.assertEqual(huge[0], "c")
        self.assertEqual(sorted(huge), ["a", "b", "c"])

    def test_preferred_beyond_limit_are_truncated(self):
        random.seed(7)
        out = self._call(["a", "b", "c"], 1, preferred=("a", "b", "c"))
        self.assertEqual(out, ["a"])


class NoEarlyBoundDefaultTests(unittest.TestCase):
    def test_no_default_is_an_attribute(self):
        """The batch 3 trap: ``random.shuffle`` is a bound method of a hidden
        module-level Random instance, so it must never be captured as a
        default."""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if default is None:
                    continue
                self.assertNotIsInstance(
                    default, ast.Attribute,
                    "%s has an un-patchable bound default" % node.name)

    def test_injected_knobs_have_no_default(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        expected = {
            "proxy_for_country": ("normalize", "country_normalizer", "selector",
                                  "label", "register"),
            "is_preferred_proxy": ("score_env", "env_bool", "load_state", "key"),
            "pick_random_proxies": ("order_group", "is_preferred"),
        }
        for name, knobs in expected.items():
            node = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            names = [a.arg for a in node.args.kwonlyargs]
            defaults = node.args.kw_defaults
            pad = len(names) - len(defaults)
            for knob in knobs:
                self.assertIn(knob, names, "%s.%s" % (name, knob))
                self.assertIsNone(defaults[names.index(knob) - pad],
                                  "%s.%s must have no default" % (name, knob))


class ModuleContractTests(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(sorted(SELECT.__all__),
                         ["is_preferred_proxy", "pick_random_proxies",
                          "proxy_for_country"])

    def test_stays_free_of_the_host_package(self):
        """architecture.md Rule 10: protocol-payment must not import sms_tool."""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("sms_tool", imported)

    def test_only_imports_stdlib(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        tops = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
        self.assertEqual(tops, {"__future__", "random", "re", "typing", "urllib"})


class ExtractorWiringTests(unittest.TestCase):
    def _func(self, path, name):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def _shared_call(self, path, name, callee):
        node = self._func(path, name)
        self.assertIsNotNone(node, "%s missing" % name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id == callee:
                return {k.arg: ast.unparse(k.value) for k in sub.keywords}
        self.fail("%s does not call %s" % (name, callee))

    def _is_stub(self, path, name, callee):
        node = self._func(path, name)
        if node is None:
            return False
        body = [n for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        if len(body) != 1:
            return False
        stmt = body[0]
        call = stmt.value if isinstance(stmt, ast.Return) else (
            stmt.value if isinstance(stmt, ast.Expr) else None)
        return (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == callee)

    def test_three_extractors_delegate_proxy_for_country(self):
        for provider in ("ideal", "twint", "blik"):
            self.assertTrue(
                self._is_stub(EXTRACTORS[provider], "proxy_for_country",
                              "shared_proxy_for_country"), provider)

    def test_three_extractors_delegate_pick_random_proxies(self):
        for provider in ("ideal", "twint", "blik"):
            self.assertTrue(
                self._is_stub(EXTRACTORS[provider], "pick_random_proxies",
                              "shared_pick_random_proxies"), provider)

    def test_ideal_and_twint_delegate_is_preferred_proxy(self):
        for provider in ("ideal", "twint"):
            self.assertTrue(
                self._is_stub(EXTRACTORS[provider], "is_preferred_proxy",
                              "shared_is_preferred_proxy"), provider)

    def test_kakao_keeps_its_own_proxy_for_country(self):
        self.assertFalse(
            self._is_stub(EXTRACTORS["kakao"], "proxy_for_country",
                          "shared_proxy_for_country"))

    def test_blik_keeps_its_own_is_preferred_proxy(self):
        self.assertFalse(
            self._is_stub(EXTRACTORS["blik"], "is_preferred_proxy",
                          "shared_is_preferred_proxy"))

    def test_score_env_is_per_provider(self):
        """A swapped name would make ideal answer to TWINT_PROXY_SCORE."""
        expected = {"ideal": "IDEAL_PROXY_SCORE", "twint": "TWINT_PROXY_SCORE"}
        for provider, want in expected.items():
            kwargs = self._shared_call(EXTRACTORS[provider], "is_preferred_proxy",
                                       "shared_is_preferred_proxy")
            self.assertEqual((kwargs.get("score_env") or "").strip("'\""), want,
                             provider)

    def test_proxy_for_country_passes_the_modules_own_knobs(self):
        want = {
            "normalize": "normalize_proxy_url",
            "country_normalizer": "normalize_country",
            "selector": "_PROXY_COUNTRY_SELECTOR_RE",
            "label": "proxy_label",
            "register": "register_proxy_for_redaction",
        }
        for provider in ("ideal", "twint", "blik"):
            kwargs = self._shared_call(EXTRACTORS[provider], "proxy_for_country",
                                       "shared_proxy_for_country")
            for knob, value in want.items():
                self.assertEqual(kwargs.get(knob), value, "%s.%s" % (provider, knob))

    def test_pick_passes_the_modules_own_preference_rule(self):
        """blik's rule is not the shared one, and this is where that shows."""
        for provider in ("ideal", "twint", "blik"):
            kwargs = self._shared_call(EXTRACTORS[provider], "pick_random_proxies",
                                       "shared_pick_random_proxies")
            self.assertEqual(kwargs.get("order_group"), "order_proxy_group", provider)
            self.assertEqual(kwargs.get("is_preferred"), "is_preferred_proxy", provider)


if __name__ == "__main__":
    unittest.main()

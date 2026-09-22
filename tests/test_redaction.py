"""Behaviour tests for ``services/protocol-payment/common/redaction.py``.

Batch 2 of the extractor consolidation.  Scope note: blik has its own
``_redact_text`` (it also strips ``IDEAL_BLIK_CODE`` and redacts six-digit
``blik_code`` fields), so ``redact_text`` is extracted for ideal/twint only --
see the module docstring.  ``redact_log_text`` and
``register_proxy_for_redaction`` are shared by all four extractors.
"""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "redaction.py"
)
SPEC = importlib.util.spec_from_file_location("redaction", MODULE_PATH)
REDACT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = REDACT
SPEC.loader.exec_module(REDACT)

PROTOCOL_PAYMENT = Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
EXTRACTORS = {
    "ideal": PROTOCOL_PAYMENT / "ideal" / "ideal_qr_extract.py",
    "twint": PROTOCOL_PAYMENT / "twint" / "twint_extract.py",
    "blik": PROTOCOL_PAYMENT / "blik" / "blik_qr_extract.py",
    "kakao": PROTOCOL_PAYMENT / "kakao" / "kakao_extract.py",
}


def _label(proxy):
    """A stand-in for the per-extractor ``proxy_label``.

    Deliberately contains NO substring that is itself a registered value:
    ``redact_log_text`` replaces sequentially, so a label that embeds another
    registered value gets replaced a second time.  That is real behaviour of
    the original code, not a bug -- but a label like ``proxy#1.2.3.4`` makes
    every assertion about "what was replaced" ambiguous.  Length-based labels
    stay unique and non-overlapping.
    """
    return "<%d>" % len(proxy)


class RegistryTests(unittest.TestCase):
    def test_register_then_snapshot(self):
        registry = REDACT.RedactionRegistry()
        registry.register({"a", "b"})
        self.assertEqual(set(registry.snapshot()), {"a", "b"})

    def test_snapshot_is_longest_first(self):
        """A shorter value must never shadow a longer one containing it.

        ``1.2.3.4`` replaced before ``1.2.3.4:8080`` would leave the port
        behind in the log, which is the whole thing being prevented.
        """
        registry = REDACT.RedactionRegistry()
        registry.register({"1.2.3.4", "1.2.3.4:8080", "http://u:p@1.2.3.4:8080"})
        self.assertEqual(
            registry.snapshot(), ["http://u:p@1.2.3.4:8080", "1.2.3.4:8080", "1.2.3.4"]
        )

    def test_instances_are_independent(self):
        """Each extractor owns one -- a shared registry would leak proxies."""
        a, b = REDACT.RedactionRegistry(), REDACT.RedactionRegistry()
        a.register({"x"})
        self.assertEqual(b.snapshot(), [])
        self.assertEqual(a.size, 1)
        self.assertEqual(b.size, 0)

    def test_register_acquires_the_lock(self):
        """Deterministic guard: assert __enter__ rather than racing threads.

        Under the GIL a set update from 8 threads survives often enough that a
        lock-removal mutant stays green; that is exactly what happened to the
        equivalent assertion in batch 4.
        """
        registry = REDACT.RedactionRegistry()
        entered = []

        class SpyLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *exc):
                return False

        registry._lock = SpyLock()
        registry.register({"x"})
        self.assertEqual(entered, [True], "register() must hold the lock")

    def test_snapshot_acquires_the_lock(self):
        """Same guard for the read side -- mutation testing showed the register
        assertion alone leaves a snapshot lock-removal mutant alive."""
        registry = REDACT.RedactionRegistry()
        entered = []

        class SpyLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *exc):
                return False

        registry._lock = SpyLock()
        registry.register({"x"})
        entered.clear()
        registry.snapshot()
        self.assertEqual(entered, [True], "snapshot() must hold the lock")


class RegisterProxyTests(unittest.TestCase):
    def _set(self, proxy, normalize=lambda value: value):
        registry = REDACT.RedactionRegistry()
        REDACT.register_proxy_for_redaction(
            proxy, registry=registry, normalize=normalize
        )
        return set(registry.snapshot())

    def test_empty_input_registers_nothing(self):
        self.assertEqual(self._set(""), set())
        self.assertEqual(self._set("   "), set())

    def test_registers_every_spelling_of_the_proxy(self):
        """Raw line, normalised URL, percent-decoded form, netloc and
        ``host:port`` -- which one reaches the log depends on which layer
        produced the message."""
        self.assertEqual(
            self._set("http://u:p@1.2.3.4:8080"),
            {
                "http://u:p@1.2.3.4:8080",   # raw + normalised + decoded
                "u:p@1.2.3.4:8080",          # netloc
                "1.2.3.4:8080",              # host:port
            },
        )

    def test_bare_hostname_is_registered_only_when_there_is_no_port(self):
        """``values.add(f"{host}:{port}" if port else host)`` -- with a port the
        bare host is not registered on its own."""
        with_port = self._set("http://1.2.3.4:8080")
        self.assertNotIn("1.2.3.4", with_port)
        self.assertIn("1.2.3.4:8080", with_port)
        without_port = self._set("http://1.2.3.4")
        self.assertIn("1.2.3.4", without_port)

    def test_percent_encoded_credentials_are_decoded_before_urlsplit(self):
        """``u%40n`` is decoded to ``u@n`` first, otherwise urlsplit would read
        the wrong netloc and the real host would never be registered."""
        values = self._set("http://u%40n:p@1.2.3.4:8080")
        self.assertIn("http://u%40n:p@1.2.3.4:8080", values)
        self.assertIn("http://u@n:p@1.2.3.4:8080", values, "decoded form missing")
        self.assertIn("1.2.3.4:8080", values)
        self.assertIn("u@n:p@1.2.3.4:8080", values)

    def test_ipv6_host_is_bracketed(self):
        """A bare ``2001:db8::1`` would be re-split on its colons, so the
        bracketed form is what gets registered."""
        values = self._set("http://[2001:db8::1]:443")
        self.assertIn("[2001:db8::1]:443", values)

    def test_unparseable_port_is_skipped_not_fatal(self):
        """``urlsplit(...).port`` raises ValueError above 65535; the old code
        caught it and registered the bare host."""
        values = self._set("http://1.2.3.4:99999")
        self.assertIn("1.2.3.4", values, "bare host must still be registered")
        self.assertIn("1.2.3.4:99999", values, "the raw netloc is kept as-is")

    def test_normalize_is_injected_not_defaulted(self):
        """Each extractor has its own normaliser; a default would pick one.
        Everything derived from the proxy comes from ``normalize``'s output, so
        swapping it changes the whole derived set."""
        seen = []

        def normalize(value):
            seen.append(value)
            return "http://normalized"

        values = self._set("http://u:p@1.2.3.4:8080", normalize=normalize)
        self.assertEqual(seen, ["http://u:p@1.2.3.4:8080"])
        self.assertEqual(
            values,
            {
                "http://u:p@1.2.3.4:8080",   # raw only
                "http://normalized",         # normalised + decoded
                "normalized",                # netloc / hostname
            },
        )


class RedactLogTextTests(unittest.TestCase):
    def _redact(self, text, values, label=_label):
        registry = REDACT.RedactionRegistry()
        registry.register(values)
        return REDACT.redact_log_text(text, registry=registry, proxy_label=label)

    def test_replaces_every_registered_value(self):
        out = self._redact("a http://u:p@1.2.3.4:8080 b 1.2.3.4 c",
                           {"http://u:p@1.2.3.4:8080", "1.2.3.4"})
        self.assertEqual(out, "a <23> b <7> c")

    def test_nothing_registered_means_unchanged(self):
        self.assertEqual(self._redact("plain", set()), "plain")

    def test_none_is_normalised_to_empty_string(self):
        self.assertEqual(self._redact(None, set()), "")

    def test_non_string_input_is_coerced_not_returned_as_is(self):
        """``str(text or "")`` -- not ``text or ""``.  The latter returns the
        int unchanged, so the caller gets an int where it expects a str.  A
        mutation to the weaker form survived until this test."""
        out = self._redact(12345, set())
        self.assertIsInstance(out, str)
        self.assertEqual(out, "12345")

    def test_direct_label_falls_back_to_a_hash(self):
        """``proxy_label`` returns 'direct' for a proxy-less entry; writing the
        literal word 'direct' would be useless and confusable."""
        out = self._redact("secret", {"secret"}, label=lambda value: "direct")
        self.assertTrue(out.startswith("proxy#"), out)
        self.assertNotIn("direct", out)

    def test_raising_label_falls_back_to_a_hash(self):
        def boom(value):
            raise ValueError(value)

        out = self._redact("secret", {"secret"}, label=boom)
        self.assertTrue(out.startswith("proxy#"), out)

    def test_fallback_hash_is_stable(self):
        a = self._redact("secret", {"secret"}, label=lambda value: "direct")
        b = self._redact("secret", {"secret"}, label=lambda value: "direct")
        self.assertEqual(a, b)

    def test_longest_value_wins(self):
        out = self._redact("1.2.3.4:8080", {"1.2.3.4", "1.2.3.4:8080"})
        self.assertEqual(out, "<12>", "the shorter value must not be replaced first")


class RedactTextTests(unittest.TestCase):
    def _env_int(self, mapping=None):
        mapping = mapping or {}
        calls = []

        def env_int(name, default, minimum=None):
            calls.append((name, default, minimum))
            raw = mapping.get(name, "")
            if raw == "":
                return default
            try:
                value = int(raw)
            except ValueError:
                return default
            return max(value, minimum or 1)

        return env_int, calls

    def _redact(self, text, limit=None, env=None):
        env_int, calls = self._env_int(env)
        registry = REDACT.RedactionRegistry()
        return (
            REDACT.redact_text(
                text,
                limit,
                registry=registry,
                proxy_label=_label,
                limit_env="X_DUMP_LIMIT",
                env_int=env_int,
            ),
            calls,
        )

    def test_bearer_token_is_scrubbed(self):
        out, _ = self._redact("Authorization: Bearer abc.def-ghi_123")
        self.assertEqual(out, "Authorization: Bearer ***")

    def test_session_cookie_is_scrubbed(self):
        out, _ = self._redact("__Secure-next-auth.session-token=zzz; Path=/")
        self.assertEqual(out, "__Secure-next-auth.session-token=***; Path=/")

    def test_token_fields_are_scrubbed(self):
        for field in ("accessToken", "access_token", "sessionToken", "token"):
            for separator in ("=", ":"):
                out, _ = self._redact('%s%s"secret"' % (field, separator))
                self.assertNotIn("secret", out, field)

    def test_registered_proxies_are_scrubbed_too(self):
        registry = REDACT.RedactionRegistry()
        registry.register({"http://u:p@1.2.3.4:8080"})
        env_int, _ = self._env_int()
        out = REDACT.redact_text(
            "url=http://u:p@1.2.3.4:8080",
            registry=registry,
            proxy_label=_label,
            limit_env="X_DUMP_LIMIT",
            env_int=env_int,
        )
        self.assertNotIn("1.2.3.4", out)

    def test_limit_comes_from_the_injected_env_name(self):
        _, calls = self._redact("x" * 2000, env={"X_DUMP_LIMIT": "700"})
        self.assertEqual(calls, [("X_DUMP_LIMIT", 6000, 500)])

    def test_limit_env_is_used_with_the_6000_default_and_500_floor(self):
        """Ideal and twint read different env vars; the name is the knob, and
        the 6000/500 pair is the contract the extractors relied on."""
        _, calls = self._redact("x" * 2000)
        self.assertEqual(calls, [("X_DUMP_LIMIT", 6000, 500)])

    def test_missing_env_value_falls_back_to_6000(self):
        out, _ = self._redact("x" * 7000)
        self.assertEqual(len(out), 6000)

    def test_explicit_limit_wins_and_needs_no_env(self):
        out, calls = self._redact("x" * 100, limit=10)
        self.assertEqual(out, "x" * 10)
        self.assertEqual(calls, [], "explicit limit must not read the env")

    def test_limit_env_is_not_read_from_the_other_provider(self):
        """A default would make twint answer to IDEAL_DUMP_LIMIT."""
        out, calls = self._redact("x" * 7000, env={"IDEAL_DUMP_LIMIT": "10"})
        self.assertEqual(calls, [("X_DUMP_LIMIT", 6000, 500)])
        self.assertEqual(len(out), 6000)


class NoEarlyBoundDefaultTests(unittest.TestCase):
    """The batch 3 trap, re-checked: a default that is an ``ast.Attribute`` is
    captured when the ``def`` runs, so no test can ever patch it."""

    def test_no_default_is_an_attribute(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if default is None:
                    continue
                self.assertNotIsInstance(
                    default, ast.Attribute,
                    "%s has an un-patchable bound default" % node.name,
                )

    def test_injected_knobs_have_no_default(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for name, knobs in (
            ("redact_log_text", ("registry", "proxy_label")),
            ("register_proxy_for_redaction", ("registry", "normalize")),
            ("redact_text", ("registry", "proxy_label", "limit_env", "env_int")),
        ):
            node = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            names = [a.arg for a in node.args.kwonlyargs]
            defaults = node.args.kw_defaults
            pad = len(names) - len(defaults)
            for knob in knobs:
                self.assertIn(knob, names)
                self.assertIsNone(defaults[names.index(knob) - pad],
                                  "%s.%s must have no default" % (name, knob))


class ModuleContractTests(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(
            sorted(REDACT.__all__),
            ["RedactionRegistry", "redact_log_text", "redact_text",
             "register_proxy_for_redaction"],
        )

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
        self.assertEqual(tops, {"__future__", "hashlib", "re", "threading",
                                "typing", "urllib"})


class ExtractorWiringTests(unittest.TestCase):
    """The delegations in the four extractors, pinned at the AST level so a
    future edit that re-inlines the bodies is caught."""

    def _func(self, path, name):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("%s has no %s" % (path.name, name))

    def _is_stub(self, node, shared_name):
        body = [n for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        if len(body) != 1:
            return False
        stmt = body[0]
        call = stmt.value if isinstance(stmt, ast.Return) else (
            stmt.value if isinstance(stmt, ast.Expr) else None)
        return (
            call is not None
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == shared_name
        )

    def test_all_four_delegate_redact_log_text(self):
        for provider, path in EXTRACTORS.items():
            self.assertTrue(
                self._is_stub(self._func(path, "redact_log_text"),
                              "shared_redact_log_text"),
                provider,
            )

    def test_all_four_delegate_register_proxy_for_redaction(self):
        for provider, path in EXTRACTORS.items():
            self.assertTrue(
                self._is_stub(self._func(path, "register_proxy_for_redaction"),
                              "shared_register_proxy_for_redaction"),
                provider,
            )

    def test_ideal_and_twint_delegate_redact_text(self):
        for provider in ("ideal", "twint"):
            self.assertTrue(
                self._is_stub(self._func(EXTRACTORS[provider], "_redact_text"),
                              "shared_redact_text"),
                provider,
            )

    def test_blik_keeps_its_own_redact_text(self):
        node = self._func(EXTRACTORS["blik"], "_redact_text")
        self.assertFalse(self._is_stub(node, "shared_redact_text"),
                         "blik's _redact_text has extra IDEAL_BLIK_CODE logic")

    def test_old_per_module_globals_are_gone(self):
        """The pair moved into RedactionRegistry; leaving it behind would mean
        two sources of truth for what gets redacted."""
        for provider, path in EXTRACTORS.items():
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("_proxy_redaction_values", source, provider)
            self.assertNotIn("_proxy_redaction_lock", source, provider)
            self.assertIn("_proxy_redaction_registry = RedactionRegistry()",
                          source, provider)

    def test_each_delegation_passes_its_own_knobs(self):
        """The values injected at the call site, not just the callee name.

        Without this, swapping ideal's ``limit_env`` to ``TWINT_DUMP_LIMIT`` or
        pointing every extractor at one shared registry object would both pass
        every other test in this file -- the delegation would still *look*
        right while reading the wrong env var / the wrong proxy set.
        """
        expected = {
            "ideal": "IDEAL_DUMP_LIMIT",
            "twint": "TWINT_DUMP_LIMIT",
        }
        for provider, path in EXTRACTORS.items():
            source = path.read_text(encoding="utf-8")
            for func in ("redact_log_text", "register_proxy_for_redaction"):
                node = self._func(path, func)
                call = next(
                    n for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id.startswith("shared_")
                )
                kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
                self.assertEqual(
                    kwargs.get("registry"), "_proxy_redaction_registry",
                    "%s.%s" % (provider, func),
                )
                self.assertEqual(
                    kwargs.get("proxy_label") or kwargs.get("normalize"),
                    "proxy_label" if func == "redact_log_text" else "normalize_proxy_url",
                    "%s.%s" % (provider, func),
                )
            if provider in expected:
                node = self._func(path, "_redact_text")
                call = next(
                    n for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "shared_redact_text"
                )
                kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
                # ast.unparse re-quotes string constants, so compare the value
                self.assertEqual(
                    (kwargs.get("limit_env") or "").strip("'\""),
                    expected[provider], provider,
                )
                self.assertEqual(kwargs.get("env_int"), "env_int", provider)
            else:
                source_names = [
                    n.func.id for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                ]
                self.assertNotIn("shared_redact_text", source_names, provider)

    def test_each_extractor_has_its_own_registry(self):
        """One shared registry would make kakao's log depend on ideal's proxies
        and would write one extractor's credentials into another's dumps."""
        for provider, path in EXTRACTORS.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "RedactionRegistry"
            ]
            self.assertEqual(len(calls), 1, provider)


if __name__ == "__main__":
    unittest.main()

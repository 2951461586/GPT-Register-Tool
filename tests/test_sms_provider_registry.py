"""Tests for ``sms_tool/sms_providers.py`` -- the provider registry.

Why this exists
---------------
The registry is the seam that replaced five hardcoded provider lists (argparse
choices, the ``phone_reuse`` alias table, the env-var lookup, and two C# files).
A seam like that only pays off if its invariants are pinned, because every
failure mode here is silent:

* **An alias owned by two providers.** ``normalize_provider`` builds a dict, so
  the later declaration wins and the earlier spelling silently starts selecting
  a different vendor. The operator sees numbers appear from a service they did
  not configure.
* **A removed value that still resolves.** ``phone_pool`` used to mean "rent
  nothing, read my static list". If it still resolves to *some* provider, a
  stale config starts spending money on rentals instead of failing.
* **A credential pasted into a spec.** This module is deliberately
  credential-free; the fields are prose, URLs, and env-var *names*. Nothing
  structurally stops someone pasting a working key into ``default_endpoint``,
  which is exactly how the benchmarked reference project leaked three vendors'
  keys. ``TestNoCredentialsInRegistry`` is that guard.
* **An unavailable provider offered as a choice.** ``client_available=False``
  exists so a name can be reserved without a surface offering it. If
  ``available_provider_keys`` stops filtering, the UI offers a vendor the
  backend cannot talk to.
"""

import re
import unittest
from dataclasses import fields

from sms_tool import sms_providers as registry
from sms_tool.smsbower import DEFAULT_ENDPOINT


class TestRegistryInvariants(unittest.TestCase):
    def test_the_default_provider_is_declared(self):
        self.assertIn(registry.DEFAULT_PROVIDER, registry.PROVIDERS)

    def test_every_spec_key_matches_its_dict_key(self):
        for key, spec in registry.PROVIDERS.items():
            self.assertEqual(key, spec.key, f"dict key {key!r} != spec.key {spec.key!r}")

    def test_every_canonical_key_is_its_own_alias(self):
        for key in registry.PROVIDERS:
            self.assertEqual(key, registry.normalize_provider(key))
            self.assertIn(key, registry.PROVIDERS[key].aliases)

    def test_no_alias_is_claimed_by_two_providers(self):
        owners: dict[str, list[str]] = {}
        for key, spec in registry.PROVIDERS.items():
            for alias in spec.aliases:
                owners.setdefault(alias, []).append(key)
        collisions = {alias: keys for alias, keys in owners.items() if len(keys) > 1}
        self.assertEqual(collisions, {}, f"alias claimed by several providers: {collisions}")

    def test_aliases_are_stored_lowercase_and_trimmed(self):
        # ``normalize_provider`` lowercases the lookup token, so an alias stored
        # with capitals could never be matched.
        for key, spec in registry.PROVIDERS.items():
            for alias in spec.aliases:
                self.assertEqual(alias, alias.strip().lower(), f"{key}: alias {alias!r}")

    def test_every_available_provider_has_an_endpoint_and_an_env_var(self):
        for key in registry.available_provider_keys():
            spec = registry.PROVIDERS[key]
            self.assertTrue(spec.default_endpoint, f"{key} has no default endpoint")
            self.assertTrue(spec.api_key_env, f"{key} has no api_key_env")
            self.assertTrue(spec.speaks_sms_activate, f"{key} is available but not sms-activate")

    def test_unavailable_providers_are_excluded_from_choices(self):
        unavailable = [k for k, s in registry.PROVIDERS.items() if not s.client_available]
        self.assertTrue(unavailable, "expected at least one reserved-but-unwired provider")
        for key in unavailable:
            self.assertNotIn(key, registry.available_provider_keys())
            # Reserved names still resolve, so a config naming one is
            # *recognised* and can be rejected with a reason rather than
            # silently falling back to the default provider.
            self.assertEqual(key, registry.normalize_provider(key))

    def test_declaration_order_is_preserved_in_both_key_views(self):
        expected = tuple(registry.PROVIDERS)
        self.assertEqual(expected, registry.provider_keys())
        self.assertEqual(
            tuple(k for k in expected if registry.PROVIDERS[k].client_available),
            registry.available_provider_keys(),
        )


class TestNormalization(unittest.TestCase):
    def test_every_alias_resolves_to_its_owner(self):
        for key, spec in registry.PROVIDERS.items():
            for alias in spec.aliases:
                self.assertEqual(key, registry.normalize_provider(alias), f"alias {alias!r}")
                self.assertEqual(key, registry.normalize_provider(alias.upper()), f"alias {alias!r} upper")
                self.assertEqual(key, registry.normalize_provider(f"  {alias}  "), f"alias {alias!r} padded")

    def test_unknown_values_normalize_to_empty(self):
        for value in ("", None, "   ", "nope", "s_m_s_bower", 42):
            self.assertEqual("", registry.normalize_provider(value), repr(value))

    def test_resolve_provider_falls_back_to_the_default(self):
        for value in ("", None, "nope"):
            self.assertEqual(registry.DEFAULT_PROVIDER, registry.resolve_provider(value))
        self.assertEqual("herosms", registry.resolve_provider("Hero-SMS"))

    def test_resolve_provider_honours_an_explicit_default(self):
        self.assertEqual("grizzly", registry.resolve_provider("", default="grizzly"))

    def test_provider_spec_accepts_aliases_and_rejects_unknown(self):
        self.assertIs(registry.PROVIDERS["smsbower"], registry.provider_spec("SMS_Bower"))
        self.assertIsNone(registry.provider_spec("nope"))
        self.assertIsNone(registry.provider_spec(""))

    def test_config_paths_are_namespaced_under_phone_reuse(self):
        spec = registry.PROVIDERS["herosms"]
        self.assertEqual("phone_reuse.herosms", spec.config_section)
        self.assertEqual("phone_reuse.herosms.api_key", spec.api_key_path)
        self.assertEqual("phone_reuse.herosms.endpoint", spec.endpoint_path)
        self.assertEqual("phone_reuse.herosms", registry.config_section("hero_sms"))
        self.assertEqual("", registry.config_section("nope"))

    def test_accessors_return_fallbacks_for_unknown_providers(self):
        self.assertEqual("", registry.default_endpoint("nope"))
        self.assertEqual("fallback", registry.default_endpoint("nope", fallback="fallback"))
        self.assertEqual("", registry.api_key_env("nope"))

    def test_labels_cover_exactly_the_available_providers(self):
        labels = registry.provider_labels()
        self.assertEqual(list(registry.available_provider_keys()), list(labels))
        self.assertEqual("SMSBower", labels["smsbower"])


class TestRemovedStaticPoolValues(unittest.TestCase):
    """The static phone-pool mode is gone; its spellings must not work."""

    def test_every_removed_value_is_recognised_as_removed(self):
        for value in registry.REMOVED_SOURCE_VALUES:
            self.assertTrue(registry.is_removed_source_value(value), value)
            self.assertTrue(registry.is_removed_source_value(value.upper()), value)
            self.assertTrue(registry.removed_source_reason(value), value)

    def test_no_removed_value_is_still_a_provider(self):
        for value in registry.REMOVED_SOURCE_VALUES:
            self.assertEqual("", registry.normalize_provider(value), value)
            self.assertNotIn(value, registry.available_provider_keys())

    def test_ordinary_values_are_not_flagged_as_removed(self):
        for value in ("smsbower", "herosms", "", None, "nope"):
            self.assertFalse(registry.is_removed_source_value(value), repr(value))
            self.assertEqual("", registry.removed_source_reason(value), repr(value))


class TestNoCredentialsInRegistry(unittest.TestCase):
    """The registry is credential-free by policy, enforced structurally.

    A working key is a long unbroken alphanumeric run; every legitimate value
    here is prose, an env-var *name*, or a URL whose longest run is a hostname
    or path segment. Threshold 24 characters separates the two.
    """

    _LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")

    def _string_fields(self):
        for key, spec in registry.PROVIDERS.items():
            for spec_field in fields(spec):
                value = getattr(spec, spec_field.name)
                if isinstance(value, str) and value:
                    yield key, spec_field.name, value

    def test_no_spec_field_contains_a_credential_shaped_token(self):
        offenders = [
            (key, name, self._LONG_TOKEN.search(value).group(0))
            for key, name, value in self._string_fields()
            if self._LONG_TOKEN.search(value)
        ]
        self.assertEqual(offenders, [], f"credential-shaped token in registry: {offenders}")

    def test_endpoints_are_bare_https_urls(self):
        for key, name, value in self._string_fields():
            if name != "default_endpoint":
                continue
            self.assertTrue(value.startswith("https://"), f"{key}: {value!r}")
            self.assertNotIn("?", value, f"{key}: endpoint must carry no query string")
            self.assertNotIn("@", value, f"{key}: endpoint must carry no userinfo")
            self.assertNotIn("api_key", value.lower(), f"{key}: endpoint must carry no key")

    def test_api_key_env_names_are_env_var_shaped(self):
        for key, name, value in self._string_fields():
            if name != "api_key_env":
                continue
            self.assertEqual(value, value.upper(), f"{key}: {value!r}")
            self.assertRegex(value, r"^[A-Z][A-Z0-9_]*_API_KEY$", f"{key}: {value!r}")

    def test_the_module_source_declares_no_key_like_literal(self):
        # Belt and braces: catches a key pasted anywhere in the file, not just
        # into a dataclass field. Only single-token string constants are
        # inspected -- prose in the docstrings is whitespace-separated, so it
        # can never reach the threshold, while a pasted key always does.
        import ast

        with open(registry.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            literal = node.value
            if not literal or any(ch.isspace() for ch in literal):
                continue
            match = re.search(r"[A-Za-z0-9]{24,}", literal)
            if match:
                offenders.append((node.lineno, match.group(0)))
        self.assertEqual(offenders, [], f"key-like literal in sms_providers.py: {offenders}")


class TestSmsBowerEndpointDerivation(unittest.TestCase):
    def test_the_protocol_client_reads_its_endpoint_from_the_registry(self):
        self.assertEqual(registry.PROVIDERS["smsbower"].default_endpoint, DEFAULT_ENDPOINT)

    def test_the_endpoint_is_the_documented_public_host(self):
        # Pins the literal so a registry edit cannot silently repoint the
        # protocol client at another host.
        self.assertEqual("https://smsbower.page/stubs/handler_api.php", DEFAULT_ENDPOINT)


if __name__ == "__main__":
    unittest.main()

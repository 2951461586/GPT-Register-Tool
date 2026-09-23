"""Behaviour tests for ``sms_tool/sms_provider_adapter.py`` (2026-09-03, round 7).

``sms_provider_adapter.py`` is the **adapter dispatch key** for the phone-verification
path: ``phone_reuse._sms_provider_adapter`` calls :func:`provider_name` to decide
which adapter owns a slot. Until 2026-09-22 that dispatch had two branches -- a
rental adapter and a static-URL adapter whose ``complete``/``cancel`` were
no-ops -- so a slot that resolved to the wrong name left its activation open and
billing while the registration had already moved on. That is a money bug with a
silent failure mode, and it is why the fallback value below is load-bearing.

The static branch is gone. Every slot is a rental, so the fallback is no longer
a separate mode: it is :data:`sms_providers.DEFAULT_PROVIDER`. Three properties
of :func:`provider_name` still matter, all pinned below:

1. It **lower-cases** -- ``provider="SMSBower"`` must resolve to the same
   provider as ``"smsbower"``, or a config written with different casing selects
   nothing.
2. It **strips before the truthiness check** -- ``"   "`` is not a name.
3. Its fallback is the **default provider**, not a mode marker. A slot with no
   provider is an ordinary rental for the default vendor.

There is **no network and no I/O** in ``sms_provider_adapter.py`` -- everything in
``ProviderNameTests``, ``AdapterProviderPropertyTests`` and ``LifecycleTests``
is pure. ``DispatcherTests`` imports ``phone_reuse`` and therefore touches the
config singleton, but still performs no I/O.
"""
from __future__ import annotations

import unittest

from sms_tool import sms_provider_adapter, sms_providers


class _Slot:
    """Only ``provider`` is ever read."""

    def __init__(self, provider=None):
        if provider is not None:
            self.provider = provider


class _Adapter(sms_provider_adapter.SmsProviderAdapter):
    pass


class _DefaultProviderAdapter(sms_provider_adapter.SmsProviderAdapter):
    """Mirrors ``phone_reuse._RentalSmsProviderAdapter``."""

    provider_key = sms_providers.DEFAULT_PROVIDER


class ProviderNameTests(unittest.TestCase):
    """The function the dispatcher actually calls."""

    def test_the_default_provider_is_smsbower(self):
        """Pinned once, as a literal. Every fallback assertion below spells
        ``"smsbower"`` rather than ``DEFAULT_PROVIDER`` on purpose: if the
        default ever moves, the literal assertions are what force this file to
        be reviewed instead of silently following along."""
        self.assertEqual("smsbower", sms_providers.DEFAULT_PROVIDER)

    def test_a_missing_attribute_falls_back_to_the_default_provider(self):
        self.assertEqual(sms_provider_adapter.provider_name(_Slot()), "smsbower")

    def test_an_empty_string_falls_back_to_the_default_provider(self):
        self.assertEqual(sms_provider_adapter.provider_name(_Slot("")), "smsbower")

    def test_a_none_value_falls_back_to_the_default_provider(self):
        self.assertEqual(sms_provider_adapter.provider_name(_Slot(None)), "smsbower")

    def test_a_whitespace_only_value_falls_back_to_the_default_provider(self):
        """``str(...).strip() or DEFAULT_PROVIDER`` -- the strip happens *before*
        the truthiness check, so ``"   "`` is not a provider name."""
        self.assertEqual(sms_provider_adapter.provider_name(_Slot("   ")), "smsbower")

    def test_a_false_boolean_falls_back_to_the_default_provider(self):
        """``provider=False`` is falsy → ``or`` kicks in → the default,
        **not** the string ``"False"``."""
        self.assertEqual(sms_provider_adapter.provider_name(_Slot(False)), "smsbower")

    def test_a_lowercase_name_is_returned_unchanged(self):
        self.assertEqual(sms_provider_adapter.provider_name(_Slot("smsbower")), "smsbower")

    def test_names_are_lower_cased(self):
        """🔴 Load-bearing. Drop ``.lower()`` and ``provider="SMSBower"`` stops
        matching the registry, so ``_sms_provider_adapter`` raises -- or worse,
        a future lenient dispatcher rents from the wrong vendor."""
        for value in ("SMSBower", "SMSBOWER", "SmsBower", "HeroSMS", "HERO_SMS"):
            with self.subTest(value=value):
                self.assertEqual(
                    sms_provider_adapter.provider_name(_Slot(value)), value.strip().lower())

    def test_surrounding_whitespace_is_stripped(self):
        for value in ("  smsbower", "smsbower  ", "  smsbower  ", "\tsmsbower\n"):
            with self.subTest(value=value):
                self.assertEqual(
                    sms_provider_adapter.provider_name(_Slot(value)), "smsbower")

    def test_non_string_values_are_stringified(self):
        self.assertEqual(sms_provider_adapter.provider_name(_Slot(12345)), "12345")

    def test_non_string_falsy_values_still_fall_back(self):
        for value in (0, 0.0, [], {}, ()):
            with self.subTest(value=value):
                self.assertEqual(
                    sms_provider_adapter.provider_name(_Slot(value)), "smsbower")

    def test_the_fallback_matches_the_class_default(self):
        """The two resolvers used to disagree here (``"legacy"`` vs the class
        key); they now share one source, which is what makes the dispatcher
        total."""
        self.assertEqual(
            sms_provider_adapter.provider_name(_Slot()),
            sms_provider_adapter.SmsProviderAdapter.provider_key,
        )


class AdapterProviderPropertyTests(unittest.TestCase):
    def test_the_slot_value_wins(self):
        self.assertEqual(_Adapter(_Slot("smspool")).provider, "smspool")

    def test_the_class_default_is_used_when_the_slot_has_nothing(self):
        self.assertEqual(_Adapter(_Slot()).provider, "smsbower")

    def test_an_empty_slot_value_falls_back_to_the_class_default(self):
        self.assertEqual(_Adapter(_Slot("")).provider, "smsbower")

    def test_a_subclass_default_is_used(self):
        self.assertEqual(_DefaultProviderAdapter(_Slot()).provider, "smsbower")

    def test_the_slot_value_beats_the_subclass_default(self):
        self.assertEqual(_DefaultProviderAdapter(_Slot("smspool")).provider, "smspool")

    def test_whitespace_is_stripped(self):
        self.assertEqual(_Adapter(_Slot("  smspool  ")).provider, "smspool")

    def test_a_stripped_empty_value_still_falls_back(self):
        """两层都要各测一遍：基类与子类各有一处 ``or self.provider_key`` ——
        只测基类的话，把第二处改成硬编码字符串是抓不到的。"""
        self.assertEqual(_Adapter(_Slot("   ")).provider, "smsbower")
        self.assertEqual(_DefaultProviderAdapter(_Slot("   ")).provider, "smsbower")

    def test_an_empty_slot_value_falls_back_to_the_subclass_default(self):
        self.assertEqual(_DefaultProviderAdapter(_Slot("")).provider, "smsbower")

    def test_non_string_values_are_stringified(self):
        self.assertEqual(_Adapter(_Slot(7)).provider, "7")

    def test_it_is_a_property_read_at_access_time(self):
        """⚠️ Two things pinned here.  (1) ``provider`` is a ``property`` on the
        base class, so a subclass can override it with its own computation.
        (2) It is re-read on **every access** -- caching it in ``__init__`` would
        break any flow that fills ``slot.provider`` in after constructing the
        adapter (which is exactly what ``_prepare_smsbower_for_send`` does to
        the slot)."""
        self.assertIsInstance(
            sms_provider_adapter.SmsProviderAdapter.__dict__["provider"], property)
        slot = _Slot("smspool")
        adapter = _Adapter(slot)
        self.assertEqual(adapter.provider, "smspool")
        slot.provider = "smsbower"
        self.assertEqual(adapter.provider, "smsbower",
                         "the property must be re-read, not snapshotted")


class DispatchConsistencyTests(unittest.TestCase):
    """The two resolvers look interchangeable. They still differ in one way --
    pin the seam so nobody "unifies" them by accident."""

    def test_they_agree_on_a_plain_lower_case_name(self):
        slot = _Slot("smsbower")
        self.assertEqual(sms_provider_adapter.provider_name(slot),
                         _DefaultProviderAdapter(slot).provider)

    def test_they_agree_on_the_default(self):
        """This used to be the disagreement: ``provider_name`` said ``"legacy"``
        while the adapter property said ``"smsbower"``, so a bare slot was
        described two different ways depending on which one you asked."""
        slot = _Slot()
        self.assertEqual(_DefaultProviderAdapter(slot).provider, "smsbower")
        self.assertEqual(sms_provider_adapter.provider_name(slot), "smsbower")

    def test_the_property_does_not_lower_case(self):
        """⚠️ Pinned: ``provider_name`` lower-cases, ``.provider`` does not.
        The dispatcher calls ``provider_name``, so the registry lookup is
        case-insensitive; ``.provider`` is for reporting."""
        slot = _Slot("SMSBower")
        self.assertEqual(sms_provider_adapter.provider_name(slot), "smsbower")
        self.assertEqual(_DefaultProviderAdapter(slot).provider, "SMSBower")

    def test_the_defaults_are_the_same_string_for_the_base_class(self):
        self.assertEqual(_Adapter(_Slot()).provider,
                         sms_provider_adapter.provider_name(_Slot()))


class LifecycleTests(unittest.TestCase):
    def test_the_slot_is_stored(self):
        slot = _Slot("smspool")
        self.assertIs(_Adapter(slot).slot, slot)

    def test_prepare_defaults_to_true(self):
        """The caller treats ``False`` as "abort this slot"."""
        self.assertTrue(_Adapter(_Slot()).prepare())

    def test_wait_code_is_not_implemented_in_the_base_class(self):
        with self.assertRaises(NotImplementedError):
            _Adapter(_Slot()).wait_code()

    def test_complete_and_cancel_are_no_ops(self):
        adapter = _Adapter(_Slot())
        self.assertIsNone(adapter.complete())
        self.assertIsNone(adapter.cancel())

    def test_the_base_class_can_be_instantiated_despite_being_an_abc(self):
        """⚠️ Pinned: ``ABC`` with no ``@abstractmethod`` does **not** block
        instantiation.  So a new adapter that forgets ``wait_code`` is accepted
        by ``_sms_provider_adapter`` and only blows up later, mid-registration.
        This test is the tripwire for "someone finally added @abstractmethod"."""
        self.assertIsInstance(_Adapter(_Slot()), sms_provider_adapter.SmsProviderAdapter)

    def test_the_base_provider_key_is_the_registry_default(self):
        self.assertEqual(sms_provider_adapter.SmsProviderAdapter.provider_key, "smsbower")

    def test_a_subclass_key_does_not_leak_into_the_base_class(self):
        class _OtherAdapter(sms_provider_adapter.SmsProviderAdapter):
            provider_key = "smspool"

        self.assertEqual(_OtherAdapter.provider_key, "smspool")
        self.assertEqual(sms_provider_adapter.SmsProviderAdapter.provider_key, "smsbower")

    def test_subclasses_inherit_the_default_lifecycle(self):
        adapter = _DefaultProviderAdapter(_Slot())
        self.assertTrue(adapter.prepare())
        self.assertIsNone(adapter.complete())
        self.assertIsNone(adapter.cancel())


class DispatcherTests(unittest.TestCase):
    """``phone_reuse._sms_provider_adapter`` -- the seam this file exists for.

    The removed static adapter made an unrecognised provider name *harmless*
    (it just meant "poll a URL"), which is why a casing slip could silently skip
    ``complete``/``cancel``. Now an unrecognised name raises, so the failure is
    at dispatch time instead of on the vendor's billing page.
    """

    def _adapter(self, provider):
        from sms_tool import phone_reuse

        return phone_reuse._sms_provider_adapter(phone_reuse.PhoneSlot(phone="+10000000000", provider=provider))

    def test_every_available_provider_routes_to_the_rental_adapter(self):
        from sms_tool import phone_reuse

        for key in sms_providers.available_provider_keys():
            with self.subTest(provider=key):
                adapter = self._adapter(key)
                self.assertIsInstance(adapter, phone_reuse._RentalSmsProviderAdapter)
                self.assertEqual(key, adapter.provider)

    def test_a_removed_static_source_name_raises(self):
        for value in sorted(sms_providers.REMOVED_SOURCE_VALUES):
            with self.subTest(provider=value):
                with self.assertRaises(ValueError) as ctx:
                    self._adapter(value)
                self.assertIn("unsupported SMS provider", str(ctx.exception))

    def test_an_unknown_name_raises_and_lists_the_supported_ones(self):
        with self.assertRaises(ValueError) as ctx:
            self._adapter("nope")
        message = str(ctx.exception)
        self.assertIn("'nope'", message)
        for key in sms_providers.available_provider_keys():
            self.assertIn(key, message)

    def test_a_reserved_but_unwired_provider_raises(self):
        """``nexsms`` is declared so the name is taken, but no client exists --
        it must not be dispatchable."""
        unwired = [k for k, s in sms_providers.PROVIDERS.items() if not s.client_available]
        self.assertTrue(unwired)
        for key in unwired:
            with self.subTest(provider=key):
                with self.assertRaises(ValueError):
                    self._adapter(key)

    def test_a_bare_slot_routes_to_the_default_provider(self):
        from sms_tool import phone_reuse

        adapter = phone_reuse._sms_provider_adapter(phone_reuse.PhoneSlot(phone="+10000000000"))
        self.assertIsInstance(adapter, phone_reuse._RentalSmsProviderAdapter)
        self.assertEqual("smsbower", adapter.provider)


if __name__ == "__main__":
    unittest.main()

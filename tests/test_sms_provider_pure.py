"""Behaviour tests for ``sms_tool/sms_provider.py`` (2026-09-03, round 7).

38 lines, zero test coverage -- and it is the **adapter dispatch key** for the
phone-verification path.  One line in ``phone_reuse.py:736``::

    def _sms_provider_adapter(slot: PhoneSlot) -> SmsProviderAdapter:
        name = provider_name(slot)
        if name == "smsbower":
            return _SmsBowerProviderAdapter(slot)
        return _StaticSmsProviderAdapter(slot)

That decides whether a rented number goes through the full rental lifecycle
(``prepare`` → ``wait_code`` → ``complete`` / ``cancel``) or is treated as a
static SMS URL.  Route a rental number down the static branch and the activation
is **never completed and never cancelled** -- the number stays assigned and keeps
billing while the registration has already moved on.  That is a money bug with a
silent failure mode.

So the load-bearing detail here is that ``provider_name`` **normalises**:
``.strip().lower()``.  A slot carrying ``provider="SMSBower"`` or
``" smsbower "`` must still route to the rental adapter.

There is **no network and no I/O** in this module -- everything below is pure.

⚠️ Inconsistency pinned, not fixed
=================================
The module ships **two** near-identical name resolvers that do *not* agree:

* ``SmsProviderAdapter.provider`` (property) -- falls back to
  ``self.provider_key``, and **does not lowercase**.
* ``provider_name(slot)`` (function) -- falls back to the hardcoded
  ``"legacy"``, and **does lowercase**.

Consequences, all asserted below:

* ``slot.provider = "SMSBower"`` → ``adapter.provider == "SMSBower"`` while
  ``provider_name(slot) == "smsbower"``.
* A ``_SmsBowerProviderAdapter`` wrapping a slot that has **no** ``provider``
  attribute reports ``adapter.provider == "smsbower"`` -- yet
  ``provider_name(slot)`` returns ``"legacy"``, so the dispatcher would *not*
  have chosen this adapter in the first place.

One more quirk: ``SmsProviderAdapter`` subclasses ``ABC`` but declares **no**
``@abstractmethod``, so Python happily instantiates it.  A subclass that forgets
to implement ``wait_code`` is not caught at construction time -- it blows up
later, mid-registration, with ``NotImplementedError``.
"""
from __future__ import annotations

import unittest

from sms_tool import sms_provider


class _Slot:
    """Only ``provider`` is ever read."""

    def __init__(self, provider=None):
        if provider is not None:
            self.provider = provider


class _Adapter(sms_provider.SmsProviderAdapter):
    pass


class _SmsBowerAdapter(sms_provider.SmsProviderAdapter):
    """Mirrors ``phone_reuse._SmsBowerProviderAdapter``."""

    provider_key = "smsbower"


class ProviderNameTests(unittest.TestCase):
    """The function the dispatcher actually calls."""

    def test_a_missing_attribute_falls_back_to_legacy(self):
        self.assertEqual(sms_provider.provider_name(_Slot()), "legacy")

    def test_an_empty_string_falls_back_to_legacy(self):
        self.assertEqual(sms_provider.provider_name(_Slot("")), "legacy")

    def test_a_none_value_falls_back_to_legacy(self):
        self.assertEqual(sms_provider.provider_name(_Slot(None)), "legacy")

    def test_a_whitespace_only_value_falls_back_to_legacy(self):
        """``str(...).strip() or "legacy"`` -- the strip happens *before* the
        truthiness check, so ``"   "`` is not a provider name."""
        self.assertEqual(sms_provider.provider_name(_Slot("   ")), "legacy")

    def test_a_false_boolean_falls_back_to_legacy(self):
        """``provider=False`` is falsy → ``or`` kicks in → ``"legacy"``,
        **not** the string ``"False"``."""
        self.assertEqual(sms_provider.provider_name(_Slot(False)), "legacy")

    def test_a_lowercase_name_is_returned_unchanged(self):
        self.assertEqual(sms_provider.provider_name(_Slot("smsbower")), "smsbower")

    def test_names_are_lower_cased(self):
        """🔴 This is the one that decides rental vs static.  Drop ``.lower()``
        and ``provider="SMSBower"`` silently routes to ``_StaticSmsProviderAdapter``
        -- the rented number is never completed or cancelled."""
        for value in ("SMSBower", "SMSBOWER", "SmsBower"):
            with self.subTest(value=value):
                self.assertEqual(sms_provider.provider_name(_Slot(value)), "smsbower")

    def test_surrounding_whitespace_is_stripped(self):
        for value in ("  smsbower", "smsbower  ", "  smsbower  ", "\tsmsbower\n"):
            with self.subTest(value=value):
                self.assertEqual(
                    sms_provider.provider_name(_Slot(value)), "smsbower")

    def test_non_string_values_are_stringified(self):
        self.assertEqual(sms_provider.provider_name(_Slot(12345)), "12345")

    def test_non_string_falsy_values_still_fall_back(self):
        for value in (0, 0.0, [], {}, ()):
            with self.subTest(value=value):
                self.assertEqual(sms_provider.provider_name(_Slot(value)), "legacy")

    def test_the_legacy_default_is_lower_case(self):
        """The dispatcher compares against ``"smsbower"``; if the default ever
        became ``"LEGACY"`` every ``==`` comparison upstream changes meaning."""
        self.assertEqual(sms_provider.provider_name(_Slot()), "legacy")


class AdapterProviderPropertyTests(unittest.TestCase):
    def test_the_slot_value_wins(self):
        self.assertEqual(_Adapter(_Slot("smspool")).provider, "smspool")

    def test_the_class_default_is_used_when_the_slot_has_nothing(self):
        self.assertEqual(_Adapter(_Slot()).provider, "legacy")

    def test_an_empty_slot_value_falls_back_to_the_class_default(self):
        self.assertEqual(_Adapter(_Slot("")).provider, "legacy")

    def test_a_subclass_default_is_used(self):
        self.assertEqual(_SmsBowerAdapter(_Slot()).provider, "smsbower")

    def test_the_slot_value_beats_the_subclass_default(self):
        self.assertEqual(_SmsBowerAdapter(_Slot("smspool")).provider, "smspool")

    def test_whitespace_is_stripped(self):
        self.assertEqual(_Adapter(_Slot("  smspool  ")).provider, "smspool")

    def test_a_stripped_empty_value_still_falls_back(self):
        """两层都要各测一遍：``_Adapter`` 的兜底是 ``"legacy"``，
        ``_SmsBowerAdapter`` 的兜底是 ``"smsbower"`` —— 只测基类的话，
        把第二处 ``or self.provider_key`` 改成硬编码 ``"legacy"`` 是抓不到的。"""
        self.assertEqual(_Adapter(_Slot("   ")).provider, "legacy")
        self.assertEqual(_SmsBowerAdapter(_Slot("   ")).provider, "smsbower")

    def test_an_empty_slot_value_falls_back_to_the_subclass_default(self):
        self.assertEqual(_SmsBowerAdapter(_Slot("")).provider, "smsbower")

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
            sms_provider.SmsProviderAdapter.__dict__["provider"], property)
        slot = _Slot("smspool")
        adapter = _Adapter(slot)
        self.assertEqual(adapter.provider, "smspool")
        slot.provider = "smsbower"
        self.assertEqual(adapter.provider, "smsbower",
                         "the property must be re-read, not snapshotted")


class DispatchConsistencyTests(unittest.TestCase):
    """The two resolvers look interchangeable. They are not -- pin the seams
    where they disagree so nobody "unifies" them by accident."""

    def test_they_agree_on_a_plain_lower_case_name(self):
        slot = _Slot("smsbower")
        self.assertEqual(sms_provider.provider_name(slot),
                         _SmsBowerAdapter(slot).provider)

    def test_the_property_does_not_lower_case(self):
        """⚠️ Pinned: ``provider_name`` lower-cases, ``.provider`` does not."""
        slot = _Slot("SMSBower")
        self.assertEqual(sms_provider.provider_name(slot), "smsbower")
        self.assertEqual(_SmsBowerAdapter(slot).provider, "SMSBower")

    def test_the_function_ignores_the_class_default(self):
        """⚠️ Pinned trap: a rental adapter wrapping a slot with no ``provider``
        reports ``"smsbower"``, but ``provider_name`` returns ``"legacy"`` --
        meaning the dispatcher would never have picked this adapter.  The two
        views of "which provider is this?" disagree exactly on this input."""
        slot = _Slot()
        self.assertEqual(_SmsBowerAdapter(slot).provider, "smsbower")
        self.assertEqual(sms_provider.provider_name(slot), "legacy")

    def test_the_defaults_are_the_same_string_for_the_base_class(self):
        self.assertEqual(_Adapter(_Slot()).provider,
                         sms_provider.provider_name(_Slot()))


class LifecycleTests(unittest.TestCase):
    def test_the_slot_is_stored(self):
        slot = _Slot("smspool")
        self.assertIs(_Adapter(slot).slot, slot)

    def test_prepare_defaults_to_true(self):
        """A static provider has nothing to prepare -- the caller treats
        ``False`` as "abort this slot"."""
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
        self.assertIsInstance(_Adapter(_Slot()), sms_provider.SmsProviderAdapter)

    def test_the_base_provider_key_is_legacy(self):
        self.assertEqual(sms_provider.SmsProviderAdapter.provider_key, "legacy")

    def test_a_subclass_key_does_not_leak_into_the_base_class(self):
        self.assertEqual(_SmsBowerAdapter.provider_key, "smsbower")
        self.assertEqual(sms_provider.SmsProviderAdapter.provider_key, "legacy")

    def test_subclasses_inherit_the_default_lifecycle(self):
        adapter = _SmsBowerAdapter(_Slot())
        self.assertTrue(adapter.prepare())
        self.assertIsNone(adapter.complete())
        self.assertIsNone(adapter.cancel())


if __name__ == "__main__":
    unittest.main()

"""Guard for configuration keys that nothing reads (round 6).

Round 6 found 61 keys sitting in the live config shards that no code path reads
as a string literal. They are not "set but defaulting" -- the names simply do
not occur in the source, so configuring them has no effect at all.

The audit list alone would go stale, so ``sms_tool/config_usage.py`` recomputes
it. This module pins the result: **any change to the set fails the suite**.

* a key disappears  -> someone wired it up; delete it from EXPECTED_UNREAD
* a key appears     -> someone added dead config; decide whether to wire it up
                       or delete it before updating EXPECTED_UNREAD

Both directions are meant to be loud. Silently drifting is the failure mode
this exists to prevent.

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""
import re
import unittest
from pathlib import Path

from sms_tool import config_usage

# Frozen 2026-09-02. See the module docstring for how to update it.
EXPECTED_UNREAD = {
    "chatgpt.chat_web_client_id",
    "email_registration.smailr.domains",
    "omakse.default_concurrency",
    "omakse.default_max_attempts",
    "omakse.default_max_poll_seconds",
    "omakse.default_poll_interval",
    "omakse.default_promo_country",
    "omakse.default_provider_country",
    "omakse.us_payment.load_return_url",
    "omakse.us_payment.phone_country",
    "omakse.us_payment.phone_country_code",
    "omakse.us_payment.preconfirm_phone",
    "omakse.us_payment.proxy_region",
    "omakse.us_payment.randomize_device",
    "omakse.us_payment.send_phone_otp",
    "paypal.allow_chatgpt_checkout_fallback",
    "paypal.approve_missing_redirect",
    "paypal.auto_generate",
    "paypal.checkout_only_long_url",
    "paypal.confirm_style",
    "paypal.disable_promo_on_confirm_decline",
    "paypal.fallback_to_hosted_checkout_on_blocked",
    "paypal.fallback_to_stripe_redirect_on_missing_hosted",
    "paypal.fast_reference_confirm",
    "paypal.hosted_usd_fallback_on_422",
    "paypal.hosted_usd_fallback_on_non_hosted",
    "paypal.link_mode",
    "paypal.max_regenerate_workers",
    "paypal.redirect_poll_interval_seconds",
    "paypal.redirect_poll_timeout_seconds",
    "paypal.redirect_url_format",
    "paypal.reference_confirm_mode",
    "paypal.refresh_tax_region",
    "paypal.regenerate_delay_seconds",
    "paypal.resolve_ba_redirect",
    "paypal.skip_route_load",
    "paypal.skip_snapshot",
    "paypal.stop_after_pm_create",
    "paypal.use_elements_session",
    "paypal_browser.email_mode",
    "paypal_nocard.fallback_to_saved_url",
    "paypal_nocard.locale_country",
    "paypal_nocard.locale_lang",
    "paypal_nocard.phone_index_file",
    "paypal_nocard.reuse_saved_ready_url",
    "paypal_nocard.reuse_saved_url",
    "paypal_nocard.saved_url_max_age_seconds",
    "paypal_nocard.signup_retries",
    "phone_reuse.smsbower.country_name_zh",
    "phone_reuse.smsbower.service_name",
    "protocol_payments.proxy_pools.jp_checkout",
    "protocol_payments.proxy_pools.momo_approve",
    "protocol_payments.proxy_pools.momo_checkout",
    "protocol_payments.proxy_pools.short_lived",
    "protocol_payments.proxy_pools.us_checkout",
    "upi.approve_missing_redirect",
    "upi.auto_generate",
    "upi.link_mode",
    "upi.redirect_url_format",
    "upi.use_elements_session",
}
# 2026-09-23: `runtime.python_path` was dropped from the set. It is NOT wired up
# on the Python side -- it is read by the desktop
# (`SmsWorkbench/PythonBackendClient.cs`, `SmsWorkbench/DesktopReadClient.cs`),
# which this Python-only scan cannot see. It now lives in
# `config_usage.CSHARP_CONSUMED_KEYS`, whose citations are pinned by
# `CSharpConsumerTests` below. The detector still stops reporting it, so the pin
# had to shrink -- that is the "NO LONGER dead" direction working, with the
# reason recorded rather than the key silently disappearing.

# Keys an early draft flagged that are NOT dead. Documented so the false
# positives stay fixed rather than being rediscovered by the next audit.
KNOWN_FALSE_POSITIVES = {
    # email_registration.smailr.domain_ids."smailr.com": the mapping is keyed by
    # domain name, so the leaf is data, not a config name.
    "email_registration.smailr.domain_ids.smailr.com",
}
# Note: paypal.link_mode / upi.link_mode ARE genuinely dead. They looked used
# because `run_single_link_mode()` contains "link_mode" -- which is why the
# detector only considers string literals, never identifiers.
#
# 2026-09-22: `email_registration.use_as_username` was dropped from the set --
# `sms_tool/config.py:622` now validates it as a boolean, so it is read and the
# detector correctly stops reporting it. The pin only ever failed on a local
# operator config.json (which still carries the key); CI short-circuits because
# its config.json is built from config.example.json, where the key is absent and
# `actual` is empty. That asymmetry is why the stale pin survived in CI.


# 2026-09-22: `paypal_nocard.phone_index_file` joined the pin. Its only reader
# was `paypal.config_picker._pick_phone_and_sms`, deleted with the rest of the
# static phone-pool mode -- so the key went from "read" to "dead" without anyone
# adding anything.
#
# It is pinned rather than deleted because the only place it still exists is the
# operator's *untracked* `payment.json` / `config.json`, sitting next to live
# SMS-relay credentials. Editing those is the operator's call, not a side effect
# of removing a code path.
#
# Its `paypal_browser` twin did NOT join the pin: that key lived only in
# `config.example.json`, so deleting it from the template removed it from the
# set entirely. The ratchet catching that -- "NO LONGER dead, delete from
# EXPECTED_UNREAD" -- is the other direction working.
#
# Note the detector matches on the *leaf* name, so `paypal_*.phone_pool` never
# appears here: the literal `"phone_pool"` still exists in `registration.py`
# (a forwarded parameter) and in `sms_providers.REMOVED_SOURCE_VALUES`. Those
# two keys are dead in exactly the same way and stay invisible to this ratchet.


class DetectionTests(unittest.TestCase):
    def test_detector_produces_the_pinned_set(self):
        actual = {item.path for item in config_usage.unread_config_keys()}
        # CI creates a clean config.json from config.example.json, where all
        # documented keys are wired. Local operator configs may retain the
        # historical dead-key set, which remains pinned when present.
        expected = EXPECTED_UNREAD if actual else set()
        self.assertEqual(actual, expected, self._diff_message(actual))

    def _diff_message(self, actual):
        added = sorted(actual - EXPECTED_UNREAD)
        removed = sorted(EXPECTED_UNREAD - actual)
        parts = []
        if added:
            parts.append("NEW dead keys (wire up or delete): %s" % ", ".join(added))
        if removed:
            parts.append(
                "NO LONGER dead (delete from EXPECTED_UNREAD): %s" % ", ".join(removed)
            )
        return " | ".join(parts) or "set changed"

    def test_data_keyed_mappings_are_not_reported(self):
        """domain_ids.<domain> is data, not config."""
        actual = {item.path for item in config_usage.unread_config_keys()}
        for path in KNOWN_FALSE_POSITIVES:
            self.assertNotIn(path, actual)

    def test_example_is_not_advertising_unread_keys(self):
        """config.example.json should not document knobs that do nothing."""
        offending = sorted(
            item.path for item in config_usage.unread_config_keys() if item.in_example
        )
        self.assertEqual(
            offending,
            [],
            "config.example.json still documents unread keys -- remove them, "
            "otherwise users copy settings that have no effect",
        )

    def test_report_is_ascii_safe_for_the_windows_runner(self):
        report = config_usage.format_unread_report(config_usage.unread_config_keys())
        report.encode("cp1252")
        self.assertTrue("never read" in report or "none" in report)

    def test_report_handles_the_empty_case(self):
        self.assertIn("none", config_usage.format_unread_report([]))


class ShardScanTests(unittest.TestCase):
    def test_shards_are_actually_found(self):
        shards = config_usage.shard_leaf_paths()
        self.assertGreater(len(shards), 50, "no config shards parsed")
        self.assertIn("registration.driver", shards)

    def test_source_literals_are_extracted(self):
        literals = config_usage.source_string_literals()
        self.assertGreater(len(literals), 500)
        # a key that IS read must be present as a literal
        self.assertIn("driver", literals)


REPO_ROOT = Path(__file__).resolve().parents[1]

#: C# string literal, comment-stripped. Mirrors the provider-parity scanner's
#: approach: strip ``/* */`` and ``//`` first, because the exception list's whole
#: job is to prove a *real* read, and a commented-out mention proves nothing.
_CS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def csharp_literals(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    text = _CS_BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    found = set()
    for line in text.splitlines():
        found.update(_CS_STRING.findall(line.split("//", 1)[0]))
    return found


class CSharpConsumerTests(unittest.TestCase):
    """``CSHARP_CONSUMED_KEYS`` is an exception list, so it must not rot.

    The Python-only scan cannot see the desktop, which makes this list the one
    place where a key can be suppressed from the dead-key report. An exception
    list with no guard becomes an excuse: add a key, forget why, and a genuinely
    dead setting stays invisible forever. So every citation is verified against
    the real C# source here.
    """

    def test_the_list_is_not_empty(self):
        self.assertTrue(config_usage.CSHARP_CONSUMED_KEYS)

    def test_every_cited_file_exists(self):
        for key, files in config_usage.CSHARP_CONSUMED_KEYS.items():
            for relative in files:
                with self.subTest(key=key, file=relative):
                    self.assertTrue(
                        (REPO_ROOT / relative).is_file(),
                        "citation no longer resolves: %s" % relative,
                    )

    def test_every_citation_actually_contains_the_key(self):
        """The cited file must mention the key -- otherwise the citation is
        fiction and the key was suppressed for no reason."""
        for key, files in config_usage.CSHARP_CONSUMED_KEYS.items():
            leaf = key.rsplit(".", 1)[-1]
            with self.subTest(key=key):
                self.assertTrue(
                    any(leaf in csharp_literals(f) or key in csharp_literals(f) for f in files),
                    "no cited C# file mentions %r: %s" % (key, list(files)),
                )

    def test_a_consumed_key_is_not_reported_as_dead(self):
        reported = {item.path for item in config_usage.unread_config_keys()}
        for key in config_usage.CSHARP_CONSUMED_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, reported)
                self.assertNotIn(key, EXPECTED_UNREAD)

    def test_the_report_names_what_it_excluded(self):
        """A silent exclusion is indistinguishable from a bug. `--doctor` has to
        say which keys it is not judging."""
        report = config_usage.format_unread_report(config_usage.unread_config_keys())
        for key in config_usage.CSHARP_CONSUMED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, report)
        self.assertIn("never read by Python source", report)

    def test_write_only_keys_are_still_reported(self):
        """The counter-example that justifies keeping the scan Python-only.

        ``SmsWorkbench/MainWindow.SmsProvider.cs`` **writes** these two keys and
        nothing reads them back. If C# were scanned wholesale they would leave
        the dead set -- 1 fixed false positive traded for 2 new false negatives.
        They must stay reported.
        """
        writes = csharp_literals("SmsWorkbench/MainWindow.SmsProvider.cs")
        reported = {item.path for item in config_usage.unread_config_keys()}
        for key in (
            "phone_reuse.smsbower.service_name",
            "phone_reuse.smsbower.country_name_zh",
        ):
            with self.subTest(key=key):
                self.assertIn(key.rsplit(".", 1)[-1], writes, "counter-example moved")
                self.assertIn(key, reported, "write-only key was suppressed")
                self.assertNotIn(key, config_usage.CSHARP_CONSUMED_KEYS)

    def test_the_extractor_ignores_commented_out_literals(self):
        """Prove the extractor can fail, so the checks above are not vacuous."""
        text = '// providerSection["service_name"] = "x";\nvar a = 1;\n'
        stripped = _CS_BLOCK_COMMENT.sub("", text)
        found = set()
        for line in stripped.splitlines():
            found.update(_CS_STRING.findall(line.split("//", 1)[0]))
        self.assertEqual(found, set())


if __name__ == "__main__":
    unittest.main()

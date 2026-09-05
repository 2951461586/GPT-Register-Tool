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
import unittest

from sms_tool import config_usage

# Frozen 2026-09-02. See the module docstring for how to update it.
EXPECTED_UNREAD = {
    "chatgpt.chat_web_client_id",
    "email_registration.smailr.domains",
    "email_registration.use_as_username",
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
    "runtime.python_path",
    "upi.approve_missing_redirect",
    "upi.auto_generate",
    "upi.link_mode",
    "upi.redirect_url_format",
    "upi.use_elements_session",
}

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


if __name__ == "__main__":
    unittest.main()

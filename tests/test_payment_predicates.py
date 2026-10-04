"""Tests for services/protocol-payment/common/payment_predicates.py.

Stage-1 batch of the protocol-payment extractor consolidation: nine predicates
that were byte-identical copies in ideal / twint (``is_redirect_like_url``
differed only in the redirect host + marker, now carried by ``ProviderProfile``).

The differential proof of equivalence against HEAD lives in
``runtime/tmp/_payment_predicates_diff_verify.py`` (368 checks, zero divergence);
these tests pin the behaviour so a one-sided edit cannot silently change it.
"""

import sys
import unittest
from pathlib import Path

# Load as part of the ``common`` package so the relative imports resolve.
_SERVICES = Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from common import payment_predicates as PP  # noqa: E402
from common.provider_profile import IDEAL_PROFILE, TWINT_PROFILE  # noqa: E402


class ProviderProfileRedirectDataTests(unittest.TestCase):
    def test_profiles_carry_their_redirect_domain_and_marker(self):
        self.assertEqual((IDEAL_PROFILE.redirect_domain, IDEAL_PROFILE.redirect_marker), ("ideal.nl", "ideal"))
        self.assertEqual((TWINT_PROFILE.redirect_domain, TWINT_PROFILE.redirect_marker), ("twint.ch", "twint"))


class CheckoutPayloadPredicateTests(unittest.TestCase):
    def test_is_checkout_not_active_error(self):
        self.assertTrue(PP.is_checkout_not_active_error("checkout_not_active_session"))
        self.assertTrue(PP.is_checkout_not_active_error({"e": "checkout_not_active_session"}))
        self.assertFalse(PP.is_checkout_not_active_error(None))
        self.assertFalse(PP.is_checkout_not_active_error("other"))

    def test_checkout_response_has_promo(self):
        self.assertFalse(PP.checkout_response_has_promo(None))
        self.assertFalse(PP.checkout_response_has_promo({}))
        self.assertFalse(PP.checkout_response_has_promo({"promo_campaign": {}}))
        self.assertTrue(PP.checkout_response_has_promo({"promo_campaign": {"x": 1}}))
        self.assertTrue(PP.checkout_response_has_promo({"promo_credit_grant": "yes"}))

    def test_checkout_response_has_trial(self):
        self.assertFalse(PP.checkout_response_has_trial(None))
        self.assertTrue(PP.checkout_response_has_trial({"one_click_trial_eligible": True}))
        self.assertFalse(PP.checkout_response_has_trial({"one_click_trial_eligible": 1}))
        self.assertTrue(PP.checkout_response_has_trial({"subscription_data": {"trial_period_days": 30}}))
        self.assertFalse(PP.checkout_response_has_trial({"subscription_data": {"trial_period_days": "0"}}))
        self.assertTrue(PP.checkout_response_has_trial({"trial_end": "2026"}))


class UrlPredicateTests(unittest.TestCase):
    def test_is_resource_url(self):
        self.assertTrue(PP.is_resource_url("https://example.com/app.js"))
        self.assertTrue(PP.is_resource_url("https://example.com/logo.PNG"))
        self.assertFalse(PP.is_resource_url("https://pay.ideal.nl/checkout"))

    def test_is_qr_candidate(self):
        self.assertTrue(PP.is_qr_candidate("data:image/png;base64,AA"))
        self.assertTrue(PP.is_qr_candidate("https://x.test/qr-code.png"))
        self.assertTrue(PP.is_qr_candidate("https://x.test/qrcode"))
        self.assertFalse(PP.is_qr_candidate("https://x.test/plain"))

    def test_extract_qr_candidates_dedupes_and_skips_static_hosts(self):
        payload = {"urls": ["https://x.test/qr.png", "https://x.test/qr.png", "https://x.test/app.js"]}
        self.assertEqual(PP.extract_qr_candidates(payload), ["https://x.test/qr.png"])


class RedirectLikeUrlTests(unittest.TestCase):
    def test_rejects_non_http_and_resources(self):
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, None))
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, "ftp://x/y"))
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, "https://x.test/app.js"))

    def test_stripe_hosts_are_always_redirect_like(self):
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://hooks.stripe.com/x"))
        self.assertTrue(PP.is_redirect_like_url(TWINT_PROFILE, "https://payments.stripe.com/x"))

    def test_provider_domain_is_profile_specific(self):
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://pay.ideal.nl/x"))
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://ideal.nl/"))
        self.assertFalse(PP.is_redirect_like_url(TWINT_PROFILE, "https://pay.ideal.nl/x"))
        self.assertTrue(PP.is_redirect_like_url(TWINT_PROFILE, "https://pay.twint.ch/x"))
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, "https://pay.twint.ch/x"))

    def test_provider_marker_is_profile_specific(self):
        self.assertTrue(PP.is_redirect_like_url(TWINT_PROFILE, "https://gateway.test/twint/pay"))
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, "https://gateway.test/twint/pay"))
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://gateway.test/ideal/pay"))

    def test_shared_markers_and_action_field(self):
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://x.test/redirect_to_url"))
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://x.test/authenticate"))
        # ``from_action_field`` is evaluated *after* the resource check, so a
        # static asset stays non-redirect even from an action field.
        self.assertTrue(PP.is_redirect_like_url(IDEAL_PROFILE, "https://x.test/plain", True))
        self.assertFalse(PP.is_redirect_like_url(IDEAL_PROFILE, "https://x.test/app.js", True))


class ErrorPredicateTests(unittest.TestCase):
    def test_is_approve_failure_error(self):
        self.assertTrue(PP.is_approve_failure_error("approve failed"))
        self.assertTrue(PP.is_approve_failure_error("ChatGPT approve timeout"))
        self.assertFalse(PP.is_approve_failure_error("other"))

    def test_should_retry_second_confirm_after_approve(self):
        for marker in (
            "checkout_upcoming_invoice_mismatch",
            "redirect url resolution timeout",
            "missing_redirect",
        ):
            self.assertTrue(PP.should_retry_second_confirm_after_approve(marker), marker)
        self.assertFalse(PP.should_retry_second_confirm_after_approve(None))
        self.assertFalse(PP.should_retry_second_confirm_after_approve("other"))


if __name__ == "__main__":
    unittest.main()

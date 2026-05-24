import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_extensions" / "paypal_autofill"


class PayPalAutofillOtpTests(unittest.TestCase):
    def test_otp_detection_excludes_phone_number_fields_in_both_worlds(self):
        for name in ("content.js", "background.js"):
            source = (EXTENSION_DIR / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIn("const isPhoneNumberEntry", source)
                self.assertRegex(source, r"type === [\"']tel[\"']")
                self.assertIn('autocomplete === "tel"', source)
                self.assertIn("!isPhoneNumberEntry(el, h)", source)
                self.assertIn("isDefinitelyBilling(h) || isPhoneNumberEntry(el, h)", source)

    def test_main_world_otp_detection_keeps_compact_multi_input_fallback(self):
        source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
        match = re.search(r"async function mainWorldOtpFill\(payload\) \{(?P<body>.*?)\n\}\n\nchrome\.runtime", source, re.S)
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("const compact = allInputs.filter", body)
        self.assertIn("rect.width >= 24", body)
        self.assertIn("rect.height >= 28", body)

    def test_paypal_hosted_otp_inputs_are_explicitly_detected(self):
        for name in ("content.js", "background.js"):
            source = (EXTENSION_DIR / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIn("ci-ciBasic-${index}", source)
                self.assertIn("hosted.length >= 6", source)

    def test_paypal_sms_code_extraction_handles_hosted_messages(self):
        source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertIn("paypalSpaced", source)
        self.assertIn("thanks\\s+for\\s+confirming", source)
        self.assertIn("ignoreKeys", source)
        self.assertIn("order_id", source)

    def test_otp_submit_continues_to_paypal_agree_and_continue(self):
        source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertIn("postOtpApproveWatchRunning", source)
        self.assertIn("watchApproveAfterOtpSubmit", source)
        self.assertIn("clickPayPalApproveButton", source)
        self.assertIn("Clicked PayPal Agree and Continue", source)
        self.assertIn("if (submitted) watchApproveAfterOtpSubmit(request.source || \"content-otp\")", source)
        self.assertIn("if (mainResult.submitted) watchApproveAfterOtpSubmit(request.source || \"main-world-otp\")", source)
        self.assertIn("if (request.submit) watchApproveAfterOtpSubmit(source)", source)

    def test_generic_paypal_pages_watch_for_agree_and_continue(self):
        source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertIn("genericPayPalApproveWatchStarted", source)
        self.assertIn("watchGenericPayPalApproveRoute", source)
        self.assertIn("Clicked PayPal Agree and Continue (${source})", source)
        self.assertIn('watchGenericPayPalApproveRoute("page-load")', source)
        self.assertIn("set up once|pay faster|automatic payments|billing agreement", source)


if __name__ == "__main__":
    unittest.main()

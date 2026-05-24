import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_extensions" / "paypal_autofill"


class PayPalAutofillCheckoutWebTests(unittest.TestCase):
    def test_checkoutweb_main_world_resets_react_tracker_for_inputs(self):
        source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
        match = re.search(
            r"async function mainWorldCheckoutWebFill\(payload\) \{(?P<body>.*?)\n\}\n\nasync function mainWorldOtpFill",
            source,
            re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("if (el._valueTracker) el._valueTracker.setValue(\"\")", body)
        self.assertIn('document.execCommand("insertText"', body)
        self.assertIn("new InputEvent(\"input\"", body)

    def test_checkoutweb_required_fields_include_identity_fields(self):
        for name in ("content.js", "background.js"):
            source = (EXTENSION_DIR / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertRegex(source, r"\[\"email\", \"phone\", \"cardNumber\"")
                self.assertIn("phone: (", source)
                self.assertIn("email: (", source)

    def test_checkoutweb_does_not_treat_hidden_billing_as_otp_progress(self):
        source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertNotIn('reason: "billing_form_hidden"', source)
        self.assertIn('reason: "checkout_pending_no_otp"', source)
        self.assertIn("checkout submit remained on billing form; not starting OTP poll", source)
        self.assertLess(
            source.index("checkout submit remained on billing form; not starting OTP poll"),
            source.index('beginOtpCodeFetch(profile, "checkoutweb-submit")'),
        )

    def test_checkoutweb_uses_debugger_input_without_refill_retry_loop(self):
        manifest = (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
        content = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertIn('"debugger"', manifest)
        self.assertIn("PAYPAL_AUTOFILL_DEBUGGER_CHECKOUTWEB", background)
        self.assertIn('chrome.debugger.sendCommand(target, "Input.insertText"', background)
        self.assertIn("fillCheckoutWebWithDebugger", content)
        self.assertNotIn("retryCheckoutWebSubmit", content)
        self.assertNotIn("return await runCheckoutWebUserscriptFlow({ force: true })", content)
        self.assertNotIn("checkout submit still on billing form, retry:", content)
        self.assertNotIn("retrying fill", content)
        self.assertIn("stopped without refill retry", content)

    def test_checkoutweb_uses_native_validity_and_format_candidates_before_submit(self):
        background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
        content = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertIn("function checkoutPhoneCandidates", content)
        self.assertIn("function checkoutExpiryCandidates", content)
        self.assertIn("phoneCandidates", content)
        self.assertIn("cardExpiryCandidates", content)
        self.assertIn("uniqueTextCandidates", background)
        self.assertIn("current.valid !== false", background)
        self.assertIn("validation: current?.validation", background)
        self.assertIn("typeof el.checkValidity === \"function\" && !el.checkValidity()", content)
        self.assertIn("typeof el.checkValidity === \"function\" && !el.checkValidity()", background)

    def test_checkoutweb_does_not_refresh_region_or_refill_during_otp(self):
        source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
        self.assertNotIn("location.replace(url.href)", source)
        self.assertNotIn('PAYPAL_AUTOFILL_RUN_CHECKOUTWEB", force: true', source[: source.index("document.documentElement.setAttribute")])
        self.assertIn("if (hasOtpInputs())", source)
        self.assertIn("PayPal OTP is visible; skipped checkout autofill", source)
        self.assertIn("if (isPayPalCheckoutWeb() && !hasOtpInputs() && checkoutWebBillingFormStillVisible() && !fillAttempted)", source)


if __name__ == "__main__":
    unittest.main()

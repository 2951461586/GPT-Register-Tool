import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "protocol-payment" / "momo"))

import momo_qr_extract as momo  # noqa: E402


_PNG_DATA_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCA',"
_MOMO_GATEWAY = "https://payment.momo.vn/v2/gateway/pay?t=1&s=abcdef123456"
_VIETQR = "00020101021238570010A00000072701270006970436"  # >=24 chars, starts 000201


class IsScannableMomoArtifactTests(unittest.TestCase):
    def test_momo_gateway_url_is_scannable(self):
        self.assertTrue(momo.is_scannable_momo_artifact(_MOMO_GATEWAY))

    def test_data_image_is_scannable(self):
        self.assertTrue(momo.is_scannable_momo_artifact(_PNG_DATA_URI))

    def test_vietqr_payload_is_scannable(self):
        self.assertTrue(momo.is_scannable_momo_artifact(_VIETQR))

    def test_stripe_hosted_checkout_is_not_scannable(self):
        self.assertFalse(momo.is_scannable_momo_artifact("https://checkout.stripe.com/c/pay/cs_test#frag"))

    def test_pay_openai_middle_page_is_not_scannable(self):
        self.assertFalse(momo.is_scannable_momo_artifact("https://pay.openai.com/c/pay/cs_test"))

    def test_payment_method_brand_icon_is_not_scannable(self):
        self.assertFalse(
            momo.is_scannable_momo_artifact("https://js.stripe.com/v3/fingerprinted/img/payment-methods/icon-pm-momo.png")
        )

    def test_bare_token_is_not_scannable(self):
        self.assertFalse(momo.is_scannable_momo_artifact("momo"))

    def test_empty_is_not_scannable(self):
        self.assertFalse(momo.is_scannable_momo_artifact(""))


class ExtractMomoQrPayloadTests(unittest.TestCase):
    def test_non_dict_returns_empty_shape(self):
        out = momo.extract_momo_qr_payload("not a dict")
        self.assertEqual(out["qr_data"], "")
        self.assertEqual(out["qr_image_url"], "")
        self.assertEqual(out["hosted_instructions_url"], "")

    def test_redirect_to_url_captures_momo_gateway(self):
        payload = {
            "next_action": {
                "type": "redirect_to_url",
                "redirect_to_url": {"url": _MOMO_GATEWAY},
            }
        }
        out = momo.extract_momo_qr_payload(payload)
        self.assertEqual(out["next_action_type"], "redirect_to_url")
        self.assertEqual(out["hosted_instructions_url"], _MOMO_GATEWAY)
        self.assertEqual(out["qr_data"], _MOMO_GATEWAY)

    def test_display_qr_code_png_data_uri_is_captured(self):
        payload = {
            "next_action": {
                "type": "display_qr_code",
                "display_qr_code": {"image_url_png": _PNG_DATA_URI},
            }
        }
        out = momo.extract_momo_qr_payload(payload)
        self.assertEqual(out["qr_image_url"], _PNG_DATA_URI)
        self.assertEqual(out["qr_png_url"], _PNG_DATA_URI)

    def test_junk_only_payload_yields_no_artifacts(self):
        payload = {"next_action": {"type": "momo", "data": "momo", "value": "card"}}
        out = momo.extract_momo_qr_payload(payload)
        self.assertEqual(out["qr_data"], "")
        self.assertEqual(out["qr_image_url"], "")
        self.assertEqual(out["hosted_instructions_url"], "")

    def test_brand_icon_url_is_rejected(self):
        payload = {
            "next_action": {
                "type": "display_qr_code",
                "display_qr_code": {
                    "image_url": "https://js.stripe.com/v3/fingerprinted/img/payment-methods/icon-pm-momo.svg"
                },
            }
        }
        out = momo.extract_momo_qr_payload(payload)
        self.assertEqual(out["qr_image_url"], "")

    def test_setup_intent_redirect_is_followed(self):
        payload = {
            "setup_intent": {
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": _MOMO_GATEWAY},
                }
            }
        }
        out = momo.extract_momo_qr_payload(payload)
        self.assertEqual(out["hosted_instructions_url"], _MOMO_GATEWAY)
        self.assertEqual(out["qr_data"], _MOMO_GATEWAY)


class ProbeAccountTests(unittest.TestCase):
    _READY_INIT = {
        "total_summary": {"due": 0},
        "payment_method_types": ["momo", "card"],
        "mode": "subscription",
        "currency": "vnd",
        "subscription_data": {"trial_period_days": 30},
    }
    _CHECKOUT_DATA = {
        "one_click_trial_eligible": True,
        "is_new_stripe_customer": True,
        "processor_entity": "openai_llc",
    }
    _QR_INFO = {
        "qr_status": "ok",
        "qr_error": "",
        "has_qr": True,
        "qr_data": _MOMO_GATEWAY,
        "qr_image_url": "",
        "qr_png_url": "",
        "hosted_instructions_url": _MOMO_GATEWAY,
        "next_action_type": "setup_intent.redirect_to_url",
        "qr_expires_at": None,
        "pm_status": "ok",
        "confirm_status": "ok",
        "approve_status": "approved",
        "middle_checkout_url": "",
        "redirect_url": "",
    }

    def test_parse_only_short_circuits_without_network(self):
        with patch.object(momo, "load_credential_text", return_value=("tok", "", {"credential_valid": True, "decision": "credential_ready"})):
            with patch.object(momo, "create_checkout") as create:
                result, idx = momo.probe_account(
                    "A", None, "tok", [""], 0, parse_only=True
                )
        create.assert_not_called()
        self.assertTrue(result["conclusive"])
        self.assertIsNone(result["supported"])

    def test_invalid_credential_short_circuits_without_network(self):
        with patch.object(momo, "load_credential_text", return_value=("", "", {"credential_valid": False, "decision": "credential_parse_failed"})):
            with patch.object(momo, "create_checkout") as create:
                result, idx = momo.probe_account("A", None, "garbage", [""], 0)
        create.assert_not_called()
        self.assertFalse(result["supported"])
        self.assertTrue(result["conclusive"])

    def test_ready_with_qr_happy_path(self):
        with patch.object(momo, "load_credential_text", return_value=("tok", "", {"credential_valid": True, "decision": "credential_ready"})):
            with patch.object(momo, "create_checkout", return_value=(dict(self._CHECKOUT_DATA), "cs_test123456", "pk_live_x", "", 1, 0)):
                with patch.object(momo, "stripe_init", return_value=(dict(self._READY_INIT), "ok", 1)):
                    with patch.object(momo, "emit_momo_qr", return_value=dict(self._QR_INFO)) as emit:
                        result, idx = momo.probe_account("A", None, "tok", [""], 0, emit_qr=True)
        emit.assert_called_once()
        self.assertEqual(result["decision"], "ready_with_qr")
        self.assertTrue(result["has_qr"])
        self.assertTrue(result["supported"])
        self.assertEqual(result["amount_due"], 0)

    def test_nonzero_momo_is_blocked_before_emitting_qr(self):
        nonzero_init = dict(self._READY_INIT, total_summary={"due": 128000})
        with patch.object(momo, "load_credential_text", return_value=("tok", "", {"credential_valid": True, "decision": "credential_ready"})):
            with patch.object(momo, "create_checkout", return_value=(dict(self._CHECKOUT_DATA), "cs_test123456", "pk_live_x", "", 1, 0)):
                with patch.object(momo, "stripe_init", return_value=(nonzero_init, "ok", 1)):
                    with patch.object(momo, "apply_promo_update", return_value=(False, "update_rejected")):
                        with patch.object(momo, "emit_momo_qr") as emit:
                            result, idx = momo.probe_account("A", None, "tok", [""], 0, emit_qr=True, max_attempts=1)
        emit.assert_not_called()
        self.assertEqual(result["decision"], "promo_nonzero")
        self.assertFalse(result["supported"])

    def test_stage_specific_proxies_are_preserved(self):
        nonzero = dict(self._READY_INIT, total_summary={"due": 128000})
        stages = {
            "checkout": "http://checkout",
            "promotion": "http://promotion",
            "provider": "http://provider",
            "approve": "http://approve",
            "redirect": "http://redirect",
        }
        with patch.object(momo, "load_credential_text", return_value=("tok", "", {"credential_valid": True, "decision": "credential_ready"})), \
             patch.object(momo, "create_checkout", return_value=(dict(self._CHECKOUT_DATA), "cs_test123456", "pk_live_x", "http://checkout", 1, 0)) as create, \
             patch.object(momo, "stripe_init", side_effect=[(nonzero, "ok", 1), (dict(self._READY_INIT), "ok", 1)]) as stripe, \
             patch.object(momo, "apply_promo_update", return_value=(True, "ok")) as promo, \
             patch.object(momo, "emit_momo_qr", return_value=dict(self._QR_INFO)) as emit:
            result, _ = momo.probe_account(
                "A", None, "tok", ["http://fallback"], 0,
                emit_qr=True, max_attempts=1, stage_proxies=stages,
            )
        self.assertEqual(create.call_args.args[2], ["http://checkout"])
        self.assertEqual(stripe.call_args_list[0].args[2], "http://provider")
        self.assertEqual(promo.call_args.args[4], "http://promotion")
        self.assertEqual(emit.call_args.kwargs["approve_proxy"], "http://approve")
        self.assertEqual(emit.call_args.kwargs["redirect_proxy"], "http://redirect")
        self.assertEqual(result["decision"], "ready_with_qr")


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "protocol-payment" / "kakao"))

import kakao_extract as kakao  # noqa: E402


class KakaoContractTests(unittest.TestCase):
    def test_success_requires_structured_redirect_contract(self):
        result = kakao.kakao_result_contract(
            ok=True,
            attempts=1,
            result={"provider_redirect_url": "https://web.nicepay.co.kr/pay/123"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "ready")
        self.assertTrue(result["has_kakao"])
        self.assertEqual(result["amount_due"], 0)

    def test_missing_kakao_is_conclusive_offer_failure(self):
        result = kakao.kakao_result_contract(
            ok=False,
            attempts=1,
            error="checkout_not_kakao_trial: stage=refresh amount=0 currency=krw methods=['card']",
        )
        self.assertEqual(result["decision"], "kakao_not_enabled")
        self.assertFalse(result["has_kakao"])
        self.assertEqual(result["stage"], "stripe_init")

    def test_nonzero_offer_is_not_proxy_failure(self):
        result = kakao.kakao_result_contract(
            ok=False,
            attempts=1,
            error="checkout_not_kakao_trial: stage=refresh amount=29000 currency=krw methods=['kakao_pay']",
        )
        self.assertEqual(result["decision"], "nonzero_offer")

    def test_401_is_credential_failure(self):
        result = kakao.kakao_result_contract(ok=False, attempts=1, error="wham/usage failed 401")
        self.assertEqual(result["decision"], "credential_invalid")


if __name__ == "__main__":
    unittest.main()

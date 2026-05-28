import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "gopay-flow"))

from gopay import GoPayCharger, GoPayFraudDeny, _check_gojek_balance_rp, _claim_configured_gopay_envelope, _expected_amount_from_init, _extract_envelope_ids, _extract_gopay_balance_rp, _gojek_call, prepare_smsbower_otp, smsbower_source_enabled, wait_smsbower_otp  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Ext:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


class _SequenceExt:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "timeout": timeout})
        return self.responses.pop(0)


class _FlakyJsonExt:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Failed to perform, curl: (35) TLS connect error")
        return _Resp(data={"success": True})


class _SmsBowerResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _BalanceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.refreshes = 0

    def get_balance(self):
        return self.responses.pop(0)

    def refresh_token(self):
        self.refreshes += 1
        return {"status": 200, "body": {"ok": True}}


class _EnvelopeClient:
    def __init__(self):
        self.claimed = []

    def envelope_claim(self, envelope_id):
        self.claimed.append(envelope_id)
        return {"status": 200, "body": {"success": True}}


class GoPayProtocolFlowTests(unittest.TestCase):
    def test_sms_channel_forces_resend_otp_after_user_consent(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.otp_channel = "sms"
        calls = []

        charger._midtrans_load_transaction = lambda snap: calls.append(("load", snap))
        charger._midtrans_init_linking = lambda snap: calls.append(("link", snap)) or "ref123"
        charger._gopay_validate_reference = lambda ref: calls.append(("validate_ref", ref))
        charger._gopay_user_consent = lambda ref: calls.append(("consent", ref))
        charger._gopay_resend_otp = lambda ref: calls.append(("resend", ref))

        state = charger.start_linking_until_otp("snap123", "cs123", "pk123")

        self.assertEqual(state["reference_id"], "ref123")
        self.assertIn(("resend", "ref123"), calls)

    def test_wa_channel_does_not_force_sms_resend(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.otp_channel = "wa"
        charger.log = lambda msg: None
        calls = []

        charger._midtrans_load_transaction = lambda snap: None
        charger._midtrans_init_linking = lambda snap: "ref123"
        charger._gopay_validate_reference = lambda ref: None
        charger._gopay_user_consent = lambda ref: None
        charger._gopay_resend_otp = lambda ref: calls.append(("resend", ref))

        charger.start_linking_until_otp("snap123")

        self.assertEqual(calls, [])

    def test_extract_challenge_details_from_nested_response(self):
        body = {
            "success": True,
            "data": {
                "challenge": {
                    "action": {
                        "value": {
                            "challenge_id": "challenge123",
                            "client_id": "client123",
                        }
                    }
                }
            },
        }

        self.assertEqual(
            GoPayCharger._extract_challenge_details(body),
            ("challenge123", "client123"),
        )

    def test_stripe_init_retries_without_unknown_parameter(self):
        first = _Resp(
            status_code=400,
            data={
                "error": {
                    "code": "parameter_unknown",
                    "param": "elements_session_client[locale]",
                    "message": "Received unknown parameter",
                }
            },
            text='{"error":{"code":"parameter_unknown"}}',
        )
        second = _Resp(
            status_code=200,
            data={
                "payment_method_types": ["gopay"],
                "currency": "idr",
                "init_checksum": "checksum123",
            },
        )
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _SequenceExt([first, second])
        charger.log = lambda msg: None

        data = charger._stripe_init("cs_test", "pk_test")

        self.assertEqual(data["init_checksum"], "checksum123")
        self.assertIn("elements_session_client[locale]", charger.ext.calls[0]["data"])
        self.assertNotIn("elements_session_client[locale]", charger.ext.calls[1]["data"])

    def test_expected_amount_uses_invoice_amount_due_when_not_zero(self):
        self.assertEqual(_expected_amount_from_init({
            "total_summary": {"due": 319000},
            "invoice": {"amount_due": 319000},
        }), "319000")

    def test_stripe_confirm_reinitializes_after_amount_mismatch(self):
        mismatch = _Resp(
            status_code=400,
            data={"error": {"code": "checkout_amount_mismatch", "param": "expected_amount"}},
            text='{"error":{"code":"checkout_amount_mismatch"}}',
        )
        ok = _Resp(status_code=200, data={"payment_status": "open", "setup_intent": {"status": "requires_action"}})
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _SequenceExt([mismatch, ok])
        charger.runtime = {}
        charger.log = lambda msg: None
        init_payloads = [
            {"init_checksum": "old", "payment_method_types": ["gopay"], "currency": "idr", "invoice": {"amount_due": 0}},
            {"init_checksum": "new", "payment_method_types": ["gopay"], "currency": "idr", "invoice": {"amount_due": 319000}},
        ]
        charger._stripe_init = lambda cs, pk: init_payloads.pop(0)

        data = charger._stripe_confirm("cs_test", "pm_test", "pk_test")

        self.assertEqual(data["payment_status"], "open")
        self.assertEqual(charger.ext.calls[0]["data"]["expected_amount"], "0")
        self.assertEqual(charger.ext.calls[1]["data"]["expected_amount"], "319000")
        self.assertEqual(charger.ext.calls[1]["data"]["init_checksum"], "new")

    def test_midtrans_charge_fraud_deny_is_terminal(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _Ext(_Resp(data={"fraud_status": "deny", "transaction_status": "deny"}))
        charger._midtrans_headers = lambda *args, **kwargs: {}

        with self.assertRaises(GoPayFraudDeny):
            charger._midtrans_create_charge("snap123")

    def test_prepare_smsbower_sets_local_indonesia_phone(self):
        calls = []

        def fake_get(endpoint, params, timeout):
            calls.append((endpoint, params, timeout))
            return _SmsBowerResp("ACCESS_NUMBER:act1:6281234567890")

        import gopay

        old_get = gopay.requests.get
        try:
            gopay.requests.get = fake_get
            activation = prepare_smsbower_otp({
                "country_code": "62",
                "otp": {
                    "source": "smsbower",
                    "smsbower": {
                        "api_key": "key",
                        "service": "gp",
                        "country": "6",
                        "register_account": False,
                    },
                },
            }, log=lambda msg: None)
        finally:
            gopay.requests.get = old_get

        self.assertEqual(activation["activation_id"], "act1")
        self.assertEqual(activation["phone"], "+6281234567890")
        self.assertEqual(activation["phone_number"], "81234567890")
        self.assertEqual(calls[0][1]["service"], "gp")
        self.assertEqual(calls[0][1]["country"], "6")

    def test_smsbower_source_accepts_top_level_otp_source(self):
        self.assertTrue(smsbower_source_enabled({"otp_source": "smsbower"}))

    def test_wait_smsbower_otp_reads_status_ok(self):
        responses = iter([
            _SmsBowerResp("STATUS_WAIT_CODE"),
            _SmsBowerResp("STATUS_OK:123456"),
        ])

        def fake_get(endpoint, params, timeout):
            return next(responses)

        import gopay

        old_get = gopay.requests.get
        try:
            gopay.requests.get = fake_get
            code = wait_smsbower_otp({
                "smsbower": {
                    "activation_id": "act1",
                    "api_key": "key",
                    "endpoint": "https://smsbower.example/api",
                    "timeout": 2,
                    "poll_interval": 1,
                }
            }, log=lambda msg: None)
        finally:
            gopay.requests.get = old_get

        self.assertEqual(code, "123456")

    def test_wait_smsbower_otp_ignores_wait_retry_stale_code(self):
        responses = iter([
            _SmsBowerResp("STATUS_WAIT_RETRY:1111"),
            _SmsBowerResp("STATUS_OK:2222"),
        ])

        def fake_get(endpoint, params, timeout):
            return next(responses)

        import gopay

        old_get = gopay.requests.get
        try:
            gopay.requests.get = fake_get
            code = wait_smsbower_otp({
                "smsbower": {
                    "activation_id": "act1",
                    "api_key": "key",
                    "endpoint": "https://smsbower.example/api",
                    "timeout": 2,
                    "poll_interval": 1,
                }
            }, log=lambda msg: None)
        finally:
            gopay.requests.get = old_get

        self.assertEqual(code, "2222")

    def test_gopay_validate_reference_retries_transient_tls_error(self):
        import gopay

        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _FlakyJsonExt()
        charger.log = lambda msg: None

        old_sleep = gopay.time.sleep
        try:
            gopay.time.sleep = lambda seconds: None
            charger._gopay_validate_reference("ref123")
        finally:
            gopay.time.sleep = old_sleep

        self.assertEqual(charger.ext.calls, 2)

    def test_gojek_call_retries_5xx(self):
        import gopay

        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                return {"status": 500, "body": {"error": "temporary"}}
            return {"status": 200, "body": {"ok": True}}

        old_sleep = gopay.time.sleep
        try:
            gopay.time.sleep = lambda seconds: None
            result = _gojek_call(fn, log=lambda msg: None)
        finally:
            gopay.time.sleep = old_sleep

        self.assertEqual(result["status"], 200)
        self.assertEqual(len(calls), 2)

    def test_extract_gopay_balance_from_source_shape(self):
        balance = _extract_gopay_balance_rp({
            "status": 200,
            "body": {
                "data": [
                    {"balance": {"value": 349000}},
                ],
            },
        })

        self.assertEqual(balance, 349000)

    def test_balance_check_refreshes_once_after_failed_read(self):
        client = _BalanceClient([
            {"status": 401, "body": {"error": "expired"}},
            {"status": 200, "body": {"data": [{"balance": {"value": 12000}}]}},
        ])

        balance = _check_gojek_balance_rp(client, log=lambda msg: None)

        self.assertEqual(balance, 12000)
        self.assertEqual(client.refreshes, 1)

    def test_extract_envelope_ids_from_short_link_html(self):
        envelope_id, link_id = _extract_envelope_ids(
            'window.__data={"envelope_request_id":"6a181e98a3335241d7dae063","link_id":"7b181e98a3335241d7dae064"}'
        )

        self.assertEqual(envelope_id, "6a181e98a3335241d7dae063")
        self.assertEqual(link_id, "7b181e98a3335241d7dae064")

    def test_claim_configured_gopay_envelope_uses_deeplink_id(self):
        client = _EnvelopeClient()

        claimed = _claim_configured_gopay_envelope(
            client,
            {"envelope_deeplink_id": "6a181e98a3335241d7dae063"},
            log=lambda msg: None,
        )

        self.assertTrue(claimed)
        self.assertEqual(client.claimed, ["6a181e98a3335241d7dae063"])


if __name__ == "__main__":
    unittest.main()

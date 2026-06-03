import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "gopay-flow"))

from gopay import GoPayCharger, GoPayFraudDeny, _bootstrap_gojek_account, _check_gojek_balance_rp, _expected_amount_from_init, _extract_gopay_balance_rp, _gojek_call, _gopay_app_cfg, _gopay_app_service_configured, _retry_smsbower_gopay_bootstrap_error, _rpc_bool, _rpc_state, _smsbower_api, _wait_for_gojek_min_balance, prepare_smsbower_otp, smsbower_source_enabled, wait_smsbower_otp  # noqa: E402
import gopay_pure_protocol  # noqa: E402


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

    def post(self, url, data=None, timeout=None, headers=None):
        captured = data if isinstance(data, str) else dict(data or {})
        self.calls.append({"url": url, "data": captured, "timeout": timeout, "headers": headers or {}})
        return self.responses.pop(0)

    def get(self, url, timeout=None, headers=None, params=None):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers or {}, "params": params or {}})
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


class _PureProtocolFake:
    instances = []

    def __init__(self, device, signer=None, debug=True, dry_run=False, proxy=None, timeout=35, **kwargs):
        self.device = device
        self.signer = signer
        self.debug = debug
        self.dry_run = dry_run
        self.proxy = proxy
        self.timeout = timeout
        self.closed = False
        self.calls = []
        self.balance_reads = 0
        self.__class__.instances.append(self)

    def login_methods(self, local, country_code):
        self.calls.append(("login_methods", local, country_code))
        return 404, {"errors": [{"code": "auth:error:user:not_found"}]}, {}

    def cvs_methods(self, local, flow="signup", country_code="+62"):
        self.calls.append(("cvs_methods", local, flow, country_code))
        return 200, {"verification_id": "v_signup", "default_method": "otp_sms", "methods": ["otp_sms"]}, {}

    def cvs_initiate(self, local, verification_id, method="otp_sms", flow="signup", country_code="+62"):
        self.calls.append(("cvs_initiate", local, verification_id, method, flow, country_code))
        return 200, {"otp_token": "otp_signup"}, {}

    def cvs_retry(self, otp_token, method="otp_sms", flow="signup"):
        self.calls.append(("cvs_retry", otp_token, method, flow))
        return 200, {"otp_token": otp_token}, {}

    def cvs_verify(self, local, verification_id, otp, method="otp_sms", flow="signup", country_code="+62", otp_token=None, **kwargs):
        self.calls.append(("cvs_verify", local, verification_id, otp, method, flow, country_code, otp_token))
        return 200, {"verification_token": "verify_signup"}, {}

    def customer_signup(self, local, full_name, **kwargs):
        self.calls.append(("customer_signup", local, full_name, kwargs.get("signup_client_name")))
        return 201, {"access_token": "at_signup", "refresh_token": "rt_signup"}, {}

    def token(self, **kwargs):
        self.calls.append(("token", kwargs))
        return 200, {"access_token": "at_refreshed", "refresh_token": "rt_refreshed"}, {}

    def pin_allowed(self, access_token, pin):
        self.calls.append(("pin_allowed", access_token, pin))
        return 200, {"success": True}, {}

    def cvs_methods_pin(self, access_token):
        self.calls.append(("cvs_methods_pin", access_token))
        return 200, {"verification_id": "v_pin", "default_method": "otp_sms", "methods": ["otp_sms"]}, {}

    def cvs_initiate_pin(self, access_token, verification_id, method="otp_sms"):
        self.calls.append(("cvs_initiate_pin", access_token, verification_id, method))
        return 200, {"otp_token": "otp_pin"}, {}

    def cvs_retry_pin(self, access_token, otp_token, method="otp_sms"):
        self.calls.append(("cvs_retry_pin", access_token, otp_token, method))
        return 200, {"otp_token": otp_token}, {}

    def cvs_verify_pin(self, access_token, verification_id, otp, otp_token, method="otp_sms"):
        self.calls.append(("cvs_verify_pin", access_token, verification_id, otp, otp_token, method))
        return 200, {"verification_token": "verify_pin"}, {}

    def pin_setup_token_after_otp(self, access_token, pin, verification_token):
        self.calls.append(("pin_setup_token_after_otp", access_token, pin, verification_token))
        return 200, {"success": True}, {}

    def user_profile(self, access_token):
        self.calls.append(("user_profile", access_token))
        return 200, {"data": {"is_pin_setup": True}}, {}

    def get(self, base, path, auth=None):
        self.calls.append(("get", base, path, auth))
        if path.startswith("/v1/festivals/envelope-requests/"):
            return 200, {"data": {"envelope_request_id": "env_req_1"}}, {}
        self.balance_reads += 1
        if self.balance_reads == 1:
            return 200, {"data": [{"balance": {"value": 0}}]}, {}
        return 200, {"data": [{"balance": {"value": 1}}]}, {}

    def post(self, base, path, body, auth=None, **kwargs):
        self.calls.append(("post", base, path, body, auth))
        if path == "/v1/festivals/envelope-requests":
            return 200, {"success": True, "data": {"envelope_request_id": body.get("envelope_request_id")}}, {}
        return 200, {"success": True}, {}

    def close(self):
        self.closed = True


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

    def test_midtrans_snap_headers_are_signed_when_key_configured(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.snap_signing_key = "secret"
        charger._snap_signature_warned = False
        charger.midtrans_client_id = "mid-client"
        charger.log = lambda msg: None

        headers = charger._midtrans_headers(
            "snap123",
            json_body=True,
            snap_path="/snap/v3/accounts/snap123/linking",
            snap_body={"type": "gopay"},
        )

        self.assertIn("X-Snap-Signature", headers)
        self.assertIn("X-Timestamp", headers)

    def test_midtrans_charge_uses_compact_signed_json_body(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _SequenceExt([_Resp(data={"transaction_status": "settlement"})])
        charger.snap_signing_key = "secret"
        charger._snap_signature_warned = False
        charger.log = lambda msg: None
        charger._midtrans_redirection_url = lambda snap: f"https://app.midtrans.com/snap/v4/redirection/{snap}"

        charge_ref = charger._midtrans_create_charge("snap123")

        self.assertEqual(charge_ref, "")
        self.assertEqual(
            charger.ext.calls[0]["data"],
            '{"payment_type":"gopay","tokenization":"true","promo_details":null}',
        )
        self.assertIn("X-Snap-Signature", charger.ext.calls[0]["headers"])

    def test_midtrans_status_poll_uses_snap_signature(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = _SequenceExt([_Resp(data={"transaction_status": "settlement", "status_code": "200"})])
        charger.snap_signing_key = "secret"
        charger._snap_signature_warned = False
        charger.log = lambda msg: None
        charger._midtrans_redirection_url = lambda snap: f"https://app.midtrans.com/snap/v4/redirection/{snap}"

        result = charger._midtrans_poll_status("snap123")

        self.assertEqual(result["transaction_status"], "settlement")
        self.assertIn("X-Snap-Signature", charger.ext.calls[0]["headers"])

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

    def test_smsbower_api_retries_transient_timeout(self):
        import gopay

        calls = []

        def fake_get(endpoint, params, timeout):
            calls.append((endpoint, params, timeout))
            if len(calls) == 1:
                raise gopay.requests.exceptions.ReadTimeout("read timeout")
            return _SmsBowerResp("ACCESS_NUMBER:act1:6281234567890")

        old_get = gopay.requests.get
        old_sleep = gopay.time.sleep
        try:
            gopay.requests.get = fake_get
            gopay.time.sleep = lambda seconds: None
            result = _smsbower_api("key", "https://smsbower.example/api", "getNumberV2", {"service": "gp"})
        finally:
            gopay.requests.get = old_get
            gopay.time.sleep = old_sleep

        self.assertEqual(result, "ACCESS_NUMBER:act1:6281234567890")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], 20)

    def test_smsbower_bootstrap_retry_accepts_gopay_init_rate_limit(self):
        err = GoPayFraudDeny(
            'signup otp initiate rate limited: status 429 {"errors":[{"code":"scp-cvs:error:ratelimit:init_verification"}]}'
        )
        self.assertTrue(_retry_smsbower_gopay_bootstrap_error(err))

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

    def test_wait_for_gojek_min_balance_polls_until_ready(self):
        import gopay

        client = _BalanceClient([
            {"status": 200, "body": {"data": [{"balance": {"value": 0}}]}},
            {"status": 200, "body": {"data": [{"balance": {"value": 0}}]}},
            {"status": 200, "body": {"data": [{"balance": {"value": 1}}]}},
        ])

        sleeps = []
        old_sleep = gopay.time.sleep
        try:
            gopay.time.sleep = lambda seconds: sleeps.append(seconds)
            balance = _wait_for_gojek_min_balance(
                client,
                min_balance_rp=1,
                timeout_seconds=120,
                poll_interval_seconds=5,
                log=lambda msg: None,
            )
        finally:
            gopay.time.sleep = old_sleep

        self.assertEqual(balance, 1)
        self.assertEqual(len(sleeps), 2)

    def test_smsbower_bootstrap_uses_python_pure_protocol_not_app_or_legacy_client(self):
        import gopay

        _PureProtocolFake.instances = []
        activation = {
            "phone": "+6281234567890",
            "country_code": "62",
            "activation_id": "act1",
            "api_key": "key",
            "poll_interval": 1,
        }
        cfg = {
            "pin": "147258",
            "proxy": "socks5h://127.0.0.1:7897",
            "pure_protocol_timeout_seconds": 9,
            "otp": {
                "smsbower": {
                    "min_balance_rp": 1,
                    "balance_wait_timeout_seconds": 1,
                    "balance_poll_interval_seconds": 1,
                }
            },
        }

        with patch.object(gopay_pure_protocol, "GoPayProtocol", _PureProtocolFake):
            with patch.object(gopay, "_wait_smsbower_otp_with_retry", side_effect=["111111", "222222"]):
                with patch.object(gopay, "_smsbower_set_status", return_value="ACCESS_RETRY_GET"):
                    with patch.object(gopay, "_call_gopay_app", side_effect=AssertionError("app service must not be used")):
                        with patch.object(gopay, "_load_gojek_client", side_effect=AssertionError("legacy client must not be used")):
                            _bootstrap_gojek_account(cfg, activation, log=lambda msg: None)

        self.assertTrue(activation["gojek_registered"])
        self.assertEqual(activation["balance_rp"], 1)
        self.assertEqual(len(_PureProtocolFake.instances), 1)
        fake = _PureProtocolFake.instances[0]
        self.assertEqual(fake.proxy, "socks5://127.0.0.1:7897")
        self.assertEqual(fake.timeout, 9)
        self.assertTrue(fake.closed)
        self.assertIn("customer_signup", [call[0] for call in fake.calls])
        self.assertIn("pin_setup_token_after_otp", [call[0] for call in fake.calls])
        self.assertEqual(activation["funded_via"], "welcome")

    def test_smsbower_bootstrap_claims_envelope_after_welcome_timeout(self):
        import gopay

        class _EnvelopeFake(_PureProtocolFake):
            def get(self, base, path, auth=None):
                self.calls.append(("get", base, path, auth))
                if path.startswith("/v1/festivals/envelope-requests/"):
                    return 200, {"data": {"envelope_request_id": "env_req_1"}}, {}
                self.balance_reads += 1
                value = 0 if self.balance_reads <= 2 else 1
                return 200, {"data": [{"balance": {"value": value}}]}, {}

        _EnvelopeFake.instances = []
        activation = {
            "phone": "+6281234567890",
            "country_code": "62",
            "activation_id": "act1",
            "api_key": "key",
            "poll_interval": 1,
        }
        cfg = {
            "pin": "147258",
            "otp": {
                "smsbower": {
                    "min_balance_rp": 1,
                    "welcome_wait_seconds": 0,
                    "fund_wait_timeout_seconds": 5,
                    "balance_poll_interval_seconds": 1,
                    "envelope_links": ["https://app.gopay.co.id/NF8p/abc123"],
                }
            },
        }

        with patch.object(gopay_pure_protocol, "GoPayProtocol", _EnvelopeFake):
            with patch.object(gopay, "_wait_smsbower_otp_with_retry", side_effect=["111111", "222222"]):
                with patch.object(gopay, "_smsbower_set_status", return_value="ACCESS_RETRY_GET"):
                    old_sleep = gopay.time.sleep
                    try:
                        gopay.time.sleep = lambda seconds: None
                        _bootstrap_gojek_account(cfg, activation, log=lambda msg: None)
                    finally:
                        gopay.time.sleep = old_sleep

        self.assertTrue(activation["gojek_registered"])
        self.assertEqual(activation["balance_rp"], 1)
        self.assertEqual(activation["funded_via"], "envelope")
        fake = _EnvelopeFake.instances[0]
        self.assertIn(
            ("post", "https://customer.gopayapi.com", "/v1/festivals/envelope-requests", {"envelope_request_id": "env_req_1"}, "at_refreshed"),
            fake.calls,
        )

    def test_gopay_app_config_uses_root_addr(self):
        cfg = _gopay_app_cfg({
            "gopay_app_service_addr": "127.0.0.1:50060",
            "provider_timeout_seconds": 123,
        })

        self.assertTrue(_gopay_app_service_configured({"gopay_app_service_addr": "127.0.0.1:50060"}))
        self.assertEqual(cfg["addr"], "127.0.0.1:50060")
        self.assertEqual(cfg["service"], "gopay_app.GopayAppService")
        self.assertEqual(cfg["timeout_seconds"], 123)

    def test_gopay_app_config_falls_back_to_wa_rebind_addr(self):
        cfg = _gopay_app_cfg({
            "wa_rebind": {
                "gopay_app_service_addr": "127.0.0.1:50061",
                "gopay_app_service": "custom.Service",
            },
        })

        self.assertEqual(cfg["addr"], "127.0.0.1:50061")
        self.assertEqual(cfg["service"], "custom.Service")

    def test_rpc_helpers_accept_grpcurl_camel_case(self):
        payload = {"success": True, "otpSent": True, "stateJson": "{\"stage\":\"ok\"}"}

        self.assertTrue(_rpc_bool(payload, "otpSent", "otp_sent"))
        self.assertEqual(_rpc_state(payload), "{\"stage\":\"ok\"}")

if __name__ == "__main__":
    unittest.main()

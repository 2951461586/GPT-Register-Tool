import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sms_tool import registration
from sms_tool.phone_reuse import PhonePool, PhoneSlot, _prepare_smsbower_for_send, _wait_for_send_cooldown, complete_phone_verification_with_reuse, send_phone_otp
from sms_tool.smsbower import SmsBowerClient, normalize_country, normalize_phone, normalize_service


class SmsBowerPhoneReuseTests(unittest.TestCase):
    def test_openai_ghana_aliases(self):
        self.assertEqual(normalize_service("openai"), "dr")
        self.assertEqual(normalize_service("OpenAI (ChatGPT)"), "dr")
        self.assertEqual(normalize_country("Ghana"), "38")
        self.assertEqual(normalize_country("+233"), "38")
        self.assertEqual(normalize_phone("233555123456"), "+233555123456")

    def test_smsbower_activation_resets_on_next_round_after_third_reuse(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=3,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])
        with patch("sms_tool.phone_reuse._prepare_smsbower_for_send", return_value=True), \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}), \
             patch("sms_tool.phone_reuse._wait_smsbower_code", side_effect=["111111", "222222", "333333"]), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}), \
             patch("sms_tool.phone_reuse._complete_smsbower_activation") as complete:
            complete.side_effect = lambda item: (
                setattr(item, "phone", ""),
                setattr(item, "activation_id", ""),
                setattr(item, "reuse_count", 0),
                setattr(item, "last_sms_code", ""),
            )
            first = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)
            second = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)
            third = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)
            reset_count = pool.reset_exhausted_smsbower_slots()

        self.assertTrue(first["ok"])
        self.assertEqual(first["reuse_count"], 1)
        self.assertEqual(second["reuse_count"], 2)
        self.assertEqual(third["reuse_count"], 3)
        complete.assert_called_once_with(slot)
        self.assertEqual(reset_count, 1)
        self.assertEqual(slot.activation_id, "")
        self.assertEqual(slot.phone, "")
        self.assertEqual(slot.reuse_count, 0)
        self.assertEqual(pool.total_capacity, 3)

    def test_smsbower_wait_ignores_previous_retry_code(self):
        client = SmsBowerClient(api_key="test-key")
        with patch.object(client, "get_status", side_effect=[
            {"status": "WAIT_RETRY", "code": "111111"},
            {"status": "OK", "code": "111111"},
            {"status": "OK", "code": "222222"},
        ]):
            code = client.wait_for_code("act-1", timeout=5, poll_interval=0, previous_code="111111")

        self.assertEqual(code, "222222")

    def test_send_phone_otp_surfaces_openai_error_code(self):
        response = Mock(status_code=400, text='{"error":{"code":"fraud_guard","message":"blocked"}}')
        response.json.return_value = {"error": {"code": "fraud_guard", "message": "blocked"}}
        session = Mock()
        session.post.return_value = response

        result = send_phone_otp(session, "did", "https://auth.openai.com/add-phone", "+233555123456", sentinel={})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "fraud_guard")
        self.assertEqual(result["message"], "blocked")

    def test_phone_send_cooldown_waits_before_reusing_same_number(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            last_send_at=70,
            send_cooldown_seconds=45,
        )

        with patch("sms_tool.phone_reuse.time.time", return_value=100), \
             patch("sms_tool.phone_reuse.time.sleep") as sleep:
            _wait_for_send_cooldown(slot)

        sleep.assert_called_once_with(15)

    def test_smsbower_rate_limit_retries_without_canceling_activation(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=2,
            slot_id="smsbower:0",
            send_retry_attempts=2,
        )
        pool = PhonePool(phones=[slot])

        with patch("sms_tool.phone_reuse._prepare_smsbower_for_send", return_value=True), \
             patch("sms_tool.phone_reuse.send_phone_otp", side_effect=[
                 {"ok": False, "status_code": 429, "error_code": "rate_limit_exceeded"},
                 {"ok": True, "status_code": 200},
             ]) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="111111"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}), \
             patch("sms_tool.phone_reuse._cancel_smsbower_activation") as cancel:
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        cancel.assert_not_called()

    def test_smsbower_fraud_guard_retires_slot_for_current_batch(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=3,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        with patch("sms_tool.phone_reuse._prepare_smsbower_for_send", return_value=True), \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": False, "status_code": 400, "error_code": "fraud_guard"}), \
             patch("sms_tool.phone_reuse._cancel_smsbower_activation") as cancel:
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "phone_send_failed:fraud_guard")
        cancel.assert_called_once()
        self.assertEqual(pool.total_capacity, 0)

    def test_smsbower_prepare_keeps_activation_when_additional_code_unavailable(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            reuse_count=1,
            max_reuse_count=3,
            slot_id="smsbower:0",
        )

        with patch("sms_tool.phone_reuse._smsbower_client") as client_factory:
            old_client = Mock()
            old_client.request_additional.return_value = False
            client_factory.return_value = old_client
            self.assertFalse(_prepare_smsbower_for_send(slot))

        self.assertEqual(slot.phone, "+233555123456")
        self.assertEqual(slot.activation_id, "act-1")
        self.assertEqual(slot.reuse_count, 1)
        old_client.complete.assert_not_called()

    def test_smsbower_sms_timeout_keeps_activation_for_retry(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=3,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        with patch("sms_tool.phone_reuse._prepare_smsbower_for_send", return_value=True), \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}), \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value=None), \
             patch("sms_tool.phone_reuse._cancel_smsbower_activation") as cancel:
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "phone_sms_timeout")
        cancel.assert_not_called()
        self.assertEqual(slot.phone, "+233555123456")
        self.assertEqual(slot.activation_id, "act-1")
        self.assertEqual(slot.reuse_count, 0)

    def test_phone_pool_state_does_not_override_configured_send_retries(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            pool = PhonePool(
                phones=[PhoneSlot(phone="", provider="smsbower", slot_id="smsbower:0", send_retry_attempts=3, send_retry_delay_seconds=45)],
                state_file=state_path,
            )
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"smsbower:0","phone":"+233555123456",'
                    '"activation_id":"act-1","reuse_count":1,"send_retry_attempts":1,'
                    '"send_retry_delay_seconds":1}]}'
                )
            pool.load_state()

        self.assertEqual(pool.phones[0].send_retry_attempts, 3)
        self.assertEqual(pool.phones[0].send_retry_delay_seconds, 45)

    def test_registration_requires_phone_when_pool_is_enabled(self):
        with patch.dict(registration.CFG, {"codex_oauth": {}}, clear=False):
            self.assertFalse(registration._registration_requires_phone_verification(None))
            self.assertTrue(registration._registration_requires_phone_verification(object()))


if __name__ == "__main__":
    unittest.main()

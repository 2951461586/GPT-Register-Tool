import ast
import contextlib
import io
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sms_tool import registration
from sms_tool import phone_reuse
from sms_tool import sms_providers
from sms_tool.phone_reuse import PhonePool, PhoneSlot, _complete_smsbower_activation, _prepare_smsbower_for_send, _wait_for_send_cooldown, complete_phone_verification_with_reuse, create_phone_pool, send_phone_otp
from sms_tool.sms_provider_adapter import SmsProviderAdapter
from sms_tool.nexsms import NexSmsClient
from sms_tool.smsbower import SmsBowerActivation, SmsBowerClient, normalize_country, normalize_phone, normalize_service


class SmsBowerPhoneReuseTests(unittest.TestCase):
    def test_openai_ghana_aliases(self):
        self.assertEqual(normalize_service("openai"), "dr")
        self.assertEqual(normalize_service("OpenAI (ChatGPT)"), "dr")
        self.assertEqual(normalize_country("Ghana"), "38")
        self.assertEqual(normalize_country("+233"), "38")
        self.assertEqual(normalize_phone("233555123456"), "+233555123456")

    def test_smsbower_exact_tier_is_forwarded_to_get_number_v2(self):
        response = Mock(text="ACCESS_NUMBER:act-1:573001234567")
        response.raise_for_status.return_value = None
        client = SmsBowerClient(api_key="test-key")

        with patch("sms_tool.smsbower._requests.get", return_value=response) as request:
            activation = client.get_number(
                service="dr",
                country="33",
                min_price="0.026",
                max_price="0.026",
                provider_ids="3243,3253",
            )

        self.assertEqual(activation.activation_id, "act-1")
        self.assertEqual(activation.country, "33")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["service"], "dr")
        self.assertEqual(params["country"], "33")
        self.assertEqual(params["minPrice"], "0.026")
        self.assertEqual(params["maxPrice"], "0.026")
        self.assertEqual(params["providerIds"], "3243,3253")

    def test_phone_reuse_uses_common_sms_provider_adapter_contract(self):
        slot = PhoneSlot(phone="+233555123456", provider="smsbower")
        adapter = phone_reuse._sms_provider_adapter(slot)

        self.assertIsInstance(adapter, SmsProviderAdapter)
        self.assertEqual(adapter.provider, "smsbower")

    def test_smsbower_acquire_refreshes_provider_ids_for_selected_tier(self):
        slot = PhoneSlot(
            phone="",
            provider="smsbower",
            api_key="test-key",
            service="dr",
            country="33",
            min_price="0.026",
            max_price="0.026",
        )
        client = Mock()
        client.get_prices.return_value = [
            {"country_id": "33", "service": "dr", "provider_id": "3243", "price": "0.026", "count": 3},
            {"country_id": "33", "service": "dr", "provider_id": "3288", "price": "0.052", "count": 64},
        ]
        client.get_number.return_value = SmsBowerActivation("act-1", "+573001234567", "dr", "33", "0.026")

        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            prepared = _prepare_smsbower_for_send(slot)

        self.assertTrue(prepared)
        self.assertEqual(slot.provider_ids, "3243")
        client.get_number.assert_called_once_with(
            service="dr",
            country="33",
            min_price="0.026",
            max_price="0.026",
            provider_ids="3243",
        )

    def test_smsbower_no_numbers_retries_up_to_ten_times(self):
        slot = PhoneSlot(
            phone="",
            provider="smsbower",
            api_key="test-key",
            service="dr",
            country="33",
            min_price="0.017",
            max_price="0.017",
        )
        client = Mock()
        client.get_prices.return_value = []
        client.get_number.side_effect = RuntimeError("getNumber error: NO_NUMBERS")

        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client), \
             patch("sms_tool.phone_reuse.time.sleep") as sleep:
            prepared = _prepare_smsbower_for_send(slot)

        self.assertFalse(prepared)
        self.assertEqual(client.get_number.call_count, 10)
        self.assertEqual(sleep.call_count, 9)

    def test_smsbower_complete_falls_back_to_cancel_and_resets_slot(self):
        slot = PhoneSlot(
            phone="+573001234567",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
        )
        client = Mock()
        client.complete.return_value = False
        client.cancel.return_value = True

        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            _complete_smsbower_activation(slot)

        client.complete.assert_called_once_with("act-1")
        client.cancel.assert_called_once_with("act-1")
        self.assertEqual(slot.activation_id, "")
        self.assertEqual(slot.phone, "")

    def test_smsbower_activation_completes_immediately_after_reuse_limit(self):
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
            reset_count = pool.reset_exhausted_slots()

        self.assertTrue(first["ok"])
        self.assertEqual(first["reuse_count"], 1)
        self.assertEqual(second["reuse_count"], 2)
        self.assertEqual(third["reuse_count"], 3)
        complete.assert_called_once_with(slot)
        self.assertEqual(reset_count, 0)
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

    def test_smsbower_fraud_guard_switches_number_and_retries_same_flow(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=2,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        def acquire_new_number(item):
            item.phone = "+234555000111"
            item.activation_id = "act-2"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client), \
             patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number), \
             patch("sms_tool.phone_reuse.send_phone_otp", side_effect=[
                 {"ok": False, "status_code": 400, "error_code": "fraud_guard"},
                 {"ok": True, "status_code": 200},
             ]) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="111111"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[3], "+233555123456")
        self.assertEqual(send.call_args_list[1].args[3], "+234555000111")
        client.cancel.assert_called_once_with("act-1")
        client.complete.assert_called_once_with("act-2")
        self.assertEqual(result["phone"], "+234555000111")

    def test_smsbower_fraud_guard_retries_even_when_number_attempts_is_one(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=1,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        def acquire_new_number(item):
            item.phone = "+234555000111"
            item.activation_id = "act-2"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client), \
             patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number), \
             patch("sms_tool.phone_reuse.send_phone_otp", side_effect=[
                 {"ok": False, "status_code": 400, "error_code": "fraud_guard"},
                 {"ok": True, "status_code": 200},
             ]) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="111111"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[3], "+233555123456")
        self.assertEqual(send.call_args_list[1].args[3], "+234555000111")

    def test_smsbower_fraud_guard_keeps_switching_numbers_until_success(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=1,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])
        acquired = iter([
            ("+234555000111", "act-2"),
            ("+235555000222", "act-3"),
            ("+236555000333", "act-4"),
        ])

        def acquire_new_number(item):
            phone, activation_id = next(acquired)
            item.phone = phone
            item.activation_id = activation_id
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client), \
             patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number), \
             patch("sms_tool.phone_reuse.send_phone_otp", side_effect=[
                 {"ok": False, "status_code": 400, "error_code": "fraud_guard"},
                 {"ok": False, "status_code": 400, "error_code": "fraud_guard"},
                 {"ok": False, "status_code": 400, "error_code": "fraud_guard"},
                 {"ok": True, "status_code": 200},
             ]) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="111111"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 4)
        self.assertEqual(send.call_args_list[0].args[3], "+233555123456")
        self.assertEqual(send.call_args_list[1].args[3], "+234555000111")
        self.assertEqual(send.call_args_list[2].args[3], "+235555000222")
        self.assertEqual(send.call_args_list[3].args[3], "+236555000333")
        self.assertEqual(client.cancel.call_count, 3)
        client.complete.assert_called_once_with("act-4")

    def test_smsbower_prepare_switches_number_when_additional_code_unavailable(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            reuse_count=1,
            max_reuse_count=3,
            slot_id="smsbower:0",
        )

        def acquire_new_number(item):
            item.phone = "+234555000111"
            item.activation_id = "act-2"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        with patch("sms_tool.phone_reuse._smsbower_client") as client_factory, \
             patch("sms_tool.phone_reuse._cancel_smsbower_activation") as cancel, \
             patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number) as acquire:
            old_client = Mock()
            old_client.request_additional.return_value = False
            client_factory.return_value = old_client
            self.assertTrue(_prepare_smsbower_for_send(slot))

        self.assertEqual(slot.phone, "+234555000111")
        self.assertEqual(slot.activation_id, "act-2")
        self.assertEqual(slot.reuse_count, 0)
        cancel.assert_called_once_with(slot)
        acquire.assert_called_once_with(slot)

    def test_smsbower_sms_timeout_switches_number_and_retries_same_flow(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=2,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        def acquire_new_number(item):
            item.phone = "+234555000111"
            item.activation_id = "act-2"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number), \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", side_effect=[None, "111111"]), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}), \
             patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[3], "+233555123456")
        self.assertEqual(send.call_args_list[1].args[3], "+234555000111")
        client.cancel.assert_called_once_with("act-1")
        client.complete.assert_called_once_with("act-2")
        self.assertEqual(result["phone"], "+234555000111")

    def test_smsbower_sms_timeout_retries_even_when_number_attempts_is_one(self):
        slot = PhoneSlot(
            phone="+233555123456",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=1,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])

        def acquire_new_number(item):
            item.phone = "+234555000111"
            item.activation_id = "act-2"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number), \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}) as send, \
             patch("sms_tool.phone_reuse._wait_smsbower_code", side_effect=[None, "111111"]), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}), \
             patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[3], "+233555123456")
        self.assertEqual(send.call_args_list[1].args[3], "+234555000111")

    def test_smsbower_phone_recently_used_validate_failure_switches_number_next_round(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            slot = PhoneSlot(
                phone="+233555123456",
                provider="smsbower",
                api_key="test-key",
                activation_id="act-1",
                reuse_count=1,
                max_reuse_count=3,
                number_attempts=1,
                slot_id="smsbower:0",
            )
            pool = PhonePool(phones=[slot], state_file=state_path)

            client = Mock()
            client.cancel.return_value = True
            with patch("sms_tool.phone_reuse._prepare_smsbower_for_send", return_value=True), \
                 patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}), \
                 patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="979739"), \
                 patch("sms_tool.phone_reuse.validate_phone_otp", return_value={
                     "ok": False,
                     "status_code": 429,
                     "body": '{"error":{"code":"phone_recently_used","message":"This phone number was recently used. Please try again later."}}',
                 }), \
                 patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
                result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "phone_validate_failed:429")
            client.cancel.assert_called_once_with("act-1")
            self.assertEqual(slot.phone, "")
            self.assertEqual(slot.activation_id, "")
            self.assertEqual(slot.reuse_count, 0)

            next_pool = PhonePool(
                phones=[PhoneSlot(phone="", provider="smsbower", api_key="test-key", slot_id="smsbower:0")],
                state_file=state_path,
            )
            next_pool.load_state()
            self.assertEqual(next_pool.phones[0].phone, "")
            self.assertEqual(next_pool.phones[0].activation_id, "")

    def test_smsbower_phone_already_in_use_cancels_and_retries_up_to_ten(self):
        slot = PhoneSlot(
            phone="+573000000001",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=1,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])
        next_number = 2

        def acquire_new_number(item):
            nonlocal next_number
            item.phone = f"+5730000000{next_number:02d}"
            item.activation_id = f"act-{next_number}"
            item.reuse_count = 0
            item.last_sms_code = ""
            next_number += 1
            return True

        rejected = {
            "ok": False,
            "status_code": 400,
            "body": '{"error":{"message":"Phone number already in use. Please use a different phone number."}}',
        }
        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number) as acquire, \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}), \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="123456"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", side_effect=[rejected] * 9 + [
                 {"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}
             ]), \
             patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(result["activation_id"], "act-10")
        self.assertEqual(client.cancel.call_count, 9)
        self.assertEqual(acquire.call_count, 9)
        client.complete.assert_called_once_with("act-10")

    def test_smsbower_phone_in_use_send_failure_cancels_and_retries_up_to_ten(self):
        slot = PhoneSlot(
            phone="+573000000001",
            provider="smsbower",
            api_key="test-key",
            activation_id="act-1",
            max_reuse_count=1,
            number_attempts=1,
            slot_id="smsbower:0",
        )
        pool = PhonePool(phones=[slot])
        next_number = 2

        def acquire_new_number(item):
            nonlocal next_number
            item.phone = f"+5730000000{next_number:02d}"
            item.activation_id = f"act-{next_number}"
            item.reuse_count = 0
            item.last_sms_code = ""
            next_number += 1
            return True

        rejected = {
            "ok": False,
            "status_code": 400,
            "error_code": "phone_number_in_use",
            "body": '{"error":{"message":"Phone number already in use. Please use a different phone number."}}',
            "message": "Phone number already in use. Please use a different phone number.",
        }
        client = Mock()
        client.cancel.return_value = True
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._acquire_smsbower_number", side_effect=acquire_new_number) as acquire, \
             patch("sms_tool.phone_reuse.send_phone_otp", side_effect=[rejected] * 9 + [{"ok": True}]), \
             patch("sms_tool.phone_reuse._wait_smsbower_code", return_value="123456") as wait_code, \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={
                 "ok": True, "continue_url": "http://localhost/callback?code=x&state=y"
             }), \
             patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(result["activation_id"], "act-10")
        self.assertEqual(client.cancel.call_count, 9)
        self.assertEqual(acquire.call_count, 9)
        wait_code.assert_called_once()
        client.complete.assert_called_once_with("act-10")

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

    def test_a_removed_static_source_is_rejected_with_a_reason(self):
        """``phone_pool``/``static``/``legacy`` used to select a static list.
        They must fail loudly now: silently coercing to a provider would start
        renting numbers against a config the operator never wrote."""
        for value in sorted(sms_providers.REMOVED_SOURCE_VALUES):
            with self.subTest(source=value):
                cfg = {"phone_reuse": {"source": value, "smsbower": {"api_key": "test-key"}}}
                with patch.dict(phone_reuse.CFG, cfg, clear=False):
                    with self.assertRaises(ValueError) as ctx:
                        create_phone_pool()
                self.assertIn("static phone pool was removed", str(ctx.exception))

    def test_an_unknown_source_is_rejected_and_lists_the_choices(self):
        cfg = {"phone_reuse": {"source": "sms_pool", "smsbower": {"api_key": "test-key"}}}
        with patch.dict(phone_reuse.CFG, cfg, clear=False):
            with self.assertRaises(ValueError) as ctx:
                create_phone_pool()
        message = str(ctx.exception)
        self.assertIn("sms_pool", message)
        for key in sms_providers.available_provider_keys():
            self.assertIn(key, message)

    def test_the_source_error_helper_is_empty_for_usable_values(self):
        for value in ("", None, "smsbower", "Hero-SMS"):
            with self.subTest(source=value):
                self.assertEqual("", phone_reuse.phone_reuse_source_error(value))

    def test_a_stale_static_pool_key_does_not_add_slots(self):
        """The key is simply ignored now -- the selected provider is the only
        source of slots."""
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "state_file": f"{tmp}/phone_state.json",
                    "smsbower": {"api_key": "test-key", "pool_size": 1},
                    "phone_pool": [
                        {"phone": "+15485091782", "sms_api_url": "https://sms789.com/sms/by_key?key=test"}
                    ],
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(len(pool.phones), 1)
        self.assertEqual(pool.phones[0].provider, "smsbower")
        self.assertEqual(pool.phones[0].phone, "")
        self.assertEqual(pool.phones[0].max_reuse_count, 1)

    def test_a_saved_static_slot_is_retired_without_a_migration(self):
        """State written by the removed mode carries ``provider="legacy"``. It
        must not match any slot, so old state files age out instead of needing a
        migration step."""
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"smsbower:0","provider":"legacy",'
                    '"phone":"+15485091782","reuse_count":2,"max_reuse_count":3}]}'
                )
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "state_file": state_path,
                    "smsbower": {"api_key": "test-key", "pool_size": 1},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(pool.phones[0].provider, "smsbower")
        self.assertEqual(pool.phones[0].phone, "")
        self.assertEqual(pool.phones[0].reuse_count, 0)

    def test_the_selected_provider_supplies_slot_identity_and_endpoint(self):
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "herosms",
                    "state_file": f"{tmp}/phone_state.json",
                    "smsbower": {"api_key": "smsbower-key", "pool_size": 1},
                    "herosms": {"api_key": "hero-key", "pool_size": 2},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(["herosms:0", "herosms:1"], [slot.slot_id for slot in pool.phones])
        for slot in pool.phones:
            self.assertEqual("herosms", slot.provider)
            self.assertEqual("hero-key", slot.api_key)
            self.assertEqual(
                sms_providers.PROVIDERS["herosms"].default_endpoint, slot.endpoint)

    def test_an_explicit_endpoint_overrides_the_registry_default(self):
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "grizzly",
                    "state_file": f"{tmp}/phone_state.json",
                    "grizzly": {"api_key": "g-key", "endpoint": "https://mirror.example/handler_api.php"},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual("grizzly", pool.phones[0].provider)
        self.assertEqual("https://mirror.example/handler_api.php", pool.phones[0].endpoint)

    def test_the_provider_key_is_read_from_its_own_env_var(self):
        """Each provider has its own env var, so configuring one cannot leak a
        key into another."""
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "herosms",
                    "state_file": f"{tmp}/phone_state.json",
                    "herosms": {"api_key": "$HEROSMS_API_KEY"},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False), \
                 patch.dict("os.environ", {"HEROSMS_API_KEY": "env-hero"}, clear=False):
                pool = create_phone_pool()

        self.assertEqual("env-hero", pool.phones[0].api_key)

    def test_has_phone_reuse_config_tracks_the_selected_provider(self):
        with patch.dict(phone_reuse.CFG, {"phone_reuse": {"source": "smsbower", "smsbower": {"api_key": "k"}}}, clear=False):
            self.assertTrue(phone_reuse.has_phone_reuse_config())
        with patch.dict(phone_reuse.CFG, {"phone_reuse": {"source": "herosms", "smsbower": {"api_key": "k"}}}, clear=False):
            self.assertFalse(phone_reuse.has_phone_reuse_config())
        with patch.dict(phone_reuse.CFG, {"phone_reuse": {"source": "herosms", "herosms": {"api_key": "k"}}}, clear=False):
            self.assertTrue(phone_reuse.has_phone_reuse_config())


    def test_smsbower_pool_uses_last_selected_country_and_exact_tier(self):
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "state_file": f"{tmp}/phone_state.json",
                    "smsbower": {
                        "api_key": "test-key",
                        "service": "dr",
                        "country": "33",
                        "min_price": "0.026",
                        "max_price": "0.026",
                        "target_price": "0.026",
                        "provider_ids": "3243,3253",
                    },
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(pool.phones[0].service, "dr")
        self.assertEqual(pool.phones[0].country, "33")
        self.assertEqual(pool.phones[0].min_price, "0.026")
        self.assertEqual(pool.phones[0].max_price, "0.026")
        self.assertEqual(pool.phones[0].provider_ids, "3243,3253")

    def test_saved_smsbower_activation_is_not_reused_after_tier_change(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"smsbower:0","provider":"smsbower",'
                    '"service":"dr","country":"38","min_price":"0.054","max_price":"0.054",'
                    '"phone":"+233555123456","activation_id":"act-old","reuse_count":0}]}'
                )
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "state_file": state_path,
                    "smsbower": {
                        "api_key": "test-key",
                        "service": "dr",
                        "country": "33",
                        "min_price": "0.026",
                        "max_price": "0.026",
                    },
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(pool.phones[0].country, "33")
        self.assertEqual(pool.phones[0].phone, "")
        self.assertEqual(pool.phones[0].activation_id, "")

    def test_source_override_selects_another_provider(self):
        """``--phone-source`` picks a provider for one run without editing the
        config. The override is what gets validated, so a stale config value
        cannot block a provider that was requested explicitly."""
        with TemporaryDirectory() as tmp:
            cfg = {
                "phone_reuse": {
                    "source": "phone_pool",
                    "state_file": f"{tmp}/phone_state.json",
                    "smsbower": {"api_key": "smsbower-key", "pool_size": 1},
                    "herosms": {"api_key": "hero-key", "pool_size": 1},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool(source_override="herosms")

        self.assertEqual(len(pool.phones), 1)
        self.assertEqual(pool.phones[0].provider, "herosms")
        self.assertEqual(pool.phones[0].api_key, "hero-key")

    def test_source_override_still_rejects_a_removed_value(self):
        cfg = {"phone_reuse": {"smsbower": {"api_key": "k"}}}
        with patch.dict(phone_reuse.CFG, cfg, clear=False):
            with self.assertRaises(ValueError):
                create_phone_pool(source_override="phone_pool")

    def test_saved_state_does_not_override_configured_max_reuse(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"smsbower:0","provider":"smsbower",'
                    '"phone":"+233555123456","activation_id":"act-1","reuse_count":0,'
                    '"max_reuse_count":3}]}'
                )
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "max_reuse_count": 1,
                    "state_file": state_path,
                    "smsbower": {"api_key": "test-key", "pool_size": 1},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(pool.phones[0].phone, "+233555123456")
        self.assertEqual(pool.phones[0].activation_id, "act-1")
        self.assertEqual(pool.phones[0].max_reuse_count, 1)

    def test_registration_requires_phone_when_pool_is_enabled(self):
        with patch.dict(registration.CFG, {"codex_oauth": {}}, clear=False):
            self.assertFalse(registration._registration_requires_phone_verification(None))
            self.assertTrue(registration._registration_requires_phone_verification(object()))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class SmsBowerCancelObservationTests(unittest.TestCase):
    """``cancel`` 必须把供应商的**原始答复**记下来。

    这条链路的失败模式是静默的：``cancel`` 把异常吞成 ``False``，
    ``_cancel_smsbower_activation`` 又把返回值整个丢掉。于是
    「因激活太新被拒」（sms-activate 协议族会回 ``EARLY_CANCEL_DENIED``）
    与「网络挂了」在日志里长得一模一样。

    后果不是日志难看，而是**问题不可判**：想回答「本仓供应商到底有没有早期
    取消窗口」就得花钱做专门的探测。加一行日志之后，下一次真实跑批免费给出答案。
    """

    def setUp(self):
        self.capture = _Capture()
        logger = logging.getLogger("sms_tool.smsbower")
        logger.addHandler(self.capture)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, self.capture)

    def _client(self, *, status=None, error=None):
        client = SmsBowerClient(api_key="test-key")
        if error is not None:
            patcher = patch.object(client, "set_status", side_effect=error)
        else:
            patcher = patch.object(client, "set_status", return_value=status)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return client, mocked

    def _events(self):
        return [getattr(record, "event", "") for record in self.capture.records]

    def test_access_cancel_returns_true_and_stays_quiet(self):
        client, mocked = self._client(status="ACCESS_CANCEL")
        self.assertTrue(client.cancel("act-1"))
        self.assertEqual(self.capture.records, [])
        self.assertEqual(mocked.call_args.args, ("act-1", "8"))

    def test_an_early_refusal_is_reported_with_the_vendor_status(self):
        """核心回归：``EARLY_CANCEL_DENIED`` 必须可见，且原样带出来。"""
        client, _ = self._client(status="EARLY_CANCEL_DENIED")
        self.assertFalse(client.cancel("act-1"))
        self.assertIn("smsbower_cancel_refused", self._events())
        refused = [r for r in self.capture.records if r.event == "smsbower_cancel_refused"]
        self.assertEqual(refused[0].vendor_status, "EARLY_CANCEL_DENIED")
        self.assertEqual(refused[0].activation_id, "act-1")

    def test_an_unexpected_status_is_reported_too(self):
        """只认 ``ACCESS_CANCEL`` —— 任何其它答复都值得留痕。"""
        for status in ("WRONG_STATUS", "NO_ACTIVATION", "BAD_ACTION", ""):
            with self.subTest(status=status):
                self.capture.records.clear()
                client, _ = self._client(status=status)
                self.assertFalse(client.cancel("act-1"))
                self.assertIn("smsbower_cancel_refused", self._events())

    def test_a_transport_failure_is_distinguishable_from_a_refusal(self):
        """两类失败必须能分开 —— 否则「被拒」仍然藏在「网络错」里。"""
        client, _ = self._client(error=RuntimeError("connection reset"))
        self.assertFalse(client.cancel("act-1"))
        self.assertIn("smsbower_cancel_failed", self._events())
        self.assertNotIn("smsbower_cancel_refused", self._events())

    def test_cancel_still_never_raises(self):
        """加了日志也不能改契约：调用方依赖它不抛。"""
        for error in (RuntimeError("boom"), ValueError("bad"), OSError("net")):
            with self.subTest(error=error):
                client, _ = self._client(error=error)
                self.assertFalse(client.cancel("act-1"))

    def test_the_record_is_aggregatable(self):
        """``extra`` 字段是给工具用的，不是给人读的散文。"""
        client, _ = self._client(status="EARLY_CANCEL_DENIED")
        client.cancel("act-42")
        record = self.capture.records[0]
        self.assertEqual(record.event, "smsbower_cancel_refused")
        self.assertEqual(record.activation_id, "act-42")
        self.assertEqual(record.vendor_status, "EARLY_CANCEL_DENIED")


class RentalProviderLifecycleTests(unittest.TestCase):
    """The rental lifecycle must key off the *protocol*, not the vendor name.

    Every guard exercised here used to read ``provider == "smsbower"``, written
    when SMSBower was the only rentable vendor -- so the comparison effectively
    meant "is this a rental slot". With ``source=herosms`` all of them evaluated
    False: the number was rented and then never cancelled, never reset, and the
    retry paths skipped the provider branch entirely, leaving the activation
    open and billing while the registration moved on. Nothing failed loudly,
    which is why these tests drive a non-SMSBower slot through the same paths.
    """

    def _slot(self, provider, **overrides):
        values = {
            "phone": "+233555123456",
            "provider": provider,
            "api_key": "test-key",
            "endpoint": sms_providers.default_endpoint(provider),
            "activation_id": "act-1",
            "slot_id": f"{provider}:0",
        }
        values.update(overrides)
        return PhoneSlot(**values)

    def test_no_executed_string_in_the_module_names_a_vendor(self):
        """A source guard, because this regression is silent at runtime.

        The vendor vocabulary belongs to ``sms_providers``; ``phone_reuse`` asks
        ``_is_rental_slot``. Docstrings may name vendors (they explain history),
        but an *executed* constant may not.

        This is deliberately a source guard rather than a behaviour test. The
        two spellings it catches are ``"smsbower"`` used as a comparison and
        ``"smsbower_prepare_failed"`` used inside an error set -- and the second
        one **changes no behaviour** (the non-matching spelling reached the same
        ``return False`` through the fallthrough), so no assertion on the
        return value could ever detect it. Verified by mutation: restoring the
        literal set reddens this test and nothing else.
        """
        with open(phone_reuse.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

        offenders = [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and "smsbower" in node.value.lower()
        ]
        self.assertEqual([], offenders, f"vendor name baked into executed code: {offenders}")

    def test_every_available_provider_is_a_rental_slot(self):
        for key in sms_providers.available_provider_keys():
            with self.subTest(provider=key):
                self.assertTrue(phone_reuse._is_rental_slot(self._slot(key)))

    def test_the_nexsms_family_is_a_rental_slot_with_its_own_client(self):
        """``nexsms`` rents through a different protocol family, so it must be a
        rental slot *and* must get its own client.

        Sending it the sms-activate client would put an ``?action=`` query string
        on a REST endpoint: the vendor would answer an envelope with a non-zero
        ``code``, the tool would report a vendor error, and the real defect --
        wrong client for the protocol -- would never be named.

        This replaced a test asserting the opposite (that ``nexsms`` was refused
        because it had no client). The invariant moved with the code: what
        matters now is *which* client it gets, not whether it gets one.
        """
        slot = self._slot("nexsms")
        self.assertTrue(phone_reuse._is_rental_slot(slot))
        self.assertIsInstance(phone_reuse._rental_client(slot), NexSmsClient)
        self.assertIsInstance(
            phone_reuse._sms_provider_adapter(slot),
            phone_reuse._RentalSmsProviderAdapter,
        )

    def test_an_unknown_provider_is_not_a_rental_slot(self):
        slot = self._slot("nope")
        self.assertFalse(phone_reuse._is_rental_slot(slot))
        with self.assertRaises(ValueError) as caught:
            phone_reuse._sms_provider_adapter(slot)
        self.assertIn("nope", str(caught.exception))

    def test_the_nexsms_client_uses_the_slots_own_endpoint(self):
        slot = self._slot("nexsms")
        with patch("sms_tool.phone_reuse.NexSmsClient") as factory:
            phone_reuse._nexsms_client(slot)
        factory.assert_called_once_with(
            api_key="test-key",
            endpoint=sms_providers.PROVIDERS["nexsms"].default_endpoint,
        )

    def test_the_rental_client_uses_the_slots_own_endpoint(self):
        for key in sms_providers.available_provider_keys():
            if not sms_providers.PROVIDERS[key].speaks_sms_activate:
                continue
            with self.subTest(provider=key):
                slot = self._slot(key)
                with patch("sms_tool.phone_reuse.SmsBowerClient") as factory:
                    phone_reuse._smsbower_client(slot)
                factory.assert_called_once_with(
                    api_key="test-key",
                    endpoint=sms_providers.PROVIDERS[key].default_endpoint,
                )

    def test_a_non_smsbower_activation_completes_through_the_rental_adapter(self):
        slot = self._slot("herosms")
        client = Mock()
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            phone_reuse._complete_provider_activation(slot)

        client.complete.assert_called_once_with("act-1")
        client.cancel.assert_not_called()
        self.assertEqual("", slot.activation_id)
        self.assertEqual("", slot.phone)

    def test_a_non_smsbower_activation_cancels_through_the_rental_adapter(self):
        slot = self._slot("grizzly")
        client = Mock()
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client):
            phone_reuse._cancel_provider_activation(slot)

        client.cancel.assert_called_once_with("act-1")
        self.assertEqual("", slot.activation_id)
        self.assertEqual("", slot.phone)

    def test_reset_exhausted_slots_completes_a_non_smsbower_activation(self):
        """The costly half of the bug: an uncompleted activation keeps billing."""
        slot = self._slot("herosms", reuse_count=1, max_reuse_count=1)
        pool = PhonePool(phones=[slot])
        with patch("sms_tool.phone_reuse._complete_provider_activation") as complete:
            reset_count = pool.reset_exhausted_slots()

        complete.assert_called_once_with(slot)
        self.assertEqual(1, reset_count)

    def test_retiring_a_non_smsbower_slot_cancels_its_activation(self):
        slot = self._slot("grizzly", reuse_count=1, max_reuse_count=1)
        pool = PhonePool(phones=[slot])
        with patch("sms_tool.phone_reuse._cancel_provider_activation") as cancel:
            phone_reuse._retire_phone_slot_for_batch(pool, slot, "fraud_guard")

        cancel.assert_called_once_with(slot)
        self.assertTrue(slot.is_exhausted)

    def test_the_retry_predicate_treats_a_non_smsbower_pool_as_rental(self):
        pool = PhonePool(phones=[self._slot("herosms")])
        self.assertTrue(
            phone_reuse._should_retry_with_new_provider_number(
                pool, {"error": "phone_sms_timeout"}))

    def test_a_pool_without_a_rental_slot_is_never_retried_as_rental(self):
        pool = PhonePool(phones=[self._slot("legacy")])
        self.assertFalse(
            phone_reuse._should_retry_with_new_provider_number(
                pool, {"error": "phone_sms_timeout"}))

    def test_operator_output_names_the_actual_provider(self):
        """``[smsbower]`` on a HeroSMS slot sends the operator to the wrong
        dashboard while debugging a live batch."""
        slot = self._slot("herosms")
        client = Mock()
        client.complete.return_value = True
        buffer = io.StringIO()
        with patch("sms_tool.phone_reuse._smsbower_client", return_value=client), \
             contextlib.redirect_stdout(buffer):
            phone_reuse._complete_provider_activation(slot)

        output = buffer.getvalue()
        self.assertIn("[herosms]", output)
        self.assertNotIn("[smsbower]", output)

    def test_pool_status_shows_service_and_country_for_a_non_smsbower_slot(self):
        slot = self._slot("grizzly", service="dr", country="33")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            phone_reuse.print_phone_pool_status(PhonePool(phones=[slot]))

        output = buffer.getvalue()
        self.assertIn("[grizzly]", output)
        self.assertIn("service=dr", output)
        self.assertIn("country=33", output)


    def test_phone_registration_follows_the_configured_source(self):
        """The standalone phone-registration entrypoint used to read
        ``phone_reuse.smsbower`` unconditionally, so a config switched to
        HeroSMS or Grizzly still rented an SMSBower number with the SMSBower
        key -- the wrong vendor, silently."""
        from sms_tool import phone_registration

        cfg = {
            "source": "herosms",
            "smsbower": {"api_key": "smsbower-key", "country": "38"},
            "herosms": {"api_key": "hero-key", "country": "33"},
        }
        provider, section, api_key, endpoint = phone_registration._provider_selection(cfg)

        self.assertEqual("herosms", provider)
        self.assertEqual("hero-key", api_key)
        self.assertEqual("33", section.get("country"))
        self.assertEqual(sms_providers.PROVIDERS["herosms"].default_endpoint, endpoint)

    def test_phone_registration_honours_an_explicit_endpoint_and_key(self):
        from sms_tool import phone_registration

        cfg = {
            "source": "grizzly",
            "grizzly": {"endpoint": "https://mirror.example/handler_api.php"},
        }
        provider, _, api_key, endpoint = phone_registration._provider_selection(cfg, "explicit-key")

        self.assertEqual("grizzly", provider)
        self.assertEqual("explicit-key", api_key)
        self.assertEqual("https://mirror.example/handler_api.php", endpoint)

    def test_phone_registration_defaults_to_the_registry_provider(self):
        from sms_tool import phone_registration

        provider, section, _, endpoint = phone_registration._provider_selection({})

        self.assertEqual(sms_providers.DEFAULT_PROVIDER, provider)
        self.assertEqual({}, section)
        self.assertEqual(
            sms_providers.default_endpoint(sms_providers.DEFAULT_PROVIDER), endpoint)

    # -- the other half of `_provider_selection` ---------------------------- #
    #
    # Selecting the right provider/key/endpoint is only half the job: the flow
    # also has to build the right *client* and address the rental by the right
    # *key*. Both were hardcoded to the sms-activate family, which stayed
    # harmless only while every selectable vendor spoke it.

    def test_the_standalone_flow_gets_the_protocol_appropriate_client(self):
        """`--phone-register` used to construct `SmsBowerClient` unconditionally.

        That was a shape defect rather than a live one while all three selectable
        vendors spoke sms-activate -- the class name was wrong but the wire format
        matched. Offering `nexsms` turned it into a real failure: the sms-activate
        client would post ``/stubs/handler_api.php?action=…`` at
        ``api.nexsms.net`` and surface an opaque vendor error.
        """
        nexsms = phone_reuse.rental_client("nexsms", "nex-key", "https://api.nexsms.net")
        self.assertIsInstance(nexsms, NexSmsClient)
        self.assertEqual("nex-key", nexsms.api_key)
        self.assertEqual("https://api.nexsms.net", nexsms.endpoint)

        sms_activate = phone_reuse.rental_client("herosms", "hero-key", "https://hero.example")
        self.assertIsInstance(sms_activate, SmsBowerClient)
        self.assertEqual("hero-key", sms_activate.api_key)

        with self.assertRaises(ValueError):
            phone_reuse.rental_client("not-a-provider", "k", "https://x.example")

    def test_the_lifecycle_key_follows_the_protocol_family(self):
        from types import SimpleNamespace

        # NexSMS issues no activation id, so the phone number *is* the key.
        self.assertEqual(
            "+233555123456",
            phone_reuse.rental_handle_for(
                "nexsms", SimpleNamespace(phone="+233555123456", activation_id="")
            ),
        )
        self.assertEqual(
            "act-9",
            phone_reuse.rental_handle_for(
                "herosms", SimpleNamespace(phone="+233555123456", activation_id="act-9")
            ),
        )
        # Nothing rented yet reads as "no handle" for either family.
        self.assertEqual(
            "", phone_reuse.rental_handle_for("nexsms", SimpleNamespace(phone=""))
        )

    def test_the_slot_and_the_standalone_flow_share_one_rule(self):
        """Two entry points, one rule -- otherwise they drift apart silently."""
        for provider in sms_providers.available_provider_keys():
            slot = PhoneSlot(provider=provider, phone="+233555123456", activation_id="act-9")
            self.assertEqual(
                phone_reuse.rental_handle_for(provider, slot),
                slot.rental_handle,
                provider,
            )

    def test_the_standalone_flow_never_names_a_concrete_client(self):
        """Read the source: the defect *is* a hardcoded class name.

        Exercising `run_phone_register` end to end needs a live registration, so
        no behavioural test can reach the choice of client class. The invariant is
        therefore pinned where it actually lives -- the module must route through
        `rental_client`, and must not name a vendor's client at all.
        """
        source = (
            Path(__file__).resolve().parents[1] / "sms_tool" / "phone_registration.py"
        ).read_text(encoding="utf-8")
        for name in ("SmsBowerClient", "NexSmsClient"):
            self.assertNotIn(name, source)
        self.assertIn("rental_client(provider, api_key, endpoint)", source)


class ProviderKeyStatusTests(unittest.TestCase):
    """`provider_key_status` -- the report `--doctor` builds its advice from.

    Its whole contract is "say what resolved, never say what the key is", and
    both halves need pinning: a report that leaks the key is a new leak vector,
    and one that guesses the origin is worse than useless when the question is
    "why does this machine work and that one not".
    """

    def _rows(self, section, env=None):
        with patch.dict("os.environ", env or {}, clear=True):
            return phone_reuse.provider_key_status(section)

    def test_every_available_provider_gets_a_row_with_exactly_one_selected(self):
        rows = self._rows({"source": "herosms", "herosms": {"api_key": "k"}})
        self.assertEqual(
            [row["provider"] for row in rows],
            list(sms_providers.available_provider_keys()),
        )
        selected = [row["provider"] for row in rows if row["selected"]]
        self.assertEqual(selected, ["herosms"])

    def test_a_literal_key_is_reported_as_coming_from_the_config(self):
        rows = {row["provider"]: row for row in self._rows({"smsbower": {"api_key": "literal-key"}})}
        self.assertTrue(rows["smsbower"]["configured"])
        self.assertEqual(rows["smsbower"]["origin"], phone_reuse.KEY_ORIGIN_CONFIG)

    def test_a_placeholder_needs_the_variable_to_actually_be_set(self):
        """`$ENV` in the config is not the same as a key being available."""
        section = {"source": "grizzly", "grizzly": {"api_key": "$GRIZZLY_API_KEY"}}
        with_env = {row["provider"]: row for row in self._rows(section, {"GRIZZLY_API_KEY": "from-env"})}
        self.assertTrue(with_env["grizzly"]["configured"])
        self.assertEqual(with_env["grizzly"]["origin"], phone_reuse.KEY_ORIGIN_ENV)

        without_env = {row["provider"]: row for row in self._rows(section)}
        self.assertFalse(without_env["grizzly"]["configured"])
        self.assertEqual(without_env["grizzly"]["origin"], phone_reuse.KEY_ORIGIN_MISSING)

    def test_the_vendor_placeholder_spelling_is_also_an_env_lookup(self):
        """`YOUR_<ENV>` is the other documented placeholder shape."""
        section = {"source": "herosms", "herosms": {"api_key": "YOUR_HEROSMS_API_KEY"}}
        rows = {row["provider"]: row for row in self._rows(section, {"HEROSMS_API_KEY": "from-env"})}
        self.assertTrue(rows["herosms"]["configured"])
        self.assertEqual(rows["herosms"]["origin"], phone_reuse.KEY_ORIGIN_ENV)

    def test_a_blank_key_falls_back_to_the_providers_own_variable(self):
        rows = {row["provider"]: row for row in self._rows({"smsbower": {"api_key": ""}}, {"SMSBOWER_API_KEY": "env"})}
        self.assertTrue(rows["smsbower"]["configured"])
        self.assertEqual(rows["smsbower"]["origin"], phone_reuse.KEY_ORIGIN_ENV)

    def test_the_key_value_never_appears_in_the_report(self):
        """The report is meant to be pasteable into a ticket."""
        secret = "Zq3Xk9Mv7Rt2Lp5Wb8Qq1Ww2Ee3Rr4T"
        rows = self._rows({"source": "smsbower", "smsbower": {"api_key": secret}})
        self.assertTrue(any(row["configured"] for row in rows))
        self.assertNotIn(secret, repr(rows))

    def test_the_endpoint_reports_the_override_and_falls_back_to_the_registry(self):
        section = {
            "source": "grizzly",
            "grizzly": {"api_key": "k", "endpoint": "https://mirror.example/handler_api.php"},
            "nexsms": {"api_key": "k2"},
        }
        rows = {row["provider"]: row for row in self._rows(section)}
        self.assertEqual(rows["grizzly"]["endpoint"], "https://mirror.example/handler_api.php")
        self.assertEqual(
            rows["nexsms"]["endpoint"],
            sms_providers.PROVIDERS["nexsms"].default_endpoint,
        )

    def test_resolve_secret_still_honours_every_indirection_form(self):
        """`_resolve_secret` now derives from `_key_lookup`; behaviour is pinned."""
        with patch.dict("os.environ", {"HEROSMS_API_KEY": "from-env"}, clear=True):
            self.assertEqual(phone_reuse._resolve_secret("literal", "herosms"), "literal")
            self.assertEqual(phone_reuse._resolve_secret("$HEROSMS_API_KEY", "herosms"), "from-env")
            self.assertEqual(phone_reuse._resolve_secret("YOUR_HEROSMS_API_KEY", "herosms"), "from-env")
            self.assertEqual(phone_reuse._resolve_secret("", "herosms"), "from-env")
            # A `$` naming some other variable must not fall back to the provider's.
            self.assertEqual(phone_reuse._resolve_secret("$SOME_OTHER_VAR", "herosms"), "")
            # A lone `$` is a literal, not an indirection with an empty name.
            self.assertEqual(phone_reuse._resolve_secret("$", "herosms"), "$")


    def test_missing_key_hint_names_the_selected_providers_own_key(self):
        """The hint follows `phone_reuse.source`, not a hardcoded vendor."""
        with patch.dict(phone_reuse.CFG, {"phone_reuse": {"source": "nexsms"}}, clear=False):
            hint = phone_reuse.missing_key_hint()
        self.assertIn("phone_reuse.nexsms.api_key", hint)
        self.assertIn("NEXSMS_API_KEY", hint)
        self.assertNotIn("smsbower", hint)

    def test_missing_key_hint_accepts_an_explicit_provider(self):
        hint = phone_reuse.missing_key_hint("grizzly")
        self.assertIn("phone_reuse.grizzly.api_key", hint)
        self.assertIn("GRIZZLY_API_KEY", hint)


if __name__ == "__main__":
    unittest.main()

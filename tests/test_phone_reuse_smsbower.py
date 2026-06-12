import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sms_tool import registration
from sms_tool import phone_reuse
from sms_tool.phone_reuse import PhonePool, PhoneSlot, _prepare_smsbower_for_send, _wait_for_send_cooldown, complete_phone_verification_with_reuse, create_phone_pool, send_phone_otp
from sms_tool.smsbower import SmsBowerClient, normalize_country, normalize_phone, normalize_service


class SmsBowerPhoneReuseTests(unittest.TestCase):
    def test_openai_ghana_aliases(self):
        self.assertEqual(normalize_service("openai"), "dr")
        self.assertEqual(normalize_service("OpenAI (ChatGPT)"), "dr")
        self.assertEqual(normalize_country("Ghana"), "38")
        self.assertEqual(normalize_country("+233"), "38")
        self.assertEqual(normalize_phone("233555123456"), "+233555123456")

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
            reset_count = pool.reset_exhausted_smsbower_slots()

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

    def test_static_phone_pool_config_change_ignores_stale_state(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"phone_pool:0","provider":"legacy",'
                    '"phone":"+15485091782","sms_api_url":"https://old.example/sms",'
                    '"reuse_count":1,"max_reuse_count":3}]}'
                )
            cfg = {
                "phone_reuse": {
                    "source": "phone_pool",
                    "max_reuse_count": 3,
                    "state_file": state_path,
                    "phone_pool": [
                        {"phone": "+817093203174", "sms_api_url": "https://new.example/sms"}
                    ],
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(len(pool.phones), 1)
        self.assertEqual(pool.phones[0].phone, "+817093203174")
        self.assertEqual(pool.phones[0].sms_api_url, "https://new.example/sms")
        self.assertEqual(pool.phones[0].reuse_count, 0)

    def test_static_phone_pool_keeps_state_when_config_entry_matches(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"current_index":0,"phones":[{"slot_id":"phone_pool:0","provider":"legacy",'
                    '"phone":"+817093203174","sms_api_url":"https://new.example/sms",'
                    '"reuse_count":2,"max_reuse_count":3}]}'
                )
            cfg = {
                "phone_reuse": {
                    "source": "phone_pool",
                    "max_reuse_count": 3,
                    "state_file": state_path,
                    "phone_pool": [
                        {"phone": "+817093203174", "sms_api_url": "https://new.example/sms"}
                    ],
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(pool.phones[0].phone, "+817093203174")
        self.assertEqual(pool.phones[0].sms_api_url, "https://new.example/sms")
        self.assertEqual(pool.phones[0].reuse_count, 2)

    def test_phone_source_phone_pool_uses_static_links_even_when_smsbower_configured(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            cfg = {
                "phone_reuse": {
                    "source": "phone_pool",
                    "max_reuse_count": 2,
                    "state_file": state_path,
                    "smsbower": {"api_key": "test-key", "pool_size": 1},
                    "phone_pool": [
                        {"phone": "+15485091782", "sms_api_url": "https://sms789.com/sms/by_key?key=test"}
                    ],
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(len(pool.phones), 1)
        self.assertEqual(pool.phones[0].provider, "legacy")
        self.assertEqual(pool.phones[0].phone, "+15485091782")
        self.assertEqual(pool.phones[0].sms_api_url, "https://sms789.com/sms/by_key?key=test")
        self.assertEqual(pool.phones[0].max_reuse_count, 2)

    def test_phone_source_smsbower_ignores_static_links(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            cfg = {
                "phone_reuse": {
                    "source": "smsbower",
                    "state_file": state_path,
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
        self.assertEqual(pool.phones[0].max_reuse_count, 1)

    def test_phone_source_nextsms_uses_nextsms_config(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            cfg = {
                "phone_reuse": {
                    "source": "nextsms",
                    "state_file": state_path,
                    "nextsms": {
                        "api_key": "nx_sms_test",
                        "endpoint": "https://sms.nextactionplus.com/api/",
                        "service": "openai",
                        "country": "US",
                        "pricing_option": 1,
                        "pool_size": 2,
                    },
                    "smsbower": {"api_key": "smsbower-key", "pool_size": 1},
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool()

        self.assertEqual(len(pool.phones), 2)
        self.assertEqual(pool.phones[0].provider, "nextsms")
        self.assertEqual(pool.phones[0].api_key, "nx_sms_test")
        self.assertEqual(pool.phones[0].service, "openai")
        self.assertEqual(pool.phones[0].country, "US")
        self.assertEqual(pool.phones[0].pricing_option, 1)

    def test_source_override_can_force_smsbower_for_registration(self):
        with TemporaryDirectory() as tmp:
            state_path = f"{tmp}/phone_state.json"
            cfg = {
                "phone_reuse": {
                    "source": "phone_pool",
                    "state_file": state_path,
                    "smsbower": {"api_key": "smsbower-key", "pool_size": 1},
                    "phone_pool": [
                        {"phone": "+15485091782", "sms_api_url": "https://sms789.com/sms/by_key?key=test"}
                    ],
                }
            }
            with patch.dict(phone_reuse.CFG, cfg, clear=False):
                pool = create_phone_pool(source_override="smsbower")

        self.assertEqual(len(pool.phones), 1)
        self.assertEqual(pool.phones[0].provider, "smsbower")
        self.assertEqual(pool.phones[0].api_key, "smsbower-key")

    def test_nextsms_phone_verification_acquires_and_polls_code(self):
        slot = PhoneSlot(
            phone="",
            provider="nextsms",
            api_key="nx_sms_test",
            endpoint="https://sms.nextactionplus.com/api/",
            service="openai",
            country="US",
            max_reuse_count=1,
            slot_id="nextsms:0",
        )
        pool = PhonePool(phones=[slot])

        def acquire_new_number(item):
            item.phone = "+13000000000"
            item.activation_id = "ord-1"
            item.reuse_count = 0
            item.last_sms_code = ""
            return True

        client = Mock()
        client.complete.return_value = True
        with patch("sms_tool.phone_reuse._acquire_nextsms_number", side_effect=acquire_new_number) as acquire, \
             patch("sms_tool.phone_reuse.send_phone_otp", return_value={"ok": True}) as send, \
             patch("sms_tool.phone_reuse._wait_nextsms_code", return_value="123456"), \
             patch("sms_tool.phone_reuse.validate_phone_otp", return_value={"ok": True, "continue_url": "http://localhost/callback?code=x&state=y"}), \
             patch("sms_tool.phone_reuse._nextsms_client", return_value=client):
            result = complete_phone_verification_with_reuse(None, "did", "https://auth.openai.com/add-phone", pool)

        self.assertTrue(result["ok"])
        self.assertEqual(result["phone"], "+13000000000")
        self.assertEqual(result["provider"], "nextsms")
        acquire.assert_called_once_with(slot)
        self.assertEqual(send.call_args.args[3], "+13000000000")
        client.complete.assert_called_once_with("ord-1")

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


if __name__ == "__main__":
    unittest.main()

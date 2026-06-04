import unittest
from unittest.mock import Mock, patch

from sms_tool.nextsms import NexSmsClient, normalize_country, normalize_service


class NexSmsClientTests(unittest.TestCase):
    def test_endpoint_accepts_api_base_url(self):
        client = NexSmsClient(api_key="nx_sms_test", endpoint="https://sms.nextactionplus.com/api/")

        self.assertEqual(
            client._url("orders"),
            "https://sms.nextactionplus.com/api/v1/orders",
        )

    def test_normalizes_openai_aliases(self):
        self.assertEqual(normalize_service("dr"), "openai")
        self.assertEqual(normalize_service("OpenAI (ChatGPT)"), "openai")
        self.assertEqual(normalize_country("us"), "US")

    def test_get_number_parses_order_response(self):
        response = Mock(status_code=200, text='{"orders":[{"id":"ord-1","phone_number_full":"+13000000000","price":220}]}')
        response.json.return_value = {
            "orders": [
                {"id": "ord-1", "phone_number_full": "+13000000000", "price": 220}
            ]
        }
        with patch("sms_tool.nextsms._requests.request", return_value=response) as request:
            activation = NexSmsClient("nx_sms_test").get_number(service="openai", country="US", pricing_option=1)

        self.assertEqual(activation.activation_id, "ord-1")
        self.assertEqual(activation.phone, "+13000000000")
        self.assertEqual(activation.service, "openai")
        self.assertEqual(activation.country, "US")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["json"]["pricing_option"], 1)
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer nx_sms_test")

    def test_get_status_extracts_json_code(self):
        response = Mock(status_code=200, text='{"status":"YES","received":true,"message":"Your code is 123456"}')
        response.json.return_value = {"status": "YES", "received": True, "message": "Your code is 123456"}
        with patch("sms_tool.nextsms._requests.request", return_value=response):
            status = NexSmsClient("nx_sms_test").get_status("ord-1")

        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["code"], "123456")


if __name__ == "__main__":
    unittest.main()

"""Behaviour tests for ``sms_tool/nexsms.py`` -- the ``nexsms_json`` family.

Why this file exists
--------------------
``nexsms`` was the last provider in the registry with ``client_available=False``:
the name was reserved, the wire protocol was unverified, and every surface was
forbidden from offering it. It is now wired, so the client is the thing that has
to be right -- and it is the only client in the repo that does **not** speak the
sms-activate handler protocol.

Three properties are load-bearing and each has a silent failure mode, so each is
pinned directly rather than through the registration flow:

1. **The key travels in the query string**, for GET *and* POST. A client that
   sent it as a header would get a normal-looking ``code != 0`` from the vendor
   and be reported as a vendor problem.
2. **The lifecycle key is the phone number, not an activation id.** There is no
   ``activationId`` on the wire at all, so ``NexSmsActivation.activation_id`` is
   always empty -- and anything that branches on it (``PhoneSlot.load_state``,
   ``PhonePool.reset_exhausted_slots``) silently takes the wrong path.
3. **``complete`` is a no-op that reports success.** Reporting failure would make
   the caller cancel a number that had already been paid for and had already
   delivered its code.

There is **no network here**: ``sms_tool.nexsms._requests`` is patched in every
test, which is also what the sms-activate client's tests do.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from sms_tool.nexsms import (
    DEFAULT_ENDPOINT,
    NexSmsActivation,
    NexSmsClient,
    NexSmsError,
    api_phone_number,
)


def _reply(payload, status_code: int = 200) -> Mock:
    """A stand-in for ``requests.Response`` carrying ``payload`` as JSON."""
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _http_error(status_code: int) -> requests.HTTPError:
    """The exception ``raise_for_status()`` raises, with ``.response`` attached.

    The ``response`` attribute is the whole point: it is how the client tells a
    rejected key (401) apart from a flaky upstream (500), and a fake without it
    would make every HTTP failure look retryable.
    """
    response = Mock()
    response.status_code = status_code
    error = requests.HTTPError(f"{status_code} error")
    error.response = response
    return error


class EnvelopeTests(unittest.TestCase):
    """``{code, message, data}`` -- ``code == 0`` is the only success."""

    def test_a_zero_code_yields_the_data(self):
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": 0, "data": {"balance": "12.5", "username": "u"}}),
        ):
            self.assertEqual(12.5, client.get_balance()["balance"])

    def test_a_string_zero_is_accepted(self):
        """The vendor's own examples send a number, but a JSON ``"0"`` is a
        plausible serialisation; comparing with ``!= 0`` would call it a
        failure and report a vendor error for a successful call."""
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": "0", "data": {"balance": "1"}}),
        ):
            self.assertEqual(1.0, client.get_balance()["balance"])

    def test_a_non_zero_code_raises_with_the_vendor_message(self):
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": 12, "message": "号码不足"}),
        ):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertEqual(12, caught.exception.code)
        self.assertIn("号码不足", str(caught.exception))

    def test_a_missing_code_field_is_a_failure_not_a_success(self):
        """``None == 0`` is false, but the ``int()`` conversion must not throw a
        bare ``TypeError`` out of the client -- callers classify on
        ``NexSmsError``, and anything else escapes their handler."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"data": {}})):
            with self.assertRaises(NexSmsError):
                client.get_balance()

    def test_a_non_dict_payload_raises(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(["nope"])):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertIn("unexpected reply", str(caught.exception))

    def test_an_auth_failure_is_not_retryable(self):
        """Retrying a rejected key spends the whole attempt budget to reach the
        same answer."""
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": 1001, "message": "API Key 无效"}),
        ):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertFalse(caught.exception.retryable)

    def test_a_balance_failure_is_not_retryable(self):
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": 1002, "message": "余额不足"}),
        ):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertFalse(caught.exception.retryable)

    def test_an_unclassified_vendor_error_stays_retryable(self):
        """The default must be "try again": the vendor's numeric codes are not
        publicly enumerated, so the only safe default for an unrecognised one is
        the one that loses attempts rather than the one that loses money."""
        client = NexSmsClient(api_key="k")
        with patch(
            "sms_tool.nexsms._requests.get",
            return_value=_reply({"code": 77, "message": "暂时没有可用号码"}),
        ):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertTrue(caught.exception.retryable)


class TransportTests(unittest.TestCase):
    """Where the key goes, and how HTTP failures are classified."""

    def test_the_key_travels_in_the_query_string_on_get(self):
        client = NexSmsClient(api_key="secret")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": {}})) as get:
            client.get_balance()
        self.assertEqual("secret", get.call_args.kwargs["params"]["apiKey"])

    def test_the_key_travels_in_the_query_string_on_post(self):
        """The POST body is business parameters only; the key stays in the
        query string. A client that moved it into the body would get a
        ``code != 0`` for a correctly-formed request."""
        client = NexSmsClient(api_key="secret")
        with patch(
            "sms_tool.nexsms._requests.post",
            return_value=_reply({"code": 0, "data": True}),
        ) as post:
            client.cancel("+233555123456")
        self.assertEqual("secret", post.call_args.kwargs["params"]["apiKey"])
        self.assertNotIn("apiKey", post.call_args.kwargs["json"])

    def test_the_endpoint_is_a_base_host_with_paths_appended(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": {}})) as get:
            client.get_balance()
        url = get.call_args[0][0]
        self.assertTrue(url.startswith(DEFAULT_ENDPOINT), url)
        self.assertTrue(url.endswith("/api/balance"), url)

    def test_a_trailing_slash_on_the_endpoint_does_not_double_up(self):
        client = NexSmsClient(api_key="k", endpoint="https://api.nexsms.net/")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": {}})) as get:
            client.get_balance()
        self.assertNotIn("//api/", get.call_args[0][0])

    def test_http_401_is_not_retryable(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", side_effect=_http_error(401)):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(401, caught.exception.code)

    def test_http_403_is_not_retryable(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", side_effect=_http_error(403)):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertFalse(caught.exception.retryable)

    def test_http_500_stays_retryable(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", side_effect=_http_error(500)):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertTrue(caught.exception.retryable)

    def test_a_connection_error_stays_retryable(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(NexSmsError) as caught:
                client.get_balance()
        self.assertTrue(caught.exception.retryable)


class PhoneNumberTests(unittest.TestCase):
    """The vendor stores numbers without ``+``; the rest of the tool stores them
    with it. Both directions are pure functions so neither can drift."""

    def test_a_prefixed_number_loses_its_prefix(self):
        self.assertEqual("254711408024", api_phone_number("+254711408024"))

    def test_a_double_zero_prefix_is_also_stripped(self):
        self.assertEqual("254711408024", api_phone_number("00254711408024"))

    def test_separators_are_dropped(self):
        self.assertEqual("254711408024", api_phone_number("+254 711-408 024"))

    def test_an_empty_value_stays_empty(self):
        self.assertEqual("", api_phone_number(""))
        self.assertEqual("", api_phone_number(None))


class ReadEndpointTests(unittest.TestCase):
    def test_get_countries_skips_entries_without_an_id(self):
        client = NexSmsClient(api_key="k")
        payload = {"code": 0, "data": [{"id": 38, "name": "加纳"}, {"name": "no id"}, "junk"]}
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(payload)):
            rows = client.get_countries()
        self.assertEqual([{"id": "38", "name": "加纳"}], rows)

    def test_get_countries_returns_empty_for_a_non_list(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": None})):
            self.assertEqual([], client.get_countries())

    def test_get_services_returns_code_and_name(self):
        client = NexSmsClient(api_key="k")
        payload = {"code": 0, "data": [{"code": "dr", "name": "OpenAI"}, {"name": "no code"}]}
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(payload)):
            self.assertEqual([{"code": "dr", "name": "OpenAI"}], client.get_services())

    def test_a_quote_for_one_country_comes_back_as_an_object(self):
        client = NexSmsClient(api_key="k")
        payload = {"code": 0, "data": {
            "countryId": 38, "countryName": "Ghana", "phoneCode": "+233",
            "minPrice": 0.05, "medianPrice": 0.06, "maxPrice": 0.11,
            "priceMap": {"0.05": 4},
        }}
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(payload)) as get:
            quote = client.get_country_quote("dr", "38")
        self.assertEqual(0.05, quote["min_price"])
        self.assertEqual("233", quote["phone_code"])
        self.assertEqual("38", quote["country_id"])
        self.assertEqual({"0.05": 4}, quote["price_map"])
        # countryId is present, so the vendor answers one object.
        self.assertEqual(38, get.call_args.kwargs["params"]["countryId"])

    def test_a_quote_response_that_is_an_array_is_unwrapped(self):
        """Without ``countryId`` the vendor answers every country as an array.
        Both shapes reach this method, so both must be accepted."""
        client = NexSmsClient(api_key="k")
        payload = {"code": 0, "data": [{"countryId": 16, "minPrice": 0.09}]}
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(payload)):
            quote = client.get_country_quote("dr", "")
        self.assertEqual(0.09, quote["min_price"])

    def test_a_quote_without_a_price_is_reported_as_missing(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": None})):
            self.assertIsNone(client.get_country_quote("dr", "38"))


class PurchaseTests(unittest.TestCase):
    """``get_number`` quotes, checks the operator's price window, then buys."""

    def _quote_reply(self, min_price=0.05, country_id=38):
        return _reply({"code": 0, "data": {
            "countryId": country_id, "countryName": "Ghana", "phoneCode": "+233",
            "minPrice": min_price, "medianPrice": min_price, "maxPrice": min_price,
            "priceMap": {str(min_price): 3},
        }})

    def _order_reply(self, phones=("233555123456",), total=0.05):
        return _reply({"code": 0, "data": {
            "quantity": len(phones), "totalAmount": total, "phoneNumbers": list(phones),
        }})

    def test_a_purchase_buys_one_number_at_the_quoted_price(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply()), \
                patch("sms_tool.nexsms._requests.post", return_value=self._order_reply()) as post:
            activation = client.get_number(service="dr", country="38", max_price="0.06")
        self.assertIsInstance(activation, NexSmsActivation)
        self.assertEqual("+233555123456", activation.phone)
        self.assertEqual("0.05", activation.price)
        self.assertEqual("dr", activation.service)
        self.assertEqual("38", activation.country)
        self.assertEqual(
            {"serviceCode": "dr", "countryId": 38, "quantity": 1, "price": 0.05},
            post.call_args.kwargs["json"],
        )
        self.assertTrue(post.call_args[0][0].endswith("/api/order/purchase"))

    def test_the_activation_has_no_id_because_the_vendor_issues_none(self):
        """Pinned explicitly: the empty string is the contract, not an oversight.
        Every call site that branches on an activation id takes a different path
        for this vendor, and this is where that starts."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply()), \
                patch("sms_tool.nexsms._requests.post", return_value=self._order_reply()):
            activation = client.get_number(service="dr", country="38", max_price="0.06")
        self.assertEqual("", activation.activation_id)

    def test_a_number_is_returned_with_the_plus_prefix(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply()), \
                patch("sms_tool.nexsms._requests.post",
                      return_value=self._order_reply(phones=("254711408024",))):
            activation = client.get_number(service="dr", country="38", max_price="0.06")
        self.assertEqual("+254711408024", activation.phone)

    def test_a_price_above_the_configured_ceiling_is_refused_without_buying(self):
        """This vendor takes no price parameter, so the operator's ceiling has
        to be enforced here or it silently stops applying the moment they switch
        vendor."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply(min_price=0.20)), \
                patch("sms_tool.nexsms._requests.post") as post:
            with self.assertRaises(NexSmsError) as caught:
                client.get_number(service="dr", country="38", max_price="0.06")
        self.assertIn("max_price", str(caught.exception))
        self.assertTrue(caught.exception.retryable)
        post.assert_not_called()

    def test_a_price_below_the_configured_floor_is_refused_without_buying(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply(min_price=0.01)), \
                patch("sms_tool.nexsms._requests.post") as post:
            with self.assertRaises(NexSmsError) as caught:
                client.get_number(service="dr", country="38", min_price="0.05")
        self.assertIn("min_price", str(caught.exception))
        post.assert_not_called()

    def test_an_empty_ceiling_is_not_a_ceiling(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply(min_price=9.99)), \
                patch("sms_tool.nexsms._requests.post", return_value=self._order_reply(total=9.99)):
            activation = client.get_number(service="dr", country="38", max_price="")
        self.assertEqual("+233555123456", activation.phone)

    def test_a_provider_filter_is_reported_rather_than_dropped(self):
        """There is no operator dimension on this vendor, so a configured
        ``provider_ids`` cannot be honoured. Saying so is the difference between
        "this vendor ignores it" and "the setting silently stopped working"."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply()), \
                patch("sms_tool.nexsms._requests.post", return_value=self._order_reply()):
            with self.assertLogs("sms_tool.nexsms", level="INFO") as captured:
                client.get_number(service="dr", country="38", provider_ids="42")
        self.assertIn("provider_ids", "\n".join(captured.output))

    def test_a_country_without_a_quote_is_an_error(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=_reply({"code": 0, "data": None})):
            with self.assertRaises(NexSmsError) as caught:
                client.get_number(service="dr", country="38")
        self.assertIn("no quote", str(caught.exception))

    def test_a_purchase_without_a_phone_number_is_an_error(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get", return_value=self._quote_reply()), \
                patch("sms_tool.nexsms._requests.post", return_value=self._order_reply(phones=())):
            with self.assertRaises(NexSmsError) as caught:
                client.get_number(service="dr", country="38")
        self.assertIn("no phone number", str(caught.exception))


class LifecycleTests(unittest.TestCase):
    """Poll, complete, cancel -- keyed on the phone number throughout."""

    def test_get_status_asks_for_the_latest_message(self):
        client = NexSmsClient(api_key="k")
        payload = {"code": 0, "data": {"code": "654321", "text": "your code", "expiresTime": "t"}}
        with patch("sms_tool.nexsms._requests.get", return_value=_reply(payload)) as get:
            status = client.get_status("+233555123456")
        self.assertEqual("OK", status["status"])
        self.assertEqual("654321", status["code"])
        self.assertEqual("t", status["expires_time"])
        params = get.call_args.kwargs["params"]
        self.assertEqual("233555123456", params["phoneNumber"])
        self.assertEqual("json_latest", params["format"])

    def test_get_status_without_a_code_is_a_wait(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get",
                   return_value=_reply({"code": 0, "data": {"text": "no code yet"}})):
            self.assertEqual("WAIT_CODE", client.get_status("+233555123456")["status"])

    def test_get_status_without_a_handle_does_not_call_the_vendor(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.get") as get:
            self.assertEqual("WAIT_CODE", client.get_status("")["status"])
        get.assert_not_called()

    def test_wait_for_code_returns_the_first_code(self):
        client = NexSmsClient(api_key="k")
        with patch.object(client, "get_status", side_effect=[
            {"status": "WAIT_CODE"},
            {"status": "OK", "code": "111111"},
        ]), patch("sms_tool.nexsms.time.sleep"):
            self.assertEqual("111111", client.wait_for_code("+233555123456", timeout=30, poll_interval=1))

    def test_wait_for_code_skips_a_code_that_was_already_consumed(self):
        """Reuse depends on this: the same number serves several sends, so the
        previous round's code is still the vendor's "latest" until a new one
        lands. Returning it again would re-validate an old code."""
        client = NexSmsClient(api_key="k")
        with patch.object(client, "get_status", side_effect=[
            {"status": "OK", "code": "111111"},
            {"status": "OK", "code": "222222"},
        ]), patch("sms_tool.nexsms.time.sleep"):
            code = client.wait_for_code(
                "+233555123456", timeout=30, poll_interval=1, previous_code="111111"
            )
        self.assertEqual("222222", code)

    def test_wait_for_code_gives_up_on_timeout(self):
        client = NexSmsClient(api_key="k")
        with patch.object(client, "get_status", return_value={"status": "WAIT_CODE"}), \
                patch("sms_tool.nexsms.time.sleep"):
            self.assertIsNone(client.wait_for_code("+233555123456", timeout=0, poll_interval=1))

    def test_a_failing_poll_does_not_abort_the_wait(self):
        """A transient 5xx mid-wait must not throw away a number that is already
        paid for and may still deliver."""
        client = NexSmsClient(api_key="k")
        with patch.object(client, "get_status", side_effect=[
            NexSmsError("upstream hiccup"),
            {"status": "OK", "code": "999999"},
        ]), patch("sms_tool.nexsms.time.sleep"):
            self.assertEqual("999999", client.wait_for_code("+233555123456", timeout=30, poll_interval=1))

    def test_complete_is_a_no_op_that_reports_success(self):
        """No HTTP at all, and ``True``. ``False`` means "completion failed" to
        the caller, whose reaction is to cancel -- which would discard a number
        that had already delivered its code."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests") as transport:
            self.assertTrue(client.complete("+233555123456"))
        transport.get.assert_not_called()
        transport.post.assert_not_called()

    def test_request_additional_is_a_no_op_that_reports_ready(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests") as transport:
            self.assertTrue(client.request_additional("+233555123456"))
        transport.get.assert_not_called()
        transport.post.assert_not_called()

    def test_request_additional_without_a_handle_is_not_ready(self):
        client = NexSmsClient(api_key="k")
        self.assertFalse(client.request_additional(""))

    def test_cancel_posts_the_digits_only_number(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.post",
                   return_value=_reply({"code": 0, "data": True})) as post:
            self.assertTrue(client.cancel("+233555123456"))
        self.assertTrue(post.call_args[0][0].endswith("/api/close/activation"))
        self.assertEqual({"phoneNumber": "233555123456"}, post.call_args.kwargs["json"])

    def test_cancel_reports_failure_when_the_vendor_refuses(self):
        """Refusal is normal (the vendor requires the number to be two minutes
        old) and must not raise: the caller treats a cancel failure as
        non-blocking, and an exception would abort the whole round."""
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.post",
                   return_value=_reply({"code": 7, "message": "号码创建时间小于2分钟"})):
            self.assertFalse(client.cancel("+233555123456"))

    def test_cancel_reports_failure_on_a_transport_error(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.post", side_effect=requests.ConnectionError("boom")):
            self.assertFalse(client.cancel("+233555123456"))

    def test_cancel_without_a_handle_does_not_call_the_vendor(self):
        client = NexSmsClient(api_key="k")
        with patch("sms_tool.nexsms._requests.post") as post:
            self.assertFalse(client.cancel(""))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

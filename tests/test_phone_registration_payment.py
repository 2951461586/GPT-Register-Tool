import inspect
import unittest

from sms_tool import registration


class PhoneRegistrationPaymentTests(unittest.TestCase):
    def test_phone_registration_does_not_generate_payment_link(self):
        source = inspect.getsource(registration.run_phone_register)

        self.assertNotIn("_generate_payment_link", source)
        self.assertNotIn('"paypal":', source)


if __name__ == "__main__":
    unittest.main()

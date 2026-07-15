import inspect
import unittest

from sms_tool import registration


class PhoneRegistrationPaymentTests(unittest.TestCase):
    def test_phone_registration_uses_shared_payment_generator(self):
        source = inspect.getsource(registration.run_phone_register)

        self.assertIn("_generate_payment_link(", source)
        self.assertNotIn("generate_paypal_link", source)


if __name__ == "__main__":
    unittest.main()

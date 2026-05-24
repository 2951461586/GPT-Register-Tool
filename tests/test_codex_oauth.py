import unittest

from sms_tool import codex_oauth


class CodexOauthTests(unittest.TestCase):
    def test_account_deactivated_response_is_terminal(self):
        body = '{"error":{"code":"account_deactivated","message":"You do not have an account because it has been deleted or deactivated."}}'

        self.assertTrue(codex_oauth._is_account_deactivated_response(403, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(401, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(403, '{"error":"wrong code"}'))


if __name__ == "__main__":
    unittest.main()

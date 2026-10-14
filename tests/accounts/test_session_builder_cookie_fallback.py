"""session_token falls back to the chunked NextAuth cookie in cookie_header."""

from __future__ import annotations

import unittest

from sms_tool.session_builder import _session_token_from_cookie_header, build_session_file


class SessionTokenFromCookieTests(unittest.TestCase):
    def test_chunks_are_joined_in_numeric_order(self):
        header = (
            "a=1; __Secure-next-auth.session-token.10=C; __Secure-next-auth.session-token.2=B; "
            "__Secure-next-auth.session-token.0=A"
        )
        self.assertEqual(_session_token_from_cookie_header(header), "ABC")

    def test_unchunked_cookie_and_plain_name(self):
        self.assertEqual(_session_token_from_cookie_header("__Secure-next-auth.session-token=XYZ"), "XYZ")
        self.assertEqual(_session_token_from_cookie_header("next-auth.session-token=Q; b=2"), "Q")

    def test_other_cookies_and_empty_input(self):
        self.assertEqual(_session_token_from_cookie_header("__Host-next-auth.csrf-token=x"), "")
        self.assertEqual(_session_token_from_cookie_header(""), "")
        self.assertEqual(_session_token_from_cookie_header(None), "")

    def test_build_session_file_uses_cookie_only_as_a_fallback(self):
        cookie = "__Secure-next-auth.session-token.0=A; __Secure-next-auth.session-token.1=B"
        self.assertEqual(build_session_file({"cookie_header": cookie})["session_token"], "AB")
        explicit = build_session_file({"cookie_header": cookie, "session_token": "REAL"})
        self.assertEqual(explicit["session_token"], "REAL")
        from_body = build_session_file({"cookie_header": cookie, "auth_session": {"sessionToken": "BODY"}})
        self.assertEqual(from_body["session_token"], "BODY")


if __name__ == "__main__":
    unittest.main()
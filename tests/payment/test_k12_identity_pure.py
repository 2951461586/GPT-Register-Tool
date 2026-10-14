"""Behaviour tests for ``sms_tool/k12_identity.py`` (2026-09-03, round 7).

148 lines, **zero direct callers in the suite** (AST audit). Every function in
the module is pure -- dict/list/str juggling plus base64 and JSON -- so this is
testable with no network, no browser and no fixtures.

It is also the module that decides **which account a session belongs to**.
Getting ``account_id`` / ``user_id`` wrong does not crash anything; it silently
attributes one account's data to another. That is exactly the kind of bug a
test suite should make impossible, and exactly the kind that survives for years
without one.

What is pinned here:

* **Candidate precedence.** Both extractors are long ``or``-chains over a tuple
  of candidate sources. The first non-empty wins; reordering the tuple changes
  which value is reported. Every chain order below is asserted explicitly.
* **Falsy-but-valid values are dropped.** ``_get_nested`` ends with
  ``str(current or "").strip()``, so a legitimate ``0`` / ``False`` / ``""`` is
  reported as absent and the chain moves on.
* **JWT parsing never raises.** ``_jwt_claims`` swallows every exception and
  returns ``{}`` -- including when the payload is valid JSON but not an object.
"""
from __future__ import annotations

import base64
import json as _json
import unittest

from sms_tool.k12_identity import (
    _base64url_json,
    _extract_access_token,
    _extract_account_id_from_data,
    _extract_user_id_from_data,
    _get_nested,
    _jwt_claims,
    _token_account_id,
    _token_user_id,
)


def _jwt(payload) -> str:
    """Build a JWT-shaped token independently of the module under test.

    Deliberately NOT using ``_base64url_json`` here: a bug in the encoder would
    otherwise cancel itself out in the round-trip test.
    """
    raw = _json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "hdr." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") + ".sig"


AUTH = "https://api.openai.com/auth"
PROFILE = "https://api.openai.com/profile"


class GetNestedTests(unittest.TestCase):
    def test_walks_a_flat_path(self):
        self.assertEqual(_get_nested({"a": "x"}, ("a",)), "x")

    def test_walks_a_deep_path(self):
        self.assertEqual(_get_nested({"a": {"b": {"c": "x"}}}, ("a", "b", "c")), "x")

    def test_missing_key_gives_empty_string(self):
        self.assertEqual(_get_nested({"a": 1}, ("zzz",)), "")

    def test_non_dict_intermediate_gives_empty_string(self):
        self.assertEqual(_get_nested({"a": "not-a-dict"}, ("a", "b")), "")

    def test_list_intermediate_gives_empty_string(self):
        self.assertEqual(_get_nested({"a": [1, 2]}, ("a", "b")), "")

    def test_value_is_stringified_and_stripped(self):
        self.assertEqual(_get_nested({"a": "  x  "}, ("a",)), "x")
        self.assertEqual(_get_nested({"a": 42}, ("a",)), "42")

    def test_zero_and_false_are_reported_as_absent(self):
        """⚠️ Pinned: ``str(current or "")`` collapses every falsy value to ''."""
        for value in (0, False, "", None, []):
            with self.subTest(value=value):
                self.assertEqual(_get_nested({"a": value}, ("a",)), "")

    def test_empty_path_stringifies_the_whole_data(self):
        """⚠️ Pinned quirk: with no keys to walk, the container itself is returned."""
        self.assertEqual(_get_nested({"a": 1}, ()), "{'a': 1}")


class JwtClaimsTests(unittest.TestCase):
    def test_round_trip_through_the_module_encoder(self):
        payload = {"sub": "user-1", "exp": 123}
        self.assertEqual(_jwt_claims(_jwt(payload)), payload)

    def test_claims_are_read_from_the_second_segment(self):
        self.assertEqual(_jwt_claims("hdr." + _base64url_json({"a": 1}) + ".sig"), {"a": 1})

    def test_non_ascii_claims_survive(self):
        payload = {"name": "张三"}
        self.assertEqual(_jwt_claims(_jwt(payload)), payload)

    def test_padding_is_reconstructed(self):
        """Payloads whose length is not a multiple of 4 must still decode."""
        for payload in ({"a": 1}, {"ab": 22}, {"abc": 333}):
            with self.subTest(payload=payload):
                self.assertEqual(_jwt_claims(_jwt(payload)), payload)

    def test_two_segment_token_is_still_parsed(self):
        """⚠️ Pinned: only ``len(parts) < 2`` is rejected. A token with no
        signature segment still yields claims -- the header is never examined.

        Mutation K11 (tightening the guard to ``< 3``) silently killed this case,
        which is why the assertion exists: a provider that hands out unsigned
        two-part tokens would otherwise look like "no identity at all".
        """
        self.assertEqual(_jwt_claims("hdr." + _base64url_json({"sub": "s1"})), {"sub": "s1"})

    def test_fewer_than_two_segments_gives_empty_claims(self):
        for token in ("", "onlyheader", None):
            with self.subTest(token=token):
                self.assertEqual(_jwt_claims(token), {})

    def test_undecodable_payload_gives_empty_claims(self):
        self.assertEqual(_jwt_claims("hdr.!!!!not-base64!!!!.sig"), {})

    def test_payload_that_is_not_json_gives_empty_claims(self):
        self.assertEqual(_jwt_claims("hdr.aGVsbG8.sig"), {})  # "hello"

    def test_payload_that_is_json_but_not_an_object_gives_empty_claims(self):
        """⚠️ Pinned: a JWT whose payload is a JSON array is treated as no claims."""
        self.assertEqual(_jwt_claims(_jwt([1, 2, 3])), {})
        self.assertEqual(_jwt_claims(_jwt("just-a-string")), {})

    def test_non_string_tokens_give_empty_claims(self):
        for token in (None, 0, {}, []):
            with self.subTest(token=token):
                self.assertEqual(_jwt_claims(token), {})


class Base64UrlJsonTests(unittest.TestCase):
    def test_uses_compact_separators(self):
        """``separators=(",", ":")`` -- no spaces after ':' or ','."""
        encoded = _base64url_json({"a": 1, "b": 2})
        decoded = base64.urlsafe_b64decode(encoded + "==").decode("utf-8")
        self.assertEqual(decoded, '{"a":1,"b":2}')

    def test_padding_is_stripped(self):
        """⚠️ Pinned: the encoder strips '=' padding. Consumers must re-pad --
        ``_jwt_claims`` does exactly that at k12_identity.py:36, so round-trips
        stay consistent even though the intermediate form is unpadded."""
        self.assertNotIn("=", _base64url_json({"a": 1}))
        self.assertEqual(_base64url_json({"a": 1, "b": 2}),
                         base64.urlsafe_b64encode(b'{"a":1,"b":2}').decode().rstrip("="))

    def test_uses_the_urlsafe_alphabet(self):
        """Standard base64 would use '+' and '/' -- neither may appear."""
        for payload in ({"a": "~~~"}, {"b": "???"}, {"c": "\xfb\xff"}):
            with self.subTest(payload=payload):
                encoded = _base64url_json(payload)
                self.assertNotIn("+", encoded)
                self.assertNotIn("/", encoded)

    def test_non_ascii_is_utf8_encoded_not_escaped(self):
        """ensure_ascii=False keeps CJK as raw UTF-8 bytes rather than \\uXXXX."""
        encoded = _base64url_json({"name": "张三"})
        decoded = base64.urlsafe_b64decode(encoded + "==").decode("utf-8")
        self.assertIn("张三", decoded)


class TokenAccountIdTests(unittest.TestCase):
    def test_reads_the_namespaced_auth_claim(self):
        self.assertEqual(
            _token_account_id(_jwt({AUTH: {"chatgpt_account_id": "acct-1"}})),
            "acct-1",
        )

    def test_falls_back_to_the_top_level_claim(self):
        self.assertEqual(_token_account_id(_jwt({"chatgpt_account_id": "acct-2"})), "acct-2")

    def test_namespaced_claim_wins_over_the_top_level_one(self):
        token = _jwt({AUTH: {"chatgpt_account_id": "named"}, "chatgpt_account_id": "flat"})
        self.assertEqual(_token_account_id(token), "named")

    def test_non_dict_auth_claim_is_ignored(self):
        self.assertEqual(_token_account_id(_jwt({AUTH: "not-a-dict"})), "")
        self.assertEqual(
            _token_account_id(_jwt({AUTH: "not-a-dict", "chatgpt_account_id": "flat"})),
            "flat",
        )

    def test_unparseable_token_gives_empty_string(self):
        self.assertEqual(_token_account_id("garbage"), "")

    def test_value_is_stripped(self):
        self.assertEqual(_token_account_id(_jwt({"chatgpt_account_id": "  acct-3  "})), "acct-3")


class TokenUserIdTests(unittest.TestCase):
    def test_profile_user_id_wins(self):
        token = _jwt({PROFILE: {"user_id": "p1", "id": "p2"}, AUTH: {"user_id": "a1"},
                      "user_id": "t1", "sub": "s1"})
        self.assertEqual(_token_user_id(token), "p1")

    def test_profile_id_is_the_second_choice(self):
        token = _jwt({PROFILE: {"id": "p2"}, AUTH: {"user_id": "a1"}, "sub": "s1"})
        self.assertEqual(_token_user_id(token), "p2")

    def test_auth_user_id_is_the_third_choice(self):
        token = _jwt({AUTH: {"user_id": "a1"}, "sub": "s1"})
        self.assertEqual(_token_user_id(token), "a1")

    def test_auth_userid_is_the_fourth_choice(self):
        self.assertEqual(_token_user_id(_jwt({AUTH: {"userId": "a2"}, "sub": "s1"})), "a2")

    def test_top_level_user_id_then_userid_then_sub(self):
        self.assertEqual(_token_user_id(_jwt({"user_id": "t1", "sub": "s1"})), "t1")
        self.assertEqual(_token_user_id(_jwt({"userId": "t2", "sub": "s1"})), "t2")
        self.assertEqual(_token_user_id(_jwt({"sub": "s1"})), "s1")

    def test_auth_user_id_beats_userid(self):
        """The two auth spellings are adjacent in the chain -- both must win in order."""
        self.assertEqual(_token_user_id(_jwt({AUTH: {"user_id": "a1", "userId": "a2"}})), "a1")

    def test_top_level_user_id_beats_userid(self):
        self.assertEqual(_token_user_id(_jwt({"user_id": "t1", "userId": "t2"})), "t1")

    def test_auth_namespace_beats_the_top_level_claims(self):
        """Every auth source outranks every bare claim, including ``sub``."""
        token = _jwt({AUTH: {"userId": "a2"}, "user_id": "t1", "sub": "s1"})
        self.assertEqual(_token_user_id(token), "a2")

    def test_no_identity_claims_gives_empty_string(self):
        self.assertEqual(_token_user_id(_jwt({"exp": 1})), "")

    def test_empty_string_claims_fall_through_to_the_next_source(self):
        """⚠️ The chain is ``or``-based, so '' is treated as absent, not as a value."""
        token = _jwt({PROFILE: {"user_id": "", "id": "p2"}, "sub": "s1"})
        self.assertEqual(_token_user_id(token), "p2")

    def test_non_dict_namespaces_are_ignored(self):
        self.assertEqual(_token_user_id(_jwt({PROFILE: "x", AUTH: "y", "sub": "s1"})), "s1")


class ExtractAccessTokenTests(unittest.TestCase):
    def test_non_dict_input_gives_empty_string(self):
        for data in (None, "x", [], 0):
            with self.subTest(data=data):
                self.assertEqual(_extract_access_token(data), "")

    def test_top_level_access_token_wins(self):
        self.assertEqual(_extract_access_token({"access_token": "T1"}), "T1")

    def test_access_token_beats_accesstoken(self):
        data = {"access_token": "snake", "accessToken": "camel"}
        self.assertEqual(_extract_access_token(data), "snake")

    def test_nested_tokens_are_consulted_in_order(self):
        cases = [
            ({"tokens": {"access_token": "T"}}, "T"),
            ({"tokens": {"accessToken": "T"}}, "T"),
            ({"token": {"access_token": "T"}}, "T"),
            ({"token": {"accessToken": "T"}}, "T"),
            ({"credentials": {"access_token": "T"}}, "T"),
            ({"credentials": {"accessToken": "T"}}, "T"),
            ({"auth_session": {"accessToken": "T"}}, "T"),
            ({"auth_session": {"access_token": "T"}}, "T"),
            ({"auth_session": {"session": {"accessToken": "T"}}}, "T"),
            ({"auth_session": {"session": {"access_token": "T"}}}, "T"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(_extract_access_token(data), expected)

    def test_top_level_beats_nested(self):
        data = {"access_token": "top", "tokens": {"access_token": "nested"},
                "auth_session": {"session": {"access_token": "deep"}}}
        self.assertEqual(_extract_access_token(data), "top")

    def test_auth_session_is_skipped_when_it_is_not_a_dict(self):
        data = {"auth_session": "not-a-dict", "tokens": {"access_token": "T"}}
        self.assertEqual(_extract_access_token(data), "T")

    def test_all_empty_gives_empty_string(self):
        self.assertEqual(_extract_access_token({"access_token": "", "tokens": {}}), "")

    def test_value_is_stripped(self):
        self.assertEqual(_extract_access_token({"access_token": "  T  "}), "T")


class ExtractUserIdFromDataTests(unittest.TestCase):
    def test_non_dict_input_gives_empty_string(self):
        self.assertEqual(_extract_user_id_from_data("nope"), "")

    def test_candidate_sources_are_all_recognised(self):
        cases = [
            ({"user_id": "U"}, "U"),
            ({"userId": "U"}, "U"),
            ({"chatgpt_user_id": "U"}, "U"),
            ({"chatgptUserId": "U"}, "U"),
            ({"user": {"id": "U"}}, "U"),
            ({"account": {"user_id": "U"}}, "U"),
            ({"account": {"userId": "U"}}, "U"),
            ({"tokens": {"user_id": "U"}}, "U"),
            ({"tokens": {"chatgpt_user_id": "U"}}, "U"),
            ({"credentials": {"user_id": "U"}}, "U"),
            ({"providerSpecificData": {"chatgpt_user_id": "U"}}, "U"),
            ({"providerSpecificData": {"chatgptUserId": "U"}}, "U"),
            ({"auth_session": {"user": {"id": "U"}}}, "U"),
            ({"auth_session": {"session": {"user": {"id": "U"}}}}, "U"),
            ({"codex_session": {"user": {"id": "U"}}}, "U"),
            ({"session": {"user": {"id": "U"}}}, "U"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(_extract_user_id_from_data(data), expected)

    def test_user_id_beats_userid(self):
        self.assertEqual(_extract_user_id_from_data({"user_id": "a", "userId": "b"}), "a")

    def test_data_level_candidates_beat_the_jwt_fallback(self):
        data = {"user_id": "from-data", "access_token": _jwt({"sub": "from-jwt"})}
        self.assertEqual(_extract_user_id_from_data(data), "from-data")

    def test_jwt_fallback_uses_id_token_first(self):
        data = {"id_token": _jwt({"sub": "idtok"}),
                "access_token": _jwt({"sub": "acctok"})}
        self.assertEqual(_extract_user_id_from_data(data), "idtok")

    def test_jwt_fallback_falls_through_to_access_token(self):
        data = {"access_token": _jwt({"sub": "acctok"}),
                "accessToken": _jwt({"sub": "cameltok"})}
        self.assertEqual(_extract_user_id_from_data(data), "acctok")

    def test_jwt_fallback_uses_accessToken_last(self):
        self.assertEqual(
            _extract_user_id_from_data({"accessToken": _jwt({"sub": "cameltok"})}),
            "cameltok",
        )

    def test_unreadable_tokens_fall_through_to_empty(self):
        self.assertEqual(_extract_user_id_from_data({"id_token": "junk", "access_token": "junk"}), "")

    def test_empty_payload_gives_empty_string(self):
        self.assertEqual(_extract_user_id_from_data({}), "")


class ExtractAccountIdFromDataTests(unittest.TestCase):
    def test_non_dict_input_gives_empty_string(self):
        self.assertEqual(_extract_account_id_from_data(None), "")

    def test_candidate_sources_are_all_recognised(self):
        cases = [
            ({"account_id": "A"}, "A"),
            ({"accountId": "A"}, "A"),
            ({"chatgpt_account_id": "A"}, "A"),
            ({"chatgptAccountId": "A"}, "A"),
            ({"workspace_id": "A"}, "A"),
            ({"workspaceId": "A"}, "A"),
            ({"k12_workspace_id": "A"}, "A"),
            ({"account": {"id": "A"}}, "A"),
            ({"tokens": {"account_id": "A"}}, "A"),
            ({"tokens": {"accountId": "A"}}, "A"),
            ({"tokens": {"chatgpt_account_id": "A"}}, "A"),
            ({"tokens": {"chatgptAccountId": "A"}}, "A"),
            ({"credentials": {"account_id": "A"}}, "A"),
            ({"credentials": {"chatgpt_account_id": "A"}}, "A"),
            ({"providerSpecificData": {"chatgpt_account_id": "A"}}, "A"),
            ({"providerSpecificData": {"chatgptAccountId": "A"}}, "A"),
            ({"auth_session": {"account": {"id": "A"}}}, "A"),
            ({"auth_session": {"session": {"account": {"id": "A"}}}}, "A"),
            ({"codex_session": {"account": {"id": "A"}}}, "A"),
            ({"session": {"account": {"id": "A"}}}, "A"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(_extract_account_id_from_data(data), expected)

    def test_account_id_beats_workspace_id(self):
        data = {"account_id": "acct", "workspace_id": "ws"}
        self.assertEqual(_extract_account_id_from_data(data), "acct")

    def test_data_level_candidates_beat_the_jwt_fallback(self):
        data = {"account_id": "from-data", "access_token": _jwt({AUTH: {"chatgpt_account_id": "from-jwt"}})}
        self.assertEqual(_extract_account_id_from_data(data), "from-data")

    def test_jwt_fallback_order(self):
        data = {"id_token": _jwt({AUTH: {"chatgpt_account_id": "idtok"}}),
                "access_token": _jwt({AUTH: {"chatgpt_account_id": "acctok"}})}
        self.assertEqual(_extract_account_id_from_data(data), "idtok")
        self.assertEqual(
            _extract_account_id_from_data(
                {"accessToken": _jwt({AUTH: {"chatgpt_account_id": "cameltok"}})}
            ),
            "cameltok",
        )

    def test_unreadable_tokens_fall_through_to_empty(self):
        """An unreadable id_token must not mask a readable access_token."""
        self.assertEqual(_extract_account_id_from_data({"id_token": "junk"}), "")
        data = {"id_token": "junk", "access_token": _jwt({AUTH: {"chatgpt_account_id": "acctok"}})}
        self.assertEqual(_extract_account_id_from_data(data), "acctok")

    def test_empty_payload_gives_empty_string(self):
        self.assertEqual(_extract_account_id_from_data({}), "")


if __name__ == "__main__":
    unittest.main()

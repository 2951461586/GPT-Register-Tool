"""``sms_tool/token_telemetry.py`` 的行为测试（零覆盖 → 全覆盖 + 变异验证）。

为什么值得测：``access_token_telemetry(...)["token_hash"]`` 是 **令牌是否被真正轮换**
的判据。``payment_auth.py:48`` 先取一次 ``original_hash``，最多 3 次稳定探测，
401 后重新登录再取一次 —— **只有这个哈希变了，运维才知道令牌真的换了**。
哈希算错对象（比如算了未 strip 的原文）或算错长度，轮换检测会静默失效：
令牌其实还在复用旧的那把，界面上却显示"已重新登录"。

另外三个字段（``iat`` / ``exp`` / ``age_seconds``）是登录态健康度的唯一来源，
``expires_in_seconds`` 的**负号**是"已过期"的唯一信号 —— 详见
``test_expires_in_is_negative_for_an_already_expired_token``。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import unittest
from unittest import mock

from sms_tool import token_telemetry
from sms_tool.token_telemetry import access_token_telemetry

NOW = 1_700_000_500

# 写死的哈希常量。绝不能在测试里用 hashlib 复算 —— 那样 `[:16]` 被改成 `[:8]`
# 或者算法被换掉都抓不到。
SHA256_ABC_16 = "ba7816bf8f01cfea"
SHA256_TOK_16 = "1a7674eb4ee78df7"

# urlsafe 字母表：A-Z a-z 0-9 '-'(62) '_'(63)。
# 可打印 ASCII 载荷里，只有落在三字节组第 3 位的 '>'/'~'(0x3E/0x7E) 能产出 62，
# '?'(0x3F) 能产出 63 —— 所以这个载荷是特意搜出来的，别随手改。
URLSAFE_PAYLOAD_B64 = (
    "eyJpYXQiOiAxNzAwMDAwMDAwLCAiZXhwIjogMTcwMDAwMzYwMCwgImoiOiAi"
    "YmFhMzNiMmNiMzM_PjI_YWE-PjNiPzE_In0"
)

# 长度 %4 == 2 / 3 的未补齐载荷，用来钉住 `"=" * (-len(parts[1]) % 4)`
PAYLOAD_MOD4_2 = "eyJpYXQiOiAxNzAwMDAwMDAwLCAiZXhwIjogMTcwMDAwMzYwMCwgImoiOiAiYjMifQ"
PAYLOAD_MOD4_3 = "eyJpYXQiOiAxNzAwMDAwMDAwLCAiZXhwIjogMTcwMDAwMzYwMCwgImoiOiAiMzFiIn0"

NON_DICT_PAYLOADS = {
    "list": "WzEsIDIsIDNd",
    "number": "MTIzNDU",
    "string": "ImhlbGxvIg",
    "null": "bnVsbA",
    "bool": "dHJ1ZQ",
}

EXPECTED_KEYS = {
    "token_hash",
    "iat",
    "exp",
    "lifetime_seconds",
    "age_seconds",
    "expires_in_seconds",
}


def _b64(raw: bytes) -> str:
    """按 JWT 习惯编码：urlsafe 字母表 + 去掉尾部 '='。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _json_b64(obj) -> str:
    return _b64(json.dumps(obj).encode("utf-8"))


def _jwt(payload, header: str = "eyJhbGciOiJIUzI1NiJ9", sig: str = "sig") -> str:
    return f"{header}.{_json_b64(payload)}.{sig}"


@contextlib.contextmanager
def _frozen(ts):
    """钉住 ``time.time()``。模块里是 ``import time``，所以打的是同一个模块对象。"""
    with mock.patch.object(token_telemetry.time, "time", return_value=ts):
        yield


class TokenHashTest(unittest.TestCase):
    """``token_hash`` = 轮换检测的判据，算错对象就白搭。"""

    def test_it_is_the_first_16_hex_chars_of_sha256(self):
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry("abc")["token_hash"], SHA256_ABC_16)
        self.assertEqual(hashlib.sha256(b"abc").hexdigest()[:16], SHA256_ABC_16)

    def test_it_is_taken_over_the_stripped_text(self):
        """⚠️ 令牌常从文件/JSON 里带换行读出来。去掉 ``.strip()`` 会让同一把令牌
        在"带换行"和"不带换行"两种读法下算出两个哈希 —— 轮换检测直接误报。"""
        with _frozen(NOW):
            stripped = access_token_telemetry("tok")["token_hash"]
            padded = access_token_telemetry("  tok \n")["token_hash"]
        self.assertEqual(padded, stripped)
        self.assertEqual(stripped, SHA256_TOK_16)

    def test_an_empty_token_yields_an_empty_string_not_the_hash_of_empty(self):
        """判据是 ``if text else ""`` —— 空令牌必须是空串，不是 ``sha256(b"")``。"""
        with _frozen(NOW):
            for value in ("", None, "   ", "\n"):
                with self.subTest(value=value):
                    self.assertEqual(access_token_telemetry(value)["token_hash"], "")
        self.assertNotEqual(
            "", hashlib.sha256(b"").hexdigest()[:16],
            "空串哈希是个真实存在的值，两者必须能区分开",
        )

    def test_a_missing_token_is_not_hashed_as_the_literal_none(self):
        """⚠️ ``str(token or "")`` 里的 ``or ""`` 是**有语义的**（和本仓其它模块的
        "假值归一化纯装饰"不同）：``None`` 令牌如果直接 ``str()`` 会得到 ``"None"``，
        一个所有失败提取都会撞在一起的哈希。"""
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(None)["token_hash"], "")
            self.assertNotEqual(
                access_token_telemetry(None)["token_hash"],
                hashlib.sha256(b"None").hexdigest()[:16],
            )

    def test_adjacent_tokens_get_different_hashes(self):
        with _frozen(NOW):
            hashes = {access_token_telemetry(f"token-{i}")["token_hash"] for i in range(50)}
        self.assertEqual(len(hashes), 50)
        for value in hashes:
            self.assertEqual(len(value), 16)


class PayloadDecodingTest(unittest.TestCase):
    """只解第二段；解不出来一律降级成空负载，绝不抛。"""

    def test_a_standard_three_part_jwt_is_decoded(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": 1700000000, "exp": 1700003600}))
        self.assertEqual(info["iat"], 1700000000)
        self.assertEqual(info["exp"], 1700003600)

    def test_only_the_second_segment_is_decoded(self):
        """头部段就算是个合法 JSON 字典也必须被忽略 —— 否则攻击者可控的 header
        （JWT 头部本来就未签名）能伪造 ``iat``/``exp``。"""
        fake_header = _json_b64({"iat": 999, "exp": 999})
        token = f"{fake_header}.{_json_b64({'iat': 111, 'exp': 222})}.sig"
        with _frozen(NOW):
            info = access_token_telemetry(token)
        self.assertEqual(info["iat"], 111)
        self.assertEqual(info["exp"], 222)

    def test_the_signature_segment_is_never_read(self):
        token = f"hdr.{_json_b64({'iat': 111, 'exp': 222})}.{_json_b64({'iat': 9, 'exp': 9})}"
        with _frozen(NOW):
            info = access_token_telemetry(token)
        self.assertEqual(info["iat"], 111)

    def test_a_two_part_token_with_no_signature_is_decoded(self):
        token = f"hdr.{_json_b64({'iat': 111, 'exp': 222})}"
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(token)["iat"], 111)

    def test_a_token_without_a_dot_yields_no_timing(self):
        with _frozen(NOW):
            info = access_token_telemetry("opaque-token")
        self.assertEqual(info["iat"], 0)
        self.assertEqual(info["exp"], 0)

    def test_a_four_part_token_still_reads_the_second_segment(self):
        token = f"a.{_json_b64({'iat': 111, 'exp': 222})}.c.d"
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(token)["iat"], 111)

    def test_unpadded_payloads_are_accepted(self):
        """JWT 惯例是不带 '=' 的；``%4`` 余 2 和余 3 两种情况都要补齐。"""
        for label, payload in (("mod4=2", PAYLOAD_MOD4_2), ("mod4=3", PAYLOAD_MOD4_3)):
            with self.subTest(label=label):
                self.assertEqual(len(payload) % 4 in (2, 3), True)
                with _frozen(NOW):
                    info = access_token_telemetry(f"hdr.{payload}.sig")
                self.assertEqual(info["iat"], 1700000000)
                self.assertEqual(info["exp"], 1700003600)

    def test_an_already_padded_payload_is_accepted(self):
        padded = PAYLOAD_MOD4_2 + "=="   # 手工补成 %4 == 0
        self.assertEqual(len(padded) % 4, 0)
        with _frozen(NOW):
            info = access_token_telemetry(f"hdr.{padded}.sig")
        self.assertEqual(info["iat"], 1700000000)
        self.assertEqual(info["exp"], 1700003600)

    def test_the_urlsafe_alphabet_is_used_not_the_standard_one(self):
        """载荷里含 '-' 和 '_'。换成 ``base64.b64decode`` 会抛 binascii.Error
        （是 ValueError 的子类）→ 被吞掉 → ``iat``/``exp`` 变 0。"""
        self.assertIn("-", URLSAFE_PAYLOAD_B64)
        self.assertIn("_", URLSAFE_PAYLOAD_B64)
        with _frozen(NOW):
            info = access_token_telemetry(f"hdr.{URLSAFE_PAYLOAD_B64}.sig")
        self.assertEqual(info["iat"], 1700000000)
        self.assertEqual(info["exp"], 1700003600)

    def test_a_payload_without_timing_claims_is_safe(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"sub": "user-1"}))
        self.assertEqual(info["iat"], 0)
        self.assertEqual(info["exp"], 0)

    def test_a_payload_that_is_not_a_dict_is_ignored(self):
        """``isinstance(parsed, dict)`` 是 ``payload.get(...)`` 的前置保险 ——
        JSON 数字/数组/字符串都解得出对象，但都没有 ``.get``。"""
        for label, payload in NON_DICT_PAYLOADS.items():
            with self.subTest(label=label):
                with _frozen(NOW):
                    info = access_token_telemetry(f"hdr.{payload}.sig")
                self.assertEqual(info["iat"], 0)
                self.assertEqual(info["exp"], 0)

    def test_malformed_payloads_never_raise(self):
        cases = {
            "not base64": "@@@@",
            "base64 but not json": _b64(b"not json at all"),
            "empty segment": "",
            "single char": "A",
            "truncated": _json_b64({"iat": 111, "exp": 222})[:6],
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                with _frozen(NOW):
                    info = access_token_telemetry(f"hdr.{payload}.sig")
                self.assertEqual(info["iat"], 0)
                self.assertEqual(info["exp"], 0)

    def test_a_payload_that_is_not_valid_utf8_never_raises(self):
        """``json.loads`` 的 UnicodeDecodeError 是 UnicodeError 的子类，被 except 接住。"""
        payload = _b64(b"\xff\xfe\xff")  # 合法 base64，但解出来不是 UTF-8
        with _frozen(NOW):
            info = access_token_telemetry(f"hdr.{payload}.sig")
        self.assertEqual(info["iat"], 0)

    def test_a_non_ascii_second_segment_never_raises(self):
        """``.encode("ascii")`` 的 UnicodeEncodeError 同样是 UnicodeError 的子类。"""
        with _frozen(NOW):
            info = access_token_telemetry("hdr.ééé.sig")
        self.assertEqual(info["iat"], 0)
        self.assertNotEqual(info["token_hash"], "")


class IntCoercionTest(unittest.TestCase):
    """``_as_int``：解不出来的东西一律当 0，绝不抛。"""

    def test_string_integers_are_coerced(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": "1700000000", "exp": "1700003600"}))
        self.assertEqual(info["iat"], 1700000000)
        self.assertEqual(info["exp"], 1700003600)

    def test_floats_are_truncated_toward_zero(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": 1700000000.9, "exp": 1700003600.2}))
        self.assertEqual(info["iat"], 1700000000)
        self.assertEqual(info["exp"], 1700003600)

    def test_negative_floats_truncate_toward_zero(self):
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(_jwt({"iat": -1.9}))["iat"], -1)

    def test_booleans_coerce_to_int(self):
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(_jwt({"iat": True}))["iat"], 1)
            self.assertEqual(access_token_telemetry(_jwt({"iat": False}))["iat"], 0)

    def test_uncoercible_values_become_zero(self):
        for label, value in {
            "text": "abc",
            "dotted text": "1.5.5",
            "nested list": [[1]],
            "non-empty dict": {"a": 1},
        }.items():
            with self.subTest(label=label):
                with _frozen(NOW):
                    self.assertEqual(access_token_telemetry(_jwt({"iat": value}))["iat"], 0)

    def test_falsy_values_become_zero(self):
        for label, value in {
            "none": None,
            "empty str": "",
            "zero": 0,
            "empty list": [],
            "empty dict": {},
        }.items():
            with self.subTest(label=label):
                with _frozen(NOW):
                    self.assertEqual(access_token_telemetry(_jwt({"iat": value}))["iat"], 0)


class TimingMathTest(unittest.TestCase):
    """三个时间字段各有**两层**独立的下界保护，别当成冗余防御。"""

    def test_lifetime_is_exp_minus_iat(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": 1700000000, "exp": 1700003600}))
        self.assertEqual(info["lifetime_seconds"], 3600)

    def test_lifetime_is_zero_when_either_bound_is_missing(self):
        """``if iat and exp`` 这一层：缺一边时不该拿 0 去减出一个假的寿命。"""
        for label, payload in {
            "no iat": {"exp": 1700003600},
            "no exp": {"iat": 1700000000},
            "neither": {"sub": "x"},
        }.items():
            with self.subTest(label=label):
                with _frozen(NOW):
                    self.assertEqual(
                        access_token_telemetry(_jwt(payload))["lifetime_seconds"], 0)

    def test_lifetime_is_clamped_at_zero_when_exp_precedes_iat(self):
        """``max(0, ...)`` 这一层：``iat``/``exp`` 都非零但顺序反了。"""
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": 1700003600, "exp": 1700000000}))
        self.assertEqual(info["lifetime_seconds"], 0)

    def test_age_uses_acquired_at_when_provided(self):
        with _frozen(NOW):
            info = access_token_telemetry(
                _jwt({"iat": 1700000000, "exp": 1700003600}), acquired_at=NOW - 120)
        self.assertEqual(info["age_seconds"], 120)

    def test_age_falls_back_to_iat_when_acquired_at_is_absent(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": NOW - 300, "exp": NOW + 3000}))
        self.assertEqual(info["age_seconds"], 300)

    def test_age_falls_back_to_iat_when_acquired_at_is_uncoercible(self):
        with _frozen(NOW):
            info = access_token_telemetry(
                _jwt({"iat": NOW - 300, "exp": NOW + 3000}), acquired_at="nonsense")
        self.assertEqual(info["age_seconds"], 300)

    def test_age_is_zero_without_any_time_reference(self):
        """``if acquired`` 这一层：没有任何参照时不该拿 ``now`` 当年龄。"""
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"sub": "x"}))
        self.assertEqual(info["age_seconds"], 0)

    def test_age_is_clamped_at_zero_for_a_future_acquisition(self):
        """``max(0, ...)`` 这一层：客户端时钟超前时年龄不该是负的。"""
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"exp": NOW + 3000}), acquired_at=NOW + 500)
        self.assertEqual(info["age_seconds"], 0)

    def test_expires_in_is_exp_minus_now(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"exp": NOW + 60}))
        self.assertEqual(info["expires_in_seconds"], 60)

    def test_expires_in_is_negative_for_an_already_expired_token(self):
        """🔴 故意不夹 0 —— 和 ``lifetime_seconds`` / ``age_seconds`` 的做法相反。
        **负值就是"已过期"的唯一信号**，调用方（``registration_handlers.py:1031``、
        ``session_builder.py:131``）靠它判断要不要重新登录。谁给它加 ``max(0, ...)``，
        过期令牌就会被当成"还剩 0 秒"，续期逻辑整个停摆。"""
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"exp": NOW - 90}))
        self.assertEqual(info["expires_in_seconds"], -90)

    def test_expires_in_is_zero_without_an_exp_claim(self):
        with _frozen(NOW):
            self.assertEqual(access_token_telemetry(_jwt({"iat": 1}))["expires_in_seconds"], 0)

    def test_a_fractional_clock_is_truncated(self):
        """``now = int(time.time())`` —— 时间戳必须是整数秒。"""
        with _frozen(NOW + 0.9):
            info = access_token_telemetry(_jwt({"exp": NOW + 60}))
        self.assertEqual(info["expires_in_seconds"], 60)
        self.assertIsInstance(info["expires_in_seconds"], int)


class ReturnShapeTest(unittest.TestCase):
    """返回值的形状是对调用方的契约。"""

    def test_the_key_set_is_exactly_these_six_keys(self):
        with _frozen(NOW):
            self.assertEqual(set(access_token_telemetry(_jwt({"iat": 1, "exp": 2}))),
                             EXPECTED_KEYS)

    def test_the_shape_is_stable_for_an_unparseable_token(self):
        with _frozen(NOW):
            self.assertEqual(set(access_token_telemetry("garbage")), EXPECTED_KEYS)

    def test_every_value_is_scalar(self):
        with _frozen(NOW):
            info = access_token_telemetry(_jwt({"iat": 1, "exp": 2}))
        self.assertIsInstance(info["token_hash"], str)
        for key in ("iat", "exp", "lifetime_seconds", "age_seconds", "expires_in_seconds"):
            with self.subTest(key=key):
                self.assertIsInstance(info[key], int)

    def test_acquired_at_is_keyword_only(self):
        with self.assertRaises(TypeError):
            access_token_telemetry(_jwt({"iat": 1}), 5)

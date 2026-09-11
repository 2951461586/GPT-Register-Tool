"""`/api/auth/session` 就绪轮询的可观测性。

2026-09-12 真机验证里出现一个此前无法归因的失败：账号**已经创建成功**（第 7 步），
但第 8 步拿不到 access token，报 `missing_auth_session_access_token`，账号直接报废。
日志里只有四行一模一样的 `Auth session: 200`：

    528:  Auth session: 200
    529:  Auth session: 200 attempt=2
    530:  Auth session: 200 attempt=3
    531:  Auth session: 200 attempt=4

四种完全不同的原因会产出这四行，且**无法区分**：

1. 响应体不是 JSON（Cloudflare 拦截页 / 空体）；
2. 是 JSON，但 session 尚未传播，确实还没有 `accessToken`；
3. **token 存在，但停在一个 `_auth_session_access_token` 没走的路径上** —— 这是提取器 bug；
4. 请求本身失败（状态码非 200）。

而且那行是 `print`，**在生产里根本不落盘**（文件 handler 只收 logging record）。

所以这里断言三件事：`auth_session_readiness` 记录必须出现且带足判别字段、
它**不得**把 token 的值写进日志、以及它写出来的字段**必须活过脱敏层**。

2026-09-12 追加的两个发现（都由变异验证逼出来，见 `audit-playbook.md` ⑭）：

* **字段名是功能性选择，不是风格问题。** 原名 `has_access_token` 命中
  `sensitive_policy.json` 的 `named_secret` 规则（`access_token` + `=`），
  `sanitize_log_text` 把 True/False **双双**改写成 `[REDACTED]`；`.jsonl` 通道
  同理（键名含 `token`/`cookie` 且不以 `_present`/`_count` 结尾即整值替换）。
  改名为 `access_token_present`（落在既有 `safe_key_suffixes` 约定内）后存活。
  ⇒ 断言必须打在 `sanitize_log_text(message)` / `sanitize(value, key=字段名)`
  这一层，只断言脱敏**前**的 `getMessage()` 会给出假绿。
* **`next-auth.state` 不是 `next-auth.session-token`。** 前者是 OAuth state，
  对"回调 200 但 session 匿名"完全没有读数；后者才是决定 `/api/auth/session`
  是否认证的那个 cookie。失败账号的 `Create account continue: 200 https://chatgpt.com/`
  说明重定向链走完了 —— 所以要看的就是这个 cookie 在不在。
* **探针必须与提取器同判据。** 匿名 session 会回 `{"accessToken": null}`，
  只看"键存在"会报 `token_key_present=True`，把"未认证"误诊成"提取器漏路径"。
"""
from __future__ import annotations

import logging
import unittest
from unittest import mock

from sms_tool.accounts import account_creation

# 派生而非硬编码：logging 自己知道哪些属性是标准的，升级 Python 也不会漏。
_STANDARD_LOG_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)
_EXPECTED_READINESS_FIELDS = frozenset(
    {
        "event",
        "status_code",
        "attempt",
        "max_attempts",
        "access_token_present",
        "token_key_present",
        "body_shape",
        "nextauth_session",
        "cookie_count",
        "jar_presence",
    }
)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Cookie:
    def __init__(self, name, value="v"):
        self.name = name
        self.value = value


class _Session:
    """Minimal cookie jar -- `_cookie_presence` only iterates `session.cookies`."""

    def __init__(self, *names):
        self.cookies = [_Cookie(name) for name in names]


class _RecordingHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


class SessionBodyShapeTests(unittest.TestCase):
    """`_session_body_shape` 必须把「非 JSON」和「JSON 无 AT」分开。"""

    def test_non_json_body_is_reported_as_raw(self):
        # len("<html>challenge</html>") == 22 —— 别手算，用 len() 锚住格式契约。
        raw = "<html>challenge</html>"
        self.assertEqual(
            account_creation._session_body_shape({"_raw": raw}),
            f"non_json raw_len={len(raw)}",
        )

    def test_json_body_reports_its_key_names(self):
        shape = account_creation._session_body_shape({"user": {}, "expires": "x"})
        self.assertEqual(shape, "json keys=['expires', 'user']")

    def test_non_dict_body_is_labelled_by_type(self):
        self.assertEqual(account_creation._session_body_shape("oops"), "type=str")

    def test_shape_never_includes_values(self):
        """键名可以进日志，值不行 —— 值里可能就是 token。"""
        secret = "eyJhbGciOiJSUzI1NiIsImtpZCI6InNlY3JldC10b2tlbi12YWx1ZSJ9"
        shape = account_creation._session_body_shape({"accessToken": secret})
        self.assertNotIn(secret, shape)
        self.assertNotIn("eyJ", shape)


class ContainsAccessTokenKeyTests(unittest.TestCase):
    """提取器漏路径时，`token_key_present=True` + `access_token_present=False` 会暴露它。"""

    def test_finds_a_nested_access_token(self):
        body = {"session": {"nested": {"access_token": "x"}}}
        self.assertTrue(account_creation._contains_access_token_key(body))

    def test_finds_it_inside_a_list(self):
        body = {"sessions": [{"accessToken": "x"}]}
        self.assertTrue(account_creation._contains_access_token_key(body))

    def test_false_when_there_is_genuinely_no_token(self):
        self.assertFalse(
            account_creation._contains_access_token_key({"user": {}, "expires": "x"})
        )

    def test_null_token_key_is_not_a_present_token(self):
        """匿名 session 会回 `{"accessToken": null, ...}` —— 键在，但**不是** token。

        只看"键是否存在"的探针会报 `token_key_present=True`，把"未认证"
        误诊成"提取器漏路径"，让人去修一个不存在的 bug。探针必须与
        `_auth_session_access_token`（`or` 链，null 即假）保持同一判据。
        """
        body = {"user": None, "expires": "x", "accessToken": None}
        self.assertFalse(account_creation._auth_session_access_token(body))
        self.assertFalse(account_creation._contains_access_token_key(body))

    def test_empty_string_token_key_is_not_a_present_token(self):
        body = {"accessToken": ""}
        self.assertFalse(account_creation._contains_access_token_key(body))

    def test_extractor_agrees_with_the_probe_on_the_normal_shape(self):
        """正常形状下两者必须一致，否则探针会天天误报提取器 bug。"""
        body = {"user": {}, "accessToken": "tok"}
        self.assertTrue(account_creation._auth_session_access_token(body))
        self.assertTrue(account_creation._contains_access_token_key(body))


class AuthSessionReadinessLogTests(unittest.TestCase):
    def setUp(self):
        self.records: list[logging.LogRecord] = []
        self.handler = _RecordingHandler(self.records)
        self.logger = logging.getLogger(account_creation.__name__)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def _events(self):
        return [
            record
            for record in self.records
            if str(getattr(record, "event", "") or "") == "auth_session_readiness"
        ]

    def _fetch(self, responses, attempts=None, session=None):
        with mock.patch.object(
            account_creation, "request_with_retry", side_effect=responses
        ), mock.patch.object(
            account_creation, "auth_impersonate", return_value=None
        ), mock.patch.object(
            account_creation, "CFG", {"timeouts": {"auth_session": 5}}
        ), mock.patch.object(
            account_creation.time, "sleep", return_value=None
        ):
            kwargs = {"attempts": attempts} if attempts is not None else {}
            return account_creation._fetch_auth_session(
                session if session is not None else mock.MagicMock(),
                "https://chatgpt.com",
                {},
                **kwargs,
            )

    def test_each_attempt_is_logged_with_its_shape(self):
        """四连 200 无 token —— 正是 2026-09-12 那次失败的样子。"""
        responses = [
            _Response(200, {"user": {}, "expires": "x"}),
            _Response(200, {"user": {}, "expires": "x"}),
            _Response(200, {"_raw": "<html>challenge</html>"}),
            _Response(200, {"user": {}, "expires": "x"}),
        ]
        self._fetch(responses)

        events = self._events()
        self.assertEqual(len(events), 4)
        self.assertTrue(all(event.access_token_present is False for event in events))
        self.assertEqual(events[0].status_code, 200)
        self.assertEqual(events[0].max_attempts, 4)
        self.assertEqual(events[0].body_shape, "json keys=['expires', 'user']")
        raw = "<html>challenge</html>"
        self.assertEqual(events[2].body_shape, f"non_json raw_len={len(raw)}")

    def test_jar_readout_reports_the_nextauth_session_cookie(self):
        """决定 `/api/auth/session` 是否认证的是 session token，不是 state cookie。

        `auth_flow._cookie_presence` 原来只查 `next-auth.state`（OAuth state），
        对"回调 200 但 session 匿名"这个失败完全没有读数。
        """
        self._fetch(
            [_Response(200, {"user": None, "expires": "x"})],
            attempts=1,
            session=_Session("oai-did", "__Secure-next-auth.session-token"),
        )

        event = self._events()[0]
        self.assertIs(event.nextauth_session, True)
        self.assertEqual(event.cookie_count, 2)
        self.assertTrue(event.jar_presence["nextauth_session"])

    def test_jar_readout_is_negative_without_the_session_cookie(self):
        self._fetch(
            [_Response(200, {"user": None, "expires": "x"})],
            attempts=1,
            session=_Session("oai-did", "__Secure-next-auth.state"),
        )

        event = self._events()[0]
        self.assertIs(event.nextauth_session, False)
        self.assertEqual(event.cookie_count, 2)

    def test_success_is_logged_and_stops_the_poll(self):
        responses = [_Response(200, {"accessToken": "tok"})]
        result = self._fetch(responses)

        self.assertEqual(result["status_code"], 200)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].access_token_present)
        self.assertTrue(events[0].token_key_present)

    def test_extractor_gap_is_visible(self):
        """token 在，但提取器走的路径不对 —— 探针必须把它指出来。"""
        responses = [_Response(200, {"session": {"deep": {"access_token": "tok"}}})]
        self._fetch(responses, attempts=1)

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].access_token_present)
        self.assertTrue(
            events[0].token_key_present,
            "token 明明在响应里，探针却报不存在 —— 提取器 bug 会被漏掉",
        )

    def test_readiness_log_never_carries_the_token(self):
        secret = "eyJhbGciOiJSUzI1NiIsImtpZCI6InNlY3JldC10b2tlbi12YWx1ZSJ9"
        self._fetch([_Response(200, {"accessToken": secret})])

        events = self._events()
        self.assertTrue(events)
        for record in events:
            blob = " ".join(
                [record.getMessage()] + [str(value) for value in vars(record).values()]
            )
            self.assertNotIn(secret, blob)

    def test_message_and_structured_field_never_disagree(self):
        """人读的是 message，机器读的是 ``extra`` 字段 —— 两者不能各说各话。

        2026-09-11 变异验证发现的缺口：把日志调用的**位置参数**写死（消息里
        谎报 access_token_present=True）时，``extra=`` 里的字段仍是真值，于是只
        断言字段的测试全绿。真实日志里 message 才是人 grep 的那一行，二者
        漂移会让"四连 200 无 token"看起来像"拿到了 token"。
        """
        responses = [
            _Response(200, {"user": {}, "expires": "x"}),
            _Response(200, {"accessToken": "tok"}),
        ]
        self._fetch(responses)

        events = self._events()
        self.assertEqual(len(events), 2)
        for record in events:
            message = record.getMessage()
            for field in (
                "access_token_present",
                "token_key_present",
                "nextauth_session",
                "cookie_count",
            ):
                self.assertIn(f"{field}={getattr(record, field)}", message)

    def test_persisted_log_line_survives_sanitization(self):
        """🔴 真正落盘的是 `sanitize_log_text(message)` —— 断言必须打在那一层。

        2026-09-12 实测：字段原名 `has_access_token` 命中 `sensitive_policy` 的
        `named_secret` 规则（`access_token` + `=`），**True 和 False 双双**被替换成
        `[REDACTED]`。只断言 `record.getMessage()`（脱敏**前**）的测试照样全绿，
        而运维 grep 到的那一行再也分不出真假 —— 典型的假绿。
        """
        from sms_tool.sanitizer import sanitize_log_text

        responses = [
            _Response(200, {"user": {}, "expires": "x"}),
            _Response(200, {"accessToken": "tok"}),
        ]
        self._fetch(responses)

        for record in self._events():
            line = sanitize_log_text(record.getMessage())
            self.assertNotIn(
                "[REDACTED]", line, f"落盘后字段被脱敏吃掉：{line}"
            )
            self.assertIn(f"access_token_present={record.access_token_present}", line)
            self.assertIn(f"token_key_present={record.token_key_present}", line)

    def test_structured_fields_survive_sanitization(self):
        """`.jsonl` 通道对每个字段跑 `sanitize(value, key=字段名)` —— **自动派生**逐个验。

        键名只要含 `sensitive_key_fragments` 里的词（`token`/`cookie`/...）且不以
        `safe_key_suffixes`（`_present`/`_count`/...）结尾，整个值都会被替换成
        `[REDACTED]`。所以字段名是**功能性选择**，不是风格问题。

        这里**不硬编码字段名**：从 record 里派生全部自定义字段。硬编码的版本
        只能守住今天这几个名字，未来新增一个踩雷的字段它看不见 —— 而"未来
        新增字段"正是这类回归最常见的形态。
        """
        from sms_tool.sanitizer import sanitize

        self._fetch([_Response(200, {"user": {}, "expires": "x"})], attempts=1)
        record = self._events()[0]

        custom = {
            field: value
            for field, value in vars(record).items()
            if field not in _STANDARD_LOG_ATTRS
        }
        self.assertTrue(custom, "记录里应有自定义字段")
        for field, value in custom.items():
            self.assertNotEqual(
                sanitize(value, key=field),
                "[REDACTED]",
                f"字段 {field} 会被脱敏层整值吃掉，诊断信息拿不到",
            )

    def test_readiness_log_carries_the_expected_fields(self):
        """字段集本身也是契约 —— 改名/漏字段必须大声失败。"""
        self._fetch([_Response(200, {"user": {}, "expires": "x"})], attempts=1)
        record = self._events()[0]

        self.assertLessEqual(
            _EXPECTED_READINESS_FIELDS, set(vars(record)), "readiness 记录缺字段"
        )


if __name__ == "__main__":
    unittest.main()

"""邮箱快照的调用时机：必须在任何 OTP 可能发出之前。

契约（2026-09-11 batch ``5e32aa85`` 血的教训，12 run 里 10 失败）：

* ``RegistrationEmailWorkflow._bootstrap`` **必须**调用
  ``_snapshot_mailbox_message`` —— 它回答的是「本次尝试开始前，邮箱里已经
  有什么」，用来把上一轮遗留的旧验证码排除掉。
* ``send_email_otp`` **不得**再调用它。passwordless 模式（``registration_mode``
  的默认值）下 OTP 是更早的 ``auth_flow``(authorize) 步骤发出的，等
  ``send_email_otp`` 执行时验证码邮件**可能已经在转发页上可见**；此时快照会把
  它写进 ``mailbox.seen_message_ids``，而 ``_latest_email_otp_candidate``
  （``mailbox.py``）对 id 命中者**直接跳过** ⇒ 随后 300s 轮询无论转发页怎么
  刷新都看不到正主，报 ``email_otp_poll_timeout``。
* 同理 ``auth_flow`` / ``user_register`` 也不得抢跑快照。

为什么用 AST 而不是行为测试：行为断言要 mock 掉 ``validate_config`` /
``registration_network_preflight`` / sentinel 等一大串依赖，而在那种 mock
环境下「谁先被调用」很容易写成一个恒真的断言；AST 直接盯调用点本身，且能
被变异验证（见 ``SnapshotTimingGuardTests`` 的负向用例）。
"""

from __future__ import annotations

import ast
import logging
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import mailbox, mailbox_icloud_url
from sms_tool.mailbox_types import MailboxAccount

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HANDLERS = PROJECT_ROOT / "sms_tool" / "registration_handlers.py"
# 浏览器 lane：同一语义的第二份实现。它**本来就写对了**（快照在 `_fill_email`
# 之前），但没人看守 —— 将来有人"顺手改"就会踩和协议 lane 同一个坑。
_ORCHESTRATOR = (
    PROJECT_ROOT / "sms_tool" / "registration_drivers" / "browser_flow" / "orchestrator.py"
)
_ORCHESTRATOR_FUNC = "run_browser_registration"
_FILL_EMAIL_ATTR = "_fill_email"


class _RecordingHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)

_SNAPSHOT_ATTR = "_snapshot_mailbox_message"

# 方法名 -> 期望是否调用快照。
_EXPECTED = {
    "_bootstrap": True,
    "send_email_otp": False,
    "auth_flow": False,
    "user_register": False,
}


def snapshot_call_sites(source: str) -> dict[str, bool]:
    """方法名 -> 方法体内是否出现 ``_snapshot_mailbox_message`` 调用。

    只看 ``ast.Attribute``（即 ``r._snapshot_mailbox_message(...)`` 形式），
    注释/字符串里的同名文本不算 —— 这正是 AST 相对 grep 的价值。
    """
    tree = ast.parse(source)
    sites: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = any(
            isinstance(sub, ast.Attribute) and sub.attr == _SNAPSHOT_ATTR
            for sub in ast.walk(node)
        )
        # 同名方法可能出现多次（不同类），任一处调用即视为该名字调用了快照。
        sites[node.name] = sites.get(node.name, False) or calls
    return sites


def call_line_numbers(source: str, func_name: str, attr_name: str) -> list[int]:
    """``func_name`` 体内 ``<obj>.<attr_name>(...)`` 调用的行号（升序）。

    只看 ``ast.Call`` 且 ``func`` 是 ``ast.Attribute`` —— 注释/字符串里的同名
    文本不计。返回行号而不是布尔值，这样"谁在谁之前"可以断言。
    """
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            target = node
            break
    if target is None:
        return []
    return sorted(
        node.lineno
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr_name
    )


class SnapshotTimingGuardTests(unittest.TestCase):
    def setUp(self):
        self.source = _HANDLERS.read_text(encoding="utf-8")
        self.sites = snapshot_call_sites(self.source)

    def test_extractor_sees_the_real_call_sites(self):
        """提取器非空断言：方法必须存在，否则守卫会静默变成恒真。"""
        for name in _EXPECTED:
            self.assertIn(
                name,
                self.sites,
                f"{_HANDLERS.name} 里找不到方法 {name!r} —— 守卫的提取器失效了",
            )

    def test_bootstrap_takes_the_pre_otp_baseline(self):
        self.assertTrue(
            self.sites["_bootstrap"],
            "_bootstrap 必须在任何 OTP 阶段之前做一次邮箱快照，"
            "否则上一轮遗留的旧验证码会被当成候选",
        )

    def test_no_otp_stage_snapshots_the_mailbox(self):
        for name in ("send_email_otp", "auth_flow", "user_register"):
            self.assertFalse(
                self.sites[name],
                f"{name} 不得再调用 _snapshot_mailbox_message："
                "passwordless 下验证码邮件此时可能已可见，"
                "快照会把它写进 seen_message_ids 从而永久屏蔽它",
            )


class SnapshotTimingGuardMutationTests(unittest.TestCase):
    """负向测试：把违规写法喂给提取器，守卫必须看得见。

    这两个用例互相保护 —— 把提取器变异成「永远 False」会让
    ``test_guard_sees_a_violation`` 红；变异成「永远 True」会让
    ``test_guard_reports_a_clean_file`` 红。
    """

    def test_guard_sees_a_violation(self):
        synthetic = textwrap.dedent(
            """
            class H:
                def _bootstrap(self):
                    r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)

                def send_email_otp(self):
                    r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)
            """
        )
        sites = snapshot_call_sites(synthetic)
        self.assertTrue(sites["_bootstrap"])
        self.assertTrue(
            sites["send_email_otp"],
            "提取器漏掉了 send_email_otp 里的违规调用 —— 守卫形同虚设",
        )

    def test_guard_reports_a_clean_file(self):
        synthetic = textwrap.dedent(
            """
            class H:
                def _bootstrap(self):
                    r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)

                def send_email_otp(self):
                    # 注释里提到 _snapshot_mailbox_message 不算调用
                    r._email_otp_send_url(...)
            """
        )
        sites = snapshot_call_sites(synthetic)
        self.assertTrue(sites["_bootstrap"])
        self.assertFalse(
            sites["send_email_otp"],
            "注释里的同名文本被误判成调用 —— 提取器在数文本而不是数调用",
        )

    def test_order_extractor_sees_a_reversed_pair(self):
        """顺序提取器必须能报出「先填邮箱、后快照」的违规写法。

        没有这个用例，``test_browser_lane_snapshots_before_filling_the_email``
        可能因为提取器把行号顺序弄反而恒真。
        """
        synthetic = textwrap.dedent(
            """
            def run_browser_registration(page):
                form_steps._fill_email(page, email)
                mailbox_pkg._snapshot_mailbox_message(mailbox)
            """
        )
        snapshots = call_line_numbers(synthetic, "run_browser_registration", _SNAPSHOT_ATTR)
        fills = call_line_numbers(synthetic, "run_browser_registration", _FILL_EMAIL_ATTR)
        self.assertTrue(snapshots and fills)
        self.assertGreater(
            min(snapshots),
            min(fills),
            "提取器没看出「快照在填邮箱之后」——顺序断言形同虚设",
        )


class BrowserLaneSnapshotOrderTests(unittest.TestCase):
    """浏览器 lane：快照必须排在 ``_fill_email`` **之前**。

    ``run_browser_registration`` 里 `_fill_email` 才是触发 OTP 的那一步，
    所以基线要在这之前取。这一份实现一直是对的 —— 加守卫是为了别让它被
    "顺手统一"成协议 lane 曾经那个错误写法（两个 lane 是同一语义的两份实现，
    改一处时最容易顺手改另一处）。
    """

    def setUp(self):
        self.source = _ORCHESTRATOR.read_text(encoding="utf-8")

    def test_extractor_finds_both_calls(self):
        """提取器非空断言：两边都找不到就会让顺序断言失去意义。"""
        snapshots = call_line_numbers(self.source, _ORCHESTRATOR_FUNC, _SNAPSHOT_ATTR)
        fills = call_line_numbers(self.source, _ORCHESTRATOR_FUNC, _FILL_EMAIL_ATTR)
        self.assertTrue(
            snapshots,
            f"{_ORCHESTRATOR.name}::{_ORCHESTRATOR_FUNC} 里找不到 _snapshot_mailbox_message 调用",
        )
        self.assertTrue(
            fills,
            f"{_ORCHESTRATOR.name}::{_ORCHESTRATOR_FUNC} 里找不到 _fill_email 调用",
        )

    def test_browser_lane_snapshots_before_filling_the_email(self):
        snapshots = call_line_numbers(self.source, _ORCHESTRATOR_FUNC, _SNAPSHOT_ATTR)
        fills = call_line_numbers(self.source, _ORCHESTRATOR_FUNC, _FILL_EMAIL_ATTR)
        self.assertTrue(snapshots and fills, "提取器没找到调用，先修提取器再谈顺序")
        self.assertLess(
            min(snapshots),
            min(fills),
            "浏览器 lane 的邮箱快照必须早于 _fill_email："
            "填邮箱才会触发 OTP，晚于它取基线就会把刚到的验证码标成已见",
        )


class MailboxBaselineSnapshotLogTests(unittest.TestCase):
    """基线快照必须留下**日志记录**，否则超时无法归因。

    ``print`` 不会进 ``runtime/logs/sms_tool.log``（文件 handler 只收 logging
    record），所以「快照把验证码标成了已见」和「邮件根本没到」在生产日志里
    长得一模一样 —— 2026-09-11 整个批次就卡在这个歧义上。断言用 ``event``
    字段而不是文案，避免以后改措辞就悄悄失效。
    """

    def setUp(self):
        self.records: list[logging.LogRecord] = []
        self.handler = _RecordingHandler(self.records)
        self.logger = logging.getLogger(mailbox.__name__)
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
            if str(getattr(record, "event", "") or "") == "mailbox_baseline_snapshot"
        ]

    def _mailbox(self, provider="icloud_url"):
        return MailboxAccount(
            email="target@icloud.com",
            provider=provider,
            token="http://mail.example/messages/secret/target@icloud.com",
        )

    def test_baseline_snapshot_logs_seen_id_count_and_newest_ts(self):
        # ``snapshot_icloud_url_messages`` normalises the listing into
        # ``receivedDateTime`` before returning, so the mock stands in for
        # that output -- not for the raw page payload.
        messages = [
            {"id": 41, "subject": "older", "receivedDateTime": "2026-08-03T13:52:09+00:00"},
            {"id": 42, "subject": "newer", "receivedDateTime": "2026-08-03T13:53:09+00:00"},
        ]
        with mock.patch.object(
            mailbox_icloud_url,
            "snapshot_icloud_url_messages",
            return_value=messages,
        ):
            mailbox._snapshot_mailbox_message(self._mailbox())

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].provider, "icloud_url")
        self.assertEqual(events[0].result, "ok")
        self.assertEqual(events[0].seen_id_count, 2)
        self.assertGreater(events[0].seen_newest_ts, 0)

    def test_unsupported_provider_logs_a_skip(self):
        """gmail/graph/outlook 没有基线，只能靠 issued_after 兜底。

        这条记录是为了让这些 provider 的超时**不要**被误读成快照时序 bug。
        """
        with mock.patch.object(
            mailbox_icloud_url, "snapshot_icloud_url_messages"
        ) as snapshot:
            self.assertEqual(mailbox._snapshot_mailbox_message(self._mailbox("gmail")), "")

        snapshot.assert_not_called()
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].result, "skipped")
        self.assertEqual(events[0].seen_id_count, 0)

    def test_snapshot_failure_is_logged_not_swallowed(self):
        """快照失败**不是**无害的：基线为空时轮询可能捞到上一轮的旧验证码。"""
        with mock.patch.object(
            mailbox_icloud_url,
            "snapshot_icloud_url_messages",
            side_effect=RuntimeError("listing exploded"),
        ):
            self.assertEqual(mailbox._snapshot_mailbox_message(self._mailbox()), "")

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].result, "error")
        self.assertIn("listing exploded", events[0].getMessage())

    def test_baseline_snapshot_log_never_carries_proxy_credentials(self):
        proxy = "http://alice:s3cret@proxy.example.com:8080"
        with mock.patch.object(
            mailbox_icloud_url,
            "snapshot_icloud_url_messages",
            return_value=[{"id": 1, "subject": "s", "receivedDateTime": "2026-08-03T13:52:09+00:00"}],
        ):
            mailbox._snapshot_mailbox_message(self._mailbox(), proxy=proxy)

        events = self._events()
        self.assertTrue(events)
        for record in events:
            blob = " ".join(
                [record.getMessage()] + [str(value) for value in vars(record).values()]
            )
            self.assertNotIn("s3cret", blob)
            self.assertNotIn("alice", blob)
            self.assertNotIn("proxy.example.com", blob)


if __name__ == "__main__":
    unittest.main()

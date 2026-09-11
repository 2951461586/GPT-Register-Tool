from unittest.mock import patch
from contextlib import contextmanager

from sms_tool.accounts import account_recovery
from sms_tool import codex_oauth
from sms_tool.mailbox import MailboxAccount
from sms_tool.accounts.account_identity import create_registration_identity


def test_chatgpt_email_relogin_validates_account_input():
    invalid = account_recovery.relogin_chatgpt_email_account(None)
    missing_email = account_recovery.relogin_chatgpt_email_account({})

    assert invalid == {"ok": False, "mode": "chatgpt_email_otp", "error": "invalid_account"}
    assert missing_email == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_email"}


def test_refresh_local_quota_uses_light_probe_before_browser(monkeypatch):
    calls = []

    @contextmanager
    def browser(_account, **_kwargs):
        calls.append("browser")
        yield lambda *args, **kwargs: {"status": 200, "body": {}}

    def probe(_account, **kwargs):
        calls.append("browser-fetch" if kwargs.get("browser_fetch") else "light")
        return {"ok": True, "status": "active", "status_code": 200, "quota_status": "可用"}

    account = {
        "email": "fast@example.com",
        "access_token": "at",
        "identity_context": {"browser_identity": {"driver": "camoufox"}},
    }
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(account_recovery, "probe_account_liveness", side_effect=probe),
        patch.object(account_recovery, "browser_fetch_for_account", side_effect=browser),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        patch.object(account_recovery, "clear_stale_promotion_at_marker"),
    ):
        result = account_recovery.refresh_local_quota_statuses(["fast@example.com"])

    assert result["ok"]
    assert calls == ["light"]


def test_liveness_explicit_proxy_wins_over_configured_pool():
    account = {"email": "proxy@example.com", "access_token": "at"}
    with patch.object(account_recovery, "proxy_pool_for", return_value=["http://pool-a:1", "http://pool-b:2"]), \
         patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active"}) as probe:
        account_recovery._probe_liveness_with_retries(account, proxy="http://explicit:3", timeout=10)
    assert probe.call_args.kwargs["proxy"] == "http://explicit:3"


def test_chatgpt_email_relogin_requires_saved_mailbox():
    with patch("sms_tool.codex_oauth._mailbox_from_data", return_value=None):
        result = account_recovery.relogin_chatgpt_email_account({"email": "ok@example.com"})

    assert result == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}


def test_icloud_mailbox_url_is_resolved_from_configured_pool_without_persisting_it():
    mailbox = MailboxAccount(
        email="ok@icloud.com",
        provider="icloud_url",
        source="token_file",
        token="https://mail.example/private-token/ok@icloud.com",
        auth_mode="otp_url",
    )
    with patch.object(codex_oauth, "_mailbox_from_configured_pool", return_value=mailbox) as lookup:
        result = codex_oauth._mailbox_from_data({
            "email": "ok@icloud.com",
            "mailbox": {"email": "ok@icloud.com", "provider": "icloud_url", "source": "token_file"},
        })

    assert result is mailbox
    lookup.assert_called_once_with("ok@icloud.com")


def test_refresh_local_quota_statuses_persists_result():
    with (
        patch.object(account_recovery, "get_account_record", return_value={"email": "ok@example.com", "access_token": "at_123"}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "quota_status": "active"}),
        patch.object(account_recovery, "mark_quota_status", return_value=True) as marked,
    ):
        result = account_recovery.refresh_local_quota_statuses(["ok@example.com"])

    assert result["ok"]
    marked.assert_called_once()
    assert marked.call_args.args[:2] == ("ok@example.com", "active")


def test_refresh_local_quota_reuses_definitive_scan_probe_without_reprobing():
    probes = []

    def probe(_account, **kwargs):
        probes.append(kwargs)
        return {"ok": True, "status": "active", "quota_status": "重新探测"}

    account = {"email": "fresh@example.com", "access_token": "at_123"}
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(account_recovery, "probe_account_liveness", side_effect=probe),
        patch.object(account_recovery, "mark_quota_status", return_value=True) as marked,
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["fresh@example.com"],
            fresh_probes={"fresh@example.com": {"ok": True, "status": "active", "quota_status": "可用"}},
        )

    assert result["ok"]
    # The scan probed wham moments ago; a definitive verdict must not be
    # probed a second time (doubled Cloudflare-401 exposure).
    assert probes == []
    assert marked.call_args.args[1] == "可用"
    assert marked.call_args.kwargs["quota_result"].get("probe_source") == "scan_reuse"


def test_refresh_local_quota_reprobes_transport_unknown_scan_probe():
    probes = []

    def probe(_account, **kwargs):
        probes.append(kwargs)
        return {"ok": True, "status": "active", "quota_status": "可用"}

    account = {"email": "stale@example.com", "access_token": "at_123"}
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(account_recovery, "probe_account_liveness", side_effect=probe),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["stale@example.com"],
            fresh_probes={"stale@example.com": {"ok": False, "status": "unknown", "error": "timeout"}},
        )

    assert result["ok"]
    assert len(probes) == 1
    assert result["results"][0]["quota_status"] == "可用"


def test_refresh_local_quota_keeps_probe_health_when_persistence_fails():
    with (
        patch.object(account_recovery, "get_account_record", return_value={"email": "ok@example.com", "access_token": "at_123"}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active", "quota_status": "active"}),
        patch.object(account_recovery, "mark_quota_status", return_value=False),
    ):
        result = account_recovery.refresh_local_quota_statuses(["ok@example.com"])

    assert result["ok"]
    assert result["success"] == 1
    assert result["persisted"] == 0
    assert result["persist_failed"] == 1
    assert result["results"][0]["probe_ok"] is True
    assert result["results"][0]["persisted"] is False


def test_refresh_local_quota_statuses_emits_terminal_event_per_account(monkeypatch):
    events = []
    monkeypatch.setenv("SMSWORKBENCH_EVENTS", "1")
    monkeypatch.setattr("sms_tool.desktop_ipc.emit_event", lambda payload, enabled=None: events.append(payload) or True)
    monkeypatch.setattr(account_recovery, "_local_quota_accounts", lambda emails: [
        {"email": "a@example.com", "access_token": "at-a"},
        {"email": "b@example.com", "access_token": "at-b"},
    ])
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda account, **kwargs: {"ok": True, "quota_status": "active"})
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *args, **kwargs: True)

    result = account_recovery.refresh_local_quota_statuses(["a@example.com", "b@example.com"], workers=2)

    terminal = [event for event in events if event.get("stage") == "account_completed"]
    assert result["total"] == 2
    assert len(terminal) == 2
    assert {event["account_ref"] for event in terminal} == {"a@example.com", "b@example.com"}
    assert all(event["total"] == 2 for event in terminal)


def test_refresh_local_quota_statuses_recovers_401():
    with (
        patch.object(account_recovery, "get_account_record", return_value={"email": "ok@example.com", "access_token": "old_at"}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": False, "status": "token_invalid", "quota_status": "invalid"}),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={"ok": True, "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "active"}},
        ) as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["ok@example.com"],
            relogin_on_401=True,
            relogin_mode="codex_oauth",
        )

    assert result["ok"]
    assert result["results"][0]["quota_status"] == "active"
    assert result["relogin_attempted"] == 1
    assert result["relogin_success"] == 1
    assert result["relogin_failed"] == 0
    assert relogin.call_args.kwargs["mode"] == "codex_oauth"


def test_relogin_lane_follows_requested_concurrency():
    """Regression: the relogin lane was pinned at two slots *and* acquired
    non-blocking, so with more than two 401 accounts in a batch every extra
    account was dropped instead of queued -- surfaced in the UI as
    "重登跳过：并发槽已满" while the operator had asked for 8 workers.
    """
    import threading
    import time

    emails = [f"acct{index}@example.com" for index in range(8)]
    attempted: list[str] = []
    attempted_lock = threading.Lock()

    def slow_relogin(_account, **_kwargs):
        # The mocked relogin has to actually hold its slot: if it returned
        # instantly the eight workers would never contend and this test would
        # pass even with the old two-slot, non-blocking lane.
        time.sleep(0.05)
        with attempted_lock:
            attempted.append("relogin")
        return {
            "ok": True,
            "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "active"},
        }

    with (
        patch.object(
            account_recovery,
            "get_account_record",
            side_effect=lambda *args, **_kwargs: {"email": args[0] if args else "", "access_token": "old_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "quota_status": "invalid"},
        ),
        patch.object(account_recovery, "relogin_codex_account", side_effect=slow_relogin),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            emails,
            relogin_on_401=True,
            relogin_mode="codex_oauth",
            workers=8,
        )

    # Every account gets a relogin; none may be skipped for want of a slot.
    assert len(attempted) == 8
    assert result["relogin_attempted"] == 8
    assert result["relogin_success"] == 8
    assert result["relogin_failed"] == 0


def test_account_deadline_does_not_mask_confirmed_401_after_relogin():
    """Regression: when recovery ran past its deadline, the timeout branch
    rewrote the already-confirmed 401 probe to status "timeout" -- the panel
    then showed 网络超时 for accounts whose AT was provably revoked, and the
    relogin note no longer matched the displayed classification."""
    import threading  # noqa: F401  (documents the threaded executor context)

    clock = {"t": 1000.0}

    def slow_failed_relogin(_account, **_kwargs):
        clock["t"] += 70.0  # burn past the 60s relogin budget
        return {"ok": False, "mode": "chatgpt_email_otp", "error": "existing_login_otp_poll_timeout"}

    with (
        patch.object(account_recovery.time, "monotonic", side_effect=lambda: clock["t"]),
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "slow@example.com", "access_token": "old_at", "password": "pw"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效"},
        ),
        patch.object(account_recovery, "relogin_codex_account", side_effect=slow_failed_relogin),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        # Hermetic: the 掉号 marker fires for this password-only account and
        # must not leak a synthetic test row into the production database.
        patch.object(account_recovery, "upsert_account", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["slow@example.com"],
            workers=1,
            relogin_on_401=True,
            relogin_timeout=60,
            account_timeout=30,
            batch_timeout=300,
        )

    probe = result["results"][0]["probe"]
    assert probe["status"] == "token_invalid"
    assert result["results"][0]["relogin"]["error"] == "existing_login_otp_poll_timeout"


def test_account_deadline_still_marks_undetermined_probe_as_timeout():
    """Guard rails both ways: a probe without a definitive classification is
    still stamped as a timeout once the account budget is gone."""
    clock = {"t": 1000.0}

    def slow_probe(*_args, **_kwargs):
        clock["t"] += 40.0  # past the 30s account budget
        return {"ok": False, "status": "unknown", "quota_status": "检测失败"}

    with (
        patch.object(account_recovery.time, "monotonic", side_effect=lambda: clock["t"]),
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "hang@example.com", "access_token": "old_at"},
        ),
        patch.object(account_recovery, "probe_account_liveness", side_effect=slow_probe),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["hang@example.com"],
            workers=1,
            account_timeout=30,
            batch_timeout=300,
        )

    assert result["results"][0]["probe"]["status"] == "timeout"


def test_refresh_local_quota_statuses_does_not_count_persisted_401_as_success():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "invalid@example.com", "access_token": "expired_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={
                "ok": False,
                "status": "token_invalid",
                "status_code": 401,
                "quota_status": "401失效",
            },
        ),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        # The account carries no recovery material, so the 掉号 marker fires;
        # keep the test hermetic by stubbing the upsert.
        patch.object(account_recovery, "upsert_account", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["invalid@example.com"])

    assert not result["ok"]
    assert result["success"] == 0
    assert result["failed"] == 1
    assert result["persisted"] == 1
    assert result["persist_failed"] == 0
    assert result["at_invalid"] == 1
    assert result["account_deactivated"] == 0
    assert result["probe_failed"] == 0
    assert result["results"][0]["persisted"] is True
    assert result["results"][0]["probe_ok"] is False
    assert result["results"][0]["ok"] is False


def test_refresh_local_quota_statuses_accepts_http_401_without_normalized_status():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "status-code-only@example.com", "access_token": "expired_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status_code": 401, "quota_status": "401失效"},
        ),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={
                "ok": True,
                "probe": {"ok": True, "status_code": 200, "status": "active"},
            },
        ) as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["status-code-only@example.com"],
            relogin_on_401=True,
        )

    assert result["ok"]
    relogin.assert_called_once()


def test_refresh_local_quota_statuses_classifies_terminal_account_without_relogin():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={
                "email": "closed@example.com",
                "access_token": "expired_at",
                "status": "account_deactivated",
            },
        ),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch.object(account_recovery, "relogin_codex_account") as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["closed@example.com"],
            relogin_on_401=True,
        )

    assert not result["ok"]
    assert result["account_deactivated"] == 1
    assert result["at_invalid"] == 0
    assert result["probe_failed"] == 0
    assert result["relogin_attempted"] == 0
    probe.assert_not_called()
    relogin.assert_not_called()


def test_health_status_codes_distinguish_relogin_otp_and_timeout(monkeypatch):
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *args, **kwargs: True)
    timed_out = account_recovery._timed_out_health_result("timeout@example.com", "batch_timeout")

    assert timed_out["health_status"] == "batch_timeout"
    assert timed_out["timed_out"] is True
    assert account_recovery._health_status_code(
        {"ok": False, "status": "token_invalid"},
        {"ok": False, "error": "email_otp_timeout"},
    ) == "relogin_otp_failed"
    assert account_recovery._health_status_code({"ok": True, "status": "active"}, {}) == "active"


def test_liveness_result_distinguishes_401_and_blocks_relogin_when_mailbox_pool_is_quarantined(monkeypatch):
    account = {"email": "user@example.com", "access_token": "at"}
    monkeypatch.setattr(account_recovery, "get_account_record", lambda email: account)
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda *args, **kwargs: {
        "ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效",
    })
    monkeypatch.setattr(account_recovery, "mailbox_relogin_allowed", lambda email=None: False)
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *args, **kwargs: True)
    # No recovery material on this account, so the 掉号 marker would fire;
    # stub the upsert to keep the test hermetic.
    monkeypatch.setattr(account_recovery, "upsert_account", lambda *args, **kwargs: True)

    result = account_recovery.refresh_local_quota_statuses(
        ["user@example.com"], relogin_on_401=True, batch_timeout=30, account_timeout=30
    )

    row = result["results"][0]
    assert row["liveness_401"] is True
    assert row["relogin_attempted"] is False
    assert row["mailbox_auth_invalid"] is False
    assert row["relogin"]["error"] == "mailbox_pool_repair_required"
    assert result["liveness_401"] == 1
    assert result["relogin_attempted"] == 0


def test_token_invalid_without_recovery_material_is_marked_dropped():
    account = {"email": "drop@example.com", "access_token": "dead_at"}
    persisted = []
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效"},
        ),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        patch.object(
            account_recovery,
            "upsert_account",
            side_effect=lambda data, json_path="": persisted.append(data) or True,
        ),
    ):
        result = account_recovery.refresh_local_quota_statuses(["drop@example.com"])

    assert not result["ok"]
    assert result["results"][0]["probe"]["dropped"] == "token_revoked"
    assert persisted, "unrecoverable revoked token must be persisted as 掉号"
    assert persisted[0]["status"] == "at_invalid"
    assert persisted[0]["error"] == "token_revoked_unrecoverable"
    assert persisted[0]["terminal_failure"]["code"] == "token_revoked"


def test_password_alone_is_not_relogin_material():
    """The auto cascade has no password-login strategy, so a stored password
    must not shield an account from 掉号 marking; a mailbox *provider* still
    counts because ReMail rehydrates credentials supplier-side by email."""
    assert not account_recovery._has_relogin_material(
        {"email": "a@example.com", "password": "pw", "session_token": "st"}
    )
    assert account_recovery._has_relogin_material({"email": "a@example.com", "mailbox_provider": "remail"})
    assert account_recovery._has_relogin_material({"email": "a@example.com", "mailbox": {"provider": "icloud_url"}})
    assert account_recovery._has_relogin_material({"email": "a@example.com", "refresh_token": "rt"})


def test_token_invalid_with_password_only_is_marked_dropped():
    account = {"email": "pwonly@example.com", "access_token": "dead_at", "password": "pw"}
    persisted = []
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效"},
        ),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        patch.object(
            account_recovery,
            "upsert_account",
            side_effect=lambda data, json_path="": persisted.append(data) or True,
        ),
    ):
        result = account_recovery.refresh_local_quota_statuses(["pwonly@example.com"])

    assert result["results"][0]["probe"]["dropped"] == "token_revoked"
    assert persisted and persisted[0]["status"] == "at_invalid"


def test_relogin_skips_second_otp_poll_after_first_times_out():
    """Both OTP strategies poll the same mailbox; once the first poll window
    elapsed with no mail, the second cannot succeed. Skipping it halves the
    per-account cascade cost that was starving the batch budget."""

    def fail(error):
        return {"ok": False, "error": error}

    with (
        patch.object(account_recovery, "_select_recovery_proxy", return_value=(None, [])),
        patch.object(
            account_recovery, "relogin_refresh_token_account", return_value=fail("missing_refresh_token")
        ),
        patch.object(
            account_recovery, "relogin_web_session_account", return_value=fail("web_session_access_token_probe_failed:401")
        ),
        patch.object(
            account_recovery, "relogin_chatgpt_email_account", return_value=fail("existing_login_otp_poll_timeout")
        ),
        patch.object(account_recovery, "relogin_local_codex_account") as codex,
        patch.object(
            account_recovery, "relogin_browser_session_account", return_value=fail("browser_session_access_token_missing")
        ),
    ):
        result = account_recovery.relogin_codex_account(
            {"email": "otp@example.com", "access_token": "dead_at"}, mode="auto"
        )

    assert result["error"] == "all_relogin_methods_failed"
    codex.assert_not_called()
    skipped = [a for a in result["attempts"] if a.get("skipped")]
    assert skipped and skipped[0]["mode"] == "codex_oauth_pkce"


def test_relogin_runs_second_otp_poll_when_first_fails_for_other_reasons():
    def fail(error):
        return {"ok": False, "error": error}

    with (
        patch.object(account_recovery, "_select_recovery_proxy", return_value=(None, [])),
        patch.object(
            account_recovery, "relogin_refresh_token_account", return_value=fail("missing_refresh_token")
        ),
        patch.object(
            account_recovery, "relogin_web_session_account", return_value=fail("web_session_access_token_probe_failed:401")
        ),
        patch.object(
            account_recovery, "relogin_chatgpt_email_account", return_value=fail("mailbox_transport_unavailable")
        ),
        patch.object(
            account_recovery, "relogin_local_codex_account", return_value=fail("passwordless_email_otp_poll_timeout")
        ) as codex,
        patch.object(
            account_recovery, "relogin_browser_session_account", return_value=fail("browser_session_access_token_missing")
        ),
    ):
        result = account_recovery.relogin_codex_account(
            {"email": "otp2@example.com", "access_token": "dead_at"}, mode="auto"
        )

    assert result["error"] == "all_relogin_methods_failed"
    codex.assert_called_once()


def test_dropped_account_skips_probe_and_relogin():
    """Regression: the 掉号 synthetic probe keeps status token_invalid, which
    re-armed the relogin cascade on every later batch — already-marked
    accounts burned a full 5-strategy relogin each run."""
    account = {
        "email": "gone@example.com",
        "access_token": "dead_at",
        "raw_json": '{"terminal_failure": {"code": "token_revoked", "reason": "token_invalid_no_relogin_material"}}',
    }
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(account_recovery, "probe_account_liveness") as probe_mock,
        patch.object(account_recovery, "relogin_codex_account") as relogin_mock,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        patch.object(account_recovery, "upsert_account", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["gone@example.com"], relogin_on_401=True
        )

    probe_mock.assert_not_called()
    relogin_mock.assert_not_called()
    item = result["results"][0]
    assert item["probe"]["status"] == "token_invalid"
    assert item["probe"]["terminal"] is True
    assert item["relogin_attempted"] is False


def test_token_invalid_with_mailbox_material_is_not_marked_when_breaker_closed(monkeypatch):
    account = {"email": "keep@example.com", "access_token": "at", "mailbox_token": "mt"}
    monkeypatch.setattr(account_recovery, "get_account_record", lambda email: account)
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda *a, **k: {
        "ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效",
    })
    monkeypatch.setattr(account_recovery, "mailbox_relogin_allowed", lambda email=None: False)
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *a, **k: True)
    dropped = []
    monkeypatch.setattr(account_recovery, "_persist_token_revoked_drop", lambda acc: dropped.append(acc) or True)

    result = account_recovery.refresh_local_quota_statuses(
        ["keep@example.com"], relogin_on_401=True, batch_timeout=30, account_timeout=30
    )

    assert dropped == []
    assert "dropped" not in result["results"][0]["probe"]
    assert result["results"][0]["relogin"]["error"] == "mailbox_pool_repair_required"


def test_token_invalid_during_relogin_cooldown_is_not_marked(monkeypatch):
    account = {"email": "cool@example.com", "access_token": "at"}
    monkeypatch.setattr(account_recovery, "get_account_record", lambda email: account)
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda *a, **k: {
        "ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效",
    })
    monkeypatch.setattr(account_recovery, "mailbox_relogin_allowed", lambda email=None: True)
    monkeypatch.setattr(account_recovery, "_relogin_cooldown_active", lambda acc: True)
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *a, **k: True)
    dropped = []
    monkeypatch.setattr(account_recovery, "_persist_token_revoked_drop", lambda acc: dropped.append(acc) or True)

    result = account_recovery.refresh_local_quota_statuses(
        ["cool@example.com"], relogin_on_401=True, batch_timeout=30, account_timeout=30
    )

    assert dropped == []
    assert result["results"][0]["relogin"]["error"] == "relogin_cooldown"


def test_token_revoked_drop_skips_probe_and_relogin_on_later_runs():
    account = {
        "email": "gone@example.com",
        "access_token": "dead",
        "terminal_failure": {"code": "token_revoked", "reason": "token_invalid_no_relogin_material", "updated_at": 1},
    }
    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch.object(account_recovery, "relogin_codex_account") as relogin,
        patch.object(account_recovery, "mailbox_relogin_allowed", side_effect=lambda email=None: False),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
        patch.object(account_recovery, "upsert_account", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["gone@example.com"], relogin_on_401=True)

    probe.assert_not_called()
    relogin.assert_not_called()
    assert result["at_invalid"] == 1
    assert result["results"][0]["probe"]["error"] == "token_revoked_unrecoverable"


def test_relogin_terminal_deactivation_is_not_double_marked(monkeypatch):
    """A terminal relogin answer is persisted by the relogin chain itself via
    _persist_permanent_deactivation; the 掉号 marker must not pile on."""
    account = {"email": "term@example.com", "access_token": "at"}
    monkeypatch.setattr(account_recovery, "get_account_record", lambda email: account)
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda *a, **k: {
        "ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效",
    })
    monkeypatch.setattr(account_recovery, "mailbox_relogin_allowed", lambda email=None: True)
    monkeypatch.setattr(account_recovery, "relogin_codex_account", lambda *a, **k: {
        "ok": False, "mode": "chatgpt_email_otp", "error": "account_deactivated", "terminal": True,
    })
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *a, **k: True)
    dropped = []
    monkeypatch.setattr(account_recovery, "_persist_token_revoked_drop", lambda acc: dropped.append(acc) or True)

    result = account_recovery.refresh_local_quota_statuses(
        ["term@example.com"], relogin_on_401=True, batch_timeout=60, account_timeout=60
    )

    assert dropped == []
    assert result["results"][0]["relogin"]["terminal"] is True


def test_relogin_otp_failure_enters_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {"relogin_cooldown_seconds": 1800}})
    account = {"email": "known@example.com", "quota_status": "legacy/OTP failure", "quota_updated_at": int(__import__('time').time())}
    assert account_recovery._relogin_cooldown_active(account)
    account = {"email": "new@example.com"}
    account_recovery._record_relogin_failure(account["email"], {"ok": False, "mode": "chatgpt_email_otp", "error": "otp_timeout"})
    assert account_recovery._relogin_cooldown_active(account)


def test_relogin_auto_uses_refresh_cookie_email_then_oauth():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "invalid_grant"},
        ) as refresh,
        patch.object(
            account_recovery,
            "relogin_web_session_account",
            return_value={"ok": False, "mode": "web_session", "error": "missing_session_cookie"},
        ) as web,
        patch.object(
            account_recovery,
            "relogin_chatgpt_email_account",
            return_value={"ok": False, "mode": "chatgpt_email_otp", "error": "email_login_failed"},
        ) as email_otp,
        patch.object(
            account_recovery,
            "relogin_local_codex_account",
            return_value={"ok": True, "mode": "codex_oauth_pkce"},
        ) as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    # Browser re-login has been removed: the auto chain is protocol-only.
    assert [item["mode"] for item in result["attempts"]] == [
        "oauth_refresh_token",
        "web_session",
        "chatgpt_email_otp",
    ]
    refresh.assert_called_once()
    web.assert_called_once()
    email_otp.assert_called_once()
    oauth.assert_called_once()
    assert not hasattr(account_recovery, "relogin_browser_account")


def test_relogin_reuses_account_proxy_affinity_for_every_strategy():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:secret@proxy.example:443"
    account = {
        "email": "ok@example.com",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }
    config = {"proxy": {"registration": base_proxy, "pool": [base_proxy]}}

    with (
        patch.object(account_recovery, "CFG", config),
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "invalid_grant"},
        ) as refresh,
        patch.object(
            account_recovery,
            "relogin_web_session_account",
            return_value={"ok": True, "mode": "web_session"},
        ) as web,
    ):
        result = account_recovery.relogin_codex_account(
            account,
            proxy="http://127.0.0.1:7897",
            mode="auto",
        )

    assert result["ok"]
    assert refresh.call_args.kwargs["proxy"] == registration_proxy
    assert web.call_args.kwargs["proxy"] == registration_proxy


def test_relogin_auto_stops_after_refresh_token_success():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": True, "mode": "oauth_refresh_token", "persisted": True},
        ) as refresh,
        patch.object(account_recovery, "relogin_web_session_account") as web,
        patch.object(account_recovery, "relogin_chatgpt_email_account") as email_otp,
        patch.object(account_recovery, "relogin_local_codex_account") as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    assert result["mode"] == "oauth_refresh_token"
    assert result["attempts"] == []
    refresh.assert_called_once()
    web.assert_not_called()
    email_otp.assert_not_called()
    oauth.assert_not_called()


def test_relogin_auto_persists_permanent_deactivation():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "account_deactivated"},
        ),
        patch.object(account_recovery, "_persist_permanent_deactivation", return_value=True) as persist,
        patch.object(account_recovery, "relogin_web_session_account") as web,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["terminal"] is True
    assert result["error"] == "account_deactivated"
    persist.assert_called_once()
    web.assert_not_called()


def test_relogin_persists_only_after_http_200_probe():
    oauth_result = {"ok": True, "tokens": {"access_token": "new_at", "refresh_token": "rt_new"}}
    with (
        patch("sms_tool.codex_oauth.refresh_codex_oauth_session", return_value=oauth_result),
        patch("sms_tool.codex_oauth._save_oauth_tokens", return_value={"ok": True, "mode": "codex_oauth_pkce"}) as save,
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active", "status_code": 200}),
    ):
        result = account_recovery.relogin_local_codex_account({"email": "ok@example.com", "access_token": "old_at"})

    assert result["ok"]
    assert result["persisted"]
    save.assert_called_once()


def test_successful_relogin_replaces_stale_quota_401_metadata():
    data = {
        "status": "at_invalid",
        "error": "oauth_refresh_http_401",
        "quota_status": "401失效",
        "quota": {
            "status": "401失效",
            "last_result": {"status": "token_invalid", "status_code": 401},
        },
    }
    probe = {
        "ok": True,
        "status": "active",
        "status_code": 200,
        "quota_status": "可用",
        "access_token": "must-not-persist-in-quota-metadata",
    }

    account_recovery._mark_successful_relogin(data, probe, now=123)

    assert data["status"] == "registered"
    assert "error" not in data
    assert data["quota_status"] == "可用"
    assert data["quota_updated_at"] == 123
    assert data["quota"]["status"] == "可用"
    assert data["quota"]["updated_at"] == 123
    assert data["quota"]["last_result"]["status_code"] == 200
    assert "access_token" not in data["quota"]["last_result"]


def test_successful_relogin_clears_stale_promotion_at_marker():
    data = {
        "status": "at_invalid",
        "promotion_status": "AT失效",
        "promotion": {"status": "AT失效", "last_result": {"status_code": 401}},
    }
    probe = {
        "ok": True,
        "status": "active",
        "status_code": 200,
        "quota_status": "可用",
    }

    account_recovery._mark_successful_relogin(data, probe, now=123)

    assert data["promotion_status"] == ""
    assert data["promotion"]["status"] == ""
    assert data["promotion"]["last_result"]["status_code"] == 401


def test_refresh_token_recovery_verifies_before_persisting():
    account = {
        "email": "ok@example.com",
        "access_token": "old_at",
        "oauth_refresh_token": "rt_old",
        "json_path": "session.json",
        "success": False,
        "status": "at_invalid",
        "error": "oauth_refresh_http_401",
        "account_scan": {"token_probe": {"status": "token_invalid", "status_code": 401}},
    }
    with (
        patch("sms_tool.codex_export._openai_refresh_token", return_value="rt_old"),
        patch("sms_tool.codex_export._refresh_with_openai_oauth", return_value={
            "ok": True,
            "data": {"access_token": "new_at", "oauth_refresh_token": "rt_new"},
        }),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": True, "status": "active", "status_code": 200},
        ) as probe,
        patch("sms_tool.session_refresh._save_refreshed", return_value="session.json") as save,
    ):
        result = account_recovery.relogin_refresh_token_account(account)

    assert result["ok"]
    assert result["mode"] == "oauth_refresh_token"
    assert result["persisted"]
    assert probe.call_args.args[0]["access_token"] == "new_at"
    assert save.call_args.args[0]["oauth_refresh_token"] == "rt_new"
    assert save.call_args.args[0]["status"] == "registered"
    assert "error" not in save.call_args.args[0]
    assert save.call_args.args[0]["account_scan_status"] == "alive"
    assert save.call_args.args[0]["account_scan"]["token_probe"]["status_code"] == 200


def test_refresh_token_recovery_rejects_unverified_candidate():
    account = {"email": "ok@example.com", "oauth_refresh_token": "rt_old"}
    with (
        patch("sms_tool.codex_export._openai_refresh_token", return_value="rt_old"),
        patch("sms_tool.codex_export._refresh_with_openai_oauth", return_value={
            "ok": True,
            "data": {"access_token": "new_at"},
        }),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "status_code": 401},
        ),
        patch("sms_tool.session_refresh._save_refreshed") as save,
    ):
        result = account_recovery.relogin_refresh_token_account(account)

    assert not result["ok"]
    assert result["error"] == "oauth_refresh_token_access_token_probe_failed:401"
    save.assert_not_called()


def test_web_session_rejects_a_cookie_for_another_account():
    candidate = {
        "email": "ok@example.com",
        "access_token": "new_at",
        "auth_session": {"user": {"email": "other@example.com"}},
    }
    with (
        patch("sms_tool.session_refresh._refresh_session_protocol", return_value={"ok": True, "data": candidate}),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch("sms_tool.session_refresh._save_refreshed") as save,
    ):
        result = account_recovery.relogin_web_session_account({"email": "ok@example.com"})

    assert not result["ok"]
    assert result["error"] == "auth_session_email_mismatch"
    probe.assert_not_called()
    save.assert_not_called()


def test_recovery_proxy_uses_registration_country_and_pool():
    with (
        patch.dict(account_recovery.CFG, {
            "proxy": {
                "pool": ["http://pool.example:8080"],
                "registration": "http://registration.example:8080",
                "default": "http://default.example:8080",
            }
        }, clear=False),
        patch(
            "sms_tool.paypal_proxy.select_proxy_from_pool",
            return_value=("http://selected.example:8080", [{"ok": True, "expected_country": "JP"}]),
        ) as select,
    ):
        proxy, attempts = account_recovery._select_recovery_proxy(
            {"registration_country": "jp"},
            "http://explicit.example:8080",
        )

    assert proxy == "http://selected.example:8080"
    assert attempts[0]["ok"]
    assert select.call_args.args[1:] == ("JP", "account_recovery")
    assert select.call_args.args[0][0] == "http://explicit.example:8080"



def test_refresh_local_quota_statuses_clears_stale_promotion_marker_after_relogin():
    cleared = {"called": False}

    def fake_clear(email, **kwargs):
        cleared["called"] = True
        return True

    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "stale@example.com", "access_token": "old_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "quota_status": "401SHIXIAO"},
        ),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={"ok": True, "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "active"}},
        ),
        patch.object(account_recovery, "clear_stale_promotion_at_marker", side_effect=fake_clear),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["stale@example.com"],
            relogin_on_401=True,
            relogin_mode="codex_oauth",
        )

    assert result["relogin_success"] == 1
    assert cleared["called"]


def test_refresh_local_quota_statuses_clears_promotion_marker_after_verified_probe():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "ok@example.com", "access_token": "at_123"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": True, "quota_status": "active"},
        ),
        patch.object(account_recovery, "clear_stale_promotion_at_marker") as clear_marker,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["ok@example.com"])

    assert result["ok"]
    clear_marker.assert_called_once()


def test_liveness_transport_failure_retries_configured_pool(monkeypatch):
    account = {"email": "retry@example.com", "access_token": "at"}
    calls = []

    def fake_probe(account, proxy=None, timeout=30, browser_fetch=None):
        calls.append(proxy)
        if len(calls) < 2:
            return {"ok": False, "status_code": 0, "error": "curl: (35) connection reset", "quota_status": "检测失败"}
        return {"ok": True, "status_code": 200, "status": "active", "quota_status": "可用"}

    monkeypatch.setattr(account_recovery, "CFG", {
        "proxy": {"registration": "http://registration.example:8080", "pool": [
            "http://pool-a.example:8080", "http://pool-b.example:8080"
        ]},
        "account_health": {"use_registration_affinity": True},
    })
    monkeypatch.setattr(account_recovery, "probe_account_liveness", fake_probe)
    result = account_recovery._probe_liveness_with_retries(
        account, proxy="http://127.0.0.1:7897", timeout=5
    )
    assert result["ok"]
    assert calls == ["http://127.0.0.1:7897", "http://registration.example:8080"]


def test_desktop_read_hides_stale_promotion_at_marker_after_verified_200():
    import json as json_mod
    from sms_tool.desktop_read import _record_payload

    stale_label = "AT" + chr(0x5931) + chr(0x6548)

    def record_with(probe_state):
        return {
            "id": "1",
            "email": "stale@example.com",
            "json_path": "",
            "raw_json": json_mod.dumps({
                "email": "stale@example.com",
                "promotion_status": stale_label,
                "promotion": {"status": stale_label, "last_result": {"status_code": 401}},
                **probe_state,
            }),
        }

    fresh_probe = {
        "quota": {"last_result": {"status_code": 200}, "status": "ok"},
        "quota_updated_at": 200,
        "account_scan": {"token_probe": {"status_code": 401}},
        "account_scan_updated_at": 100,
    }
    payload = _record_payload(record_with(fresh_probe))
    assert "promotion_status" not in payload
    assert payload["at_probe_status_code"] == "200"

    still_401 = {
        "quota": {"last_result": {"status_code": 401}, "status": "bad"},
        "quota_updated_at": 200,
    }
    payload_401 = _record_payload(record_with(still_401))
    assert payload_401["promotion_status"] == stale_label


def test_browser_recovery_uses_driver_from_browser_identity():
    """Browser recovery reopens the same driver recorded at registration."""
    from unittest.mock import MagicMock, patch

    account = {
        "email": "browser@example.com",
        "access_token": "expired_at",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": "cloak", "profile_id": "browser@example.com"},
        ),
    }
    mock_browser = MagicMock()
    mock_browser.__enter__ = MagicMock(return_value=mock_browser)
    mock_browser.__exit__ = MagicMock(return_value=False)
    mock_browser.page = MagicMock()
    mock_browser.cookie_header.return_value = ""

    with (
        patch("sms_tool.accounts.account_recovery.CFG", {"chatgpt": {"chat_base_url": "https://chatgpt.com", "auth_base_url": "https://auth.openai.com"}, "registration": {}}),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", return_value=mock_browser) as create_session,
        patch("sms_tool.registration_drivers.browser_flow.page_state._wait_for_challenge_clear"),
        patch("sms_tool.registration_drivers.browser_flow.session._session_payload", return_value={"body": {}, "access_token": "new_at", "id_token": ""}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active", "status_code": 200}),
        patch("sms_tool.session_refresh._save_refreshed", return_value="session.json"),
    ):
        result = account_recovery.relogin_browser_session_account(account)

    assert result["ok"]
    # Must use the driver from browser_identity, not the default camoufox
    assert create_session.call_args.args[0] == "cloak"
    # Must pass browser_identity so the same profile is reopened
    assert create_session.call_args.kwargs["browser_identity"] == {"driver": "cloak", "profile_id": "browser@example.com"}


def test_refresh_local_quota_statuses_uses_browser_fetch_when_browser_identity_present():
    """Liveness probe routes through browser context when browser_identity is present."""
    from unittest.mock import MagicMock, patch

    account = {
        "email": "browser@example.com",
        "access_token": "at_123",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": "camoufox", "profile_id": "browser@example.com"},
        ),
    }

    mock_browser = MagicMock()
    mock_browser.fetch_json = MagicMock(return_value={"status_code": 200, "body": {}})
    mock_browser.page = MagicMock()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_browser)
    mock_session.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", return_value=mock_session) as create_session,
        patch("sms_tool.registration_drivers.browser_flow.page_state._wait_for_challenge_clear"),
        patch.object(account_recovery, "probe_account_liveness", wraps=account_recovery.probe_account_liveness) as probe,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["browser@example.com"])

    assert result["ok"]
    # Must have opened a browser session with the saved driver
    assert create_session.call_args.args[0] == "camoufox"
    assert create_session.call_args.kwargs.get("browser_identity") == {
        "driver": "camoufox",
        "profile_id": "browser@example.com",
    }
    # Must have passed browser_fetch to probe_account_liveness
    assert probe.call_args.kwargs.get("browser_fetch") is not None


def test_browser_liveness_reuses_persisted_geo_aligned_profile():
    """Follow-up browser probes reopen the registration locale/timezone."""
    from unittest.mock import MagicMock, patch

    identity = create_registration_identity(
        "http://proxy.example:8080",
        pool_index=0,
        fingerprint_key="chrome146",
        device_id="device-jp",
        account_key="browser@example.com",
        browser_identity={"driver": "camoufox", "profile_id": "browser@example.com"},
    )
    identity.update({"geo_country": "JP", "geo_timezone": "Asia/Tokyo", "fingerprint_seed": "device-jp"})
    account = {"email": "browser@example.com", "access_token": "at_123", "identity_context": identity}
    mock_browser = MagicMock()
    mock_browser.fetch_json = MagicMock(return_value={"status_code": 200, "body": {}})
    mock_browser.page = MagicMock()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_browser)
    mock_session.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", return_value=mock_session) as create_session,
        patch("sms_tool.registration_drivers.browser_flow.page_state._wait_for_challenge_clear"),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["browser@example.com"])

    assert result["ok"]
    assert create_session.call_args.kwargs["locale"] == "ja-JP"
    assert create_session.call_args.kwargs["timezone_id"] == "Asia/Tokyo"


def test_refresh_local_quota_statuses_falls_back_to_curl_when_no_browser_identity():
    """Liveness probe uses curl_cffi when no browser_identity is present."""
    from unittest.mock import patch

    account = {
        "email": "plain@example.com",
        "access_token": "at_123",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session") as create_session,
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "quota_status": "active"}) as probe,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["plain@example.com"])

    assert result["ok"]
    # Must NOT have opened a browser session
    create_session.assert_not_called()
    # Must NOT have passed browser_fetch
    assert "browser_fetch" not in probe.call_args.kwargs or probe.call_args.kwargs.get("browser_fetch") is None


def test_browser_liveness_does_not_downgrade_to_curl_when_context_unavailable():
    """A browser account must fail closed instead of changing its fingerprint."""
    from unittest.mock import patch

    account = {
        "email": "browser@example.com",
        "access_token": "at_123",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            browser_identity={"driver": "camoufox", "profile_id": "browser@example.com"},
        ),
    }

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", side_effect=RuntimeError("unavailable")),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["browser@example.com"])

    assert not result["ok"]
    assert result["results"][0]["probe"].get("browser_fallback") == "unavailable"
    probe.assert_called_once()

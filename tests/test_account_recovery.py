from unittest.mock import Mock, patch
from contextlib import contextmanager

from sms_tool.accounts import account_recovery
from sms_tool.accounts import recovery_batch
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
        result = recovery_batch.refresh_local_quota_statuses(["fast@example.com"])

    assert result["ok"]
    assert calls == ["light"]


def test_liveness_explicit_proxy_wins_over_configured_pool():
    account = {"email": "proxy@example.com", "access_token": "at"}
    with patch.object(account_recovery, "proxy_pool_for", return_value=["http://pool-a:1", "http://pool-b:2"]), \
         patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active"}) as probe:
        recovery_batch._probe_liveness_with_retries(account, proxy="http://explicit:3", timeout=10)
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
        result = recovery_batch.refresh_local_quota_statuses(["ok@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(["ok@example.com"])

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

    result = recovery_batch.refresh_local_quota_statuses(["a@example.com", "b@example.com"], workers=2)

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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(["invalid@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(
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

    result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(["drop@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(["pwonly@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(
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

    result = recovery_batch.refresh_local_quota_statuses(
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

    result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(["gone@example.com"], relogin_on_401=True)

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

    result = recovery_batch.refresh_local_quota_statuses(
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


def _guard_entry(tmp_path, email="dead@example.com"):
    import json

    path = account_recovery._relogin_guard_path()
    assert path.is_file(), "the failure was not recorded at all"
    return json.loads(path.read_text(encoding="utf-8"))[email]


# What the ``/about-you`` lane returns for an address that already has an account.
# Measured 2026-09-14: ``create_account`` answers 400 ``user_already_exists`` with
# ``userAlreadyExistsRecovery.action=continue_to_login``, 23/23 byte-identical.
ABOUT_YOU_DEAD_END = {
    "ok": False,
    "mode": "codex_oauth_pkce",
    "error": "about_you_existing_account_user_already_exists:continue_to_login",
    "last_url": "https://auth.openai.com/about-you",
}


def test_relogin_dead_end_is_recorded_as_a_permanent_account_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {}})
    account_recovery._record_relogin_failure("dead@example.com", ABOUT_YOU_DEAD_END)

    entry = _guard_entry(tmp_path)
    assert entry["failure_class"] == "account"
    assert entry["dead_end"] == "account_exists_login_loop"
    assert entry["permanent"] is True


def test_relogin_dead_end_is_recorded_even_without_an_otp_marker(tmp_path, monkeypatch):
    """The stop must rest on the dead end itself, not on ``fallback_from`` wording.

    ``ABOUT_YOU_DEAD_END`` mentions neither ``otp`` nor ``mailbox`` nor ``email``:
    gating the record on those markers would silently drop exactly the failure
    this fix exists for.
    """
    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {}})
    text = str(ABOUT_YOU_DEAD_END).lower()
    assert not any(marker in text for marker in ("otp", "mailbox", "email"))

    account_recovery._record_relogin_failure("dead@example.com", ABOUT_YOU_DEAD_END)

    assert _guard_entry(tmp_path)["dead_end"] == "account_exists_login_loop"


def test_an_ordinary_otp_failure_is_not_marked_permanent(tmp_path, monkeypatch):
    """Negative control: the escalation must not swallow every relogin failure."""
    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {}})
    account_recovery._record_relogin_failure(
        "flaky@example.com", {"ok": False, "mode": "chatgpt_email_otp", "error": "otp_timeout"}
    )

    entry = _guard_entry(tmp_path, "flaky@example.com")
    assert entry["failure_class"] == "relogin_otp_failed"
    assert "permanent" not in entry
    assert "dead_end" not in entry


def test_a_permanent_dead_end_survives_an_expired_cooldown(tmp_path, monkeypatch):
    """The one case where the two implementations disagree.

    A clock-based cooldown releases the address once ``cooldown_until`` passes,
    which is what produced one burned OTP every window.  The dead end must not be
    released by the clock.
    """
    import json
    import time

    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {"relogin_cooldown_seconds": 300}})
    past = int(time.time()) - 10_000
    path = account_recovery._relogin_guard_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dead@example.com": {
                    "failure_class": "account",
                    "dead_end": "account_exists_login_loop",
                    "permanent": True,
                    "cooldown_until": past,
                    "updated_at": past,
                }
            }
        ),
        encoding="utf-8",
    )

    assert account_recovery._relogin_cooldown_active({"email": "dead@example.com"}) is True


def test_an_expired_cooldown_without_a_dead_end_is_released(tmp_path, monkeypatch):
    """Negative control for the test above: the clock must still work."""
    import json
    import time

    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {"relogin_cooldown_seconds": 300}})
    past = int(time.time()) - 10_000
    path = account_recovery._relogin_guard_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "flaky@example.com": {
                    "failure_class": "relogin_otp_failed",
                    "cooldown_until": past,
                    "updated_at": past,
                }
            }
        ),
        encoding="utf-8",
    )

    assert account_recovery._relogin_cooldown_active({"email": "flaky@example.com"}) is False


def test_the_dead_end_escalation_can_be_turned_back_into_a_cooldown(tmp_path, monkeypatch):
    """Operator escape hatch: with the flag off, the clock releases the address."""
    monkeypatch.setattr(
        account_recovery,
        "CFG",
        {"runtime": {"directory": str(tmp_path)}, "account_health": {"relogin_dead_end_permanent": False}},
    )
    account_recovery._record_relogin_failure("dead@example.com", ABOUT_YOU_DEAD_END)

    entry = _guard_entry(tmp_path)
    assert entry["dead_end"] == "account_exists_login_loop"
    assert "permanent" not in entry


def test_a_cooldown_pass_does_not_erase_the_permanent_marker(tmp_path, monkeypatch):
    """Once marked, the dead end must survive the very next batch.

    ``_record_relogin_failure`` replaces ``data[key]`` wholesale, so a later
    "cooldown" outcome written over it would silently drop ``permanent`` and
    restart the OTP burn.  Two gates guard that (the ``mode == "cooldown"``
    early return, plus the marker filter), which also means a single-point
    mutation of either gate is unobservable -- this test pins the outcome
    rather than either gate.
    """
    monkeypatch.setattr(account_recovery, "CFG", {"runtime": {"directory": str(tmp_path)}, "account_health": {}})
    account_recovery._record_relogin_failure("dead@example.com", ABOUT_YOU_DEAD_END)
    account_recovery._record_relogin_failure(
        "dead@example.com", {"ok": False, "mode": "cooldown", "error": "relogin_cooldown"}
    )

    entry = _guard_entry(tmp_path)
    assert entry.get("permanent") is True
    assert entry.get("dead_end") == "account_exists_login_loop"
    assert account_recovery._relogin_cooldown_active({"email": "dead@example.com"}) is True


def test_the_dead_end_switch_is_read_through_the_real_config_path():
    """End-to-end through ``CFG``, not a monkeypatched dict.

    ``CFG`` is a compatibility view over the workflow-scoped ``RuntimeConfig``,
    so setting ``account_health.relogin_dead_end_permanent`` has to actually
    reach ``_relogin_dead_end_permanent()``.  A switch that only works in tests
    is not a switch.
    """
    from sms_tool.config import runtime_config_scope

    with runtime_config_scope({"chatgpt": {}, "account_health": {"relogin_dead_end_permanent": False}}):
        assert account_recovery._relogin_dead_end_permanent() is False
    with runtime_config_scope({"chatgpt": {}, "account_health": {}}):
        assert account_recovery._relogin_dead_end_permanent() is True


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
        result = recovery_batch.refresh_local_quota_statuses(
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
        result = recovery_batch.refresh_local_quota_statuses(["ok@example.com"])

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
    result = recovery_batch._probe_liveness_with_retries(
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
        result = recovery_batch.refresh_local_quota_statuses(["browser@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(["browser@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(["plain@example.com"])

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
        result = recovery_batch.refresh_local_quota_statuses(["browser@example.com"])

    assert not result["ok"]
    assert result["results"][0]["probe"].get("browser_fallback") == "unavailable"
    probe.assert_called_once()


def test_recovery_proxy_candidates_orders_selected_then_affinity_then_direct():
    """The chain's own pick goes first, then the account's original exit, then
    (only when the operator allowed it) a direct exit."""
    with patch.object(account_recovery, "_recovery_allow_direct", return_value=True):
        got = account_recovery._recovery_proxy_candidates("http://affinity:1", "http://selected:2")
    assert got == ["http://selected:2", "http://affinity:1", None]

    with patch.object(account_recovery, "_recovery_allow_direct", return_value=False):
        got = account_recovery._recovery_proxy_candidates("http://affinity:1", "http://selected:2")
    assert got == ["http://selected:2", "http://affinity:1"]

    with patch.object(account_recovery, "_recovery_allow_direct", return_value=True):
        assert account_recovery._recovery_proxy_candidates("", "") == [None]


def test_recoverable_on_other_proxy_only_trusts_transport_evidence():
    """A different exit can fix a blocked exit; it cannot resurrect a dead
    session. Only non-2xx / transport markers may trigger the retry."""
    retry = account_recovery._recoverable_on_other_proxy
    assert retry({"ok": False, "last_status": "403"}) is True
    assert retry({"ok": False, "last_status": "429"}) is True
    assert retry({"ok": False, "error": "curl: (28) Operation timed out"}) is True
    # A 200 that carried no access token is a dead session, not a bad exit.
    assert retry({"ok": False, "last_status": "200"}) is False
    assert retry({"ok": False, "error": "auth_session_missing_access_token"}) is False
    assert retry({"ok": True}) is False
    assert retry({"ok": False, "terminal": True, "last_status": "403"}) is False


def test_relogin_retries_web_session_on_the_next_exit_before_burning_an_otp():
    """A blocked exit fails every strategy that goes through it. web_session is
    free, so it must be retried on the account's other exit before the chain
    reaches the OTP strategy -- that retry is what keeps a transient 403 from
    turning into a real OTP send."""
    seen = []

    def web_session(account, proxy=None, timeout=None):
        seen.append(proxy)
        if len(seen) == 1:
            return {
                "ok": False,
                "mode": "web_session",
                "error": "auth_session_missing_access_token",
                "last_status": "403",
                "http_statuses": ["403", "403"],
            }
        return {"ok": True, "mode": "web_session", "persisted": True}

    with (
        patch.object(account_recovery, "_recovery_allow_direct", return_value=False),
        patch.object(account_recovery, "resolve_account_proxy", return_value="http://affinity:1"),
        patch.object(account_recovery, "_select_recovery_proxy", return_value=("http://selected:2", [])),
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "error": "missing_refresh_token"},
        ),
        patch.object(account_recovery, "relogin_web_session_account", side_effect=web_session),
        patch.object(account_recovery, "relogin_chatgpt_email_account") as otp,
    ):
        result = account_recovery.relogin_codex_account({"email": "retry@example.com"}, mode="auto")

    assert result["ok"] is True
    assert result["proxy_index"] == 1
    assert seen == ["http://selected:2", "http://affinity:1"]
    otp.assert_not_called()


def test_relogin_does_not_retry_web_session_when_the_session_is_dead():
    """Negative control for the retry above: the same strategy with
    ``last_status`` 200 must not spend a second exit, otherwise the retry would
    fire unconditionally and the discriminator would not be what gates it."""
    seen = []

    def web_session(account, proxy=None, timeout=None):
        seen.append(proxy)
        return {
            "ok": False,
            "mode": "web_session",
            "error": "auth_session_missing_access_token",
            "last_status": "200",
        }

    with (
        patch.object(account_recovery, "_recovery_allow_direct", return_value=False),
        patch.object(account_recovery, "resolve_account_proxy", return_value="http://affinity:1"),
        patch.object(account_recovery, "_select_recovery_proxy", return_value=("http://selected:2", [])),
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "error": "missing_refresh_token"},
        ),
        patch.object(account_recovery, "relogin_web_session_account", side_effect=web_session),
        patch.object(
            account_recovery, "relogin_chatgpt_email_account", return_value={"ok": False, "error": "otp_failed"}
        ),
        patch.object(
            account_recovery, "relogin_local_codex_account", return_value={"ok": False, "error": "codex_failed"}
        ),
        patch.object(
            account_recovery,
            "relogin_browser_session_account",
            return_value={"ok": False, "error": "browser_failed"},
        ),
    ):
        result = account_recovery.relogin_codex_account({"email": "dead@example.com"}, mode="auto")

    assert result["ok"] is False
    assert seen == ["http://selected:2"]


def test_relogin_never_retries_otp_strategies_across_exits():
    """Retrying an OTP strategy on another exit would mail the same mailbox a
    second time, so the cross-exit retry is limited to the free strategies.

    The OTP failure is deliberately transport-shaped (``last_status`` 403): with
    a non-transport failure the retry gate alone would suppress the second call,
    and the test would pass even if OTP strategies were listed as retryable.
    """
    otp_calls = []

    def otp(account, proxy=None, timeout=None):
        otp_calls.append(proxy)
        return {
            "ok": False,
            "error": "existing_login_otp_validate",
            "last_status": "403",
        }

    with (
        patch.object(account_recovery, "_recovery_allow_direct", return_value=False),
        patch.object(account_recovery, "resolve_account_proxy", return_value="http://affinity:1"),
        patch.object(account_recovery, "_select_recovery_proxy", return_value=("http://selected:2", [])),
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "error": "missing_refresh_token"},
        ),
        patch.object(
            account_recovery,
            "relogin_web_session_account",
            return_value={"ok": False, "error": "web_session_probe_failed:401"},
        ),
        patch.object(account_recovery, "relogin_chatgpt_email_account", side_effect=otp),
        patch.object(
            account_recovery, "relogin_local_codex_account", return_value={"ok": False, "error": "codex_failed"}
        ),
        patch.object(
            account_recovery,
            "relogin_browser_session_account",
            return_value={"ok": False, "error": "browser_failed"},
        ),
    ):
        account_recovery.relogin_codex_account({"email": "otp@example.com"}, mode="auto")

    assert otp_calls == ["http://selected:2"]


def test_auto_chain_gives_web_session_a_workable_but_bounded_budget():
    """The old 30s cap was shorter than a single blocked-exit retry window (which
    is how a transient 403 became an OTP send); an unbounded budget makes a dead
    exit expensive. The budget must stay inside a known band."""
    budgets = []

    def web_session(account, proxy=None, timeout=None):
        budgets.append(timeout)
        return {"ok": False, "error": "auth_session_missing_access_token", "last_status": "200"}

    def run(timeout):
        with (
            patch.object(account_recovery, "_recovery_allow_direct", return_value=False),
            patch.object(account_recovery, "resolve_account_proxy", return_value="http://affinity:1"),
            patch.object(account_recovery, "_select_recovery_proxy", return_value=("http://selected:2", [])),
            patch.object(
                account_recovery,
                "relogin_refresh_token_account",
                return_value={"ok": False, "error": "missing_refresh_token"},
            ),
            patch.object(account_recovery, "relogin_web_session_account", side_effect=web_session),
            patch.object(
                account_recovery, "relogin_chatgpt_email_account", return_value={"ok": False, "error": "otp"}
            ),
            patch.object(
                account_recovery, "relogin_local_codex_account", return_value={"ok": False, "error": "codex"}
            ),
            patch.object(
                account_recovery, "relogin_browser_session_account", return_value={"ok": False, "error": "browser"}
            ),
        ):
            account_recovery.relogin_codex_account({"email": "budget@example.com"}, mode="auto", timeout=timeout)

    run(30)
    run(600)

    assert budgets[0] == 45, "a short caller timeout must still leave a workable floor"
    assert budgets[1] == 60, "the budget must stay bounded so a dead exit is cheap"


def test_relogin_hands_the_stored_password_to_the_login_probe():
    """The lane probes before spending an email code, so it needs the password.

    A positive probe verdict is only actionable if there is a password to
    submit; without this the recovery lane would stop at "the account has a
    password we cannot submit" instead of logging in.  ``registration`` binds
    the symbol at call time (it is imported inside the function), so patching
    that module reaches the call site.
    """
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "error": "existing_login_password_verify_failed:401"}

    with (
        patch("sms_tool.codex_oauth._mailbox_from_data", return_value=Mock()),
        patch("sms_tool.auth_headers.select_auth_fingerprint"),
        patch("sms_tool.sentinel_tokens._set_oai_did_cookie"),
        patch("sms_tool.http_client.request_with_retry", return_value=Mock(status_code=200)),
        patch("sms_tool.auth_flow._json_or_raw", return_value={"csrfToken": "csrf"}),
        patch(
            "sms_tool.accounts.account_recovery._stored_registration_password",
            return_value="StoredPass123",
        ),
        patch("sms_tool.registration._login_existing_account_with_email_otp", side_effect=fake_login),
    ):
        account_recovery.relogin_chatgpt_email_account({"email": "ok@example.com"})

    assert captured["password"] == "StoredPass123"


# ---------------------------------------------------------------------------
# Repeat-failure backoff: an unchanged shape means the window bought nothing.
# ---------------------------------------------------------------------------

# The OTP body as it actually arrives: 200 characters of it, which is why the
# ``code`` field is usually cut off and ``status`` is the field left to compare.
_OTP_409 = (
    'existing_login_otp_validate:{"endpoint": "/api/accounts/email-otp/validate", "status": 409,'
    ' "body": {"error": {"message": "Your sign-in session is no longer valid. Please start over'
    ' to continue.", "type": "invalid_request_error", "code": "invalid_state"}}'
)
_OTP_401 = (
    'existing_login_otp_validate:{"endpoint": "/api/accounts/email-otp/validate", "status": 401,'
    ' "body": {"error": {"message": "Login failed.", "code": "login_failed"}}'
)


def _relogin_all_methods_failed(otp_error=_OTP_409):
    """The real ``results[0].relogin`` recorded for ``elms-dopey.8t+oai02``.

    Six attempts, all failed (``runtime/account_liveness_batches/
    65f6f49ca967464fa614419537ba853d.json``, 2026-09-14 16:04).
    """
    return {
        "ok": False,
        "mode": "auto",
        "error": "all_relogin_methods_failed",
        "attempts": [
            {"ok": False, "mode": "oauth_refresh_token", "error": "missing_refresh_token", "skipped": True},
            {"ok": False, "mode": "web_session", "error": "auth_session_missing_access_token", "last_status": "403"},
            {"ok": False, "mode": "web_session", "error": "web_session_access_token_probe_failed:401", "proxy_index": 1},
            {"ok": False, "mode": "chatgpt_email_otp", "error": otp_error},
            {"ok": False, "mode": "codex_oauth_pkce", "error": "passwordless_email_otp_poll_timeout"},
            {"ok": False, "mode": "browser_session", "error": "page_state"},
        ],
    }


def _patch_health_cfg(tmp_path, monkeypatch, **health):
    monkeypatch.setattr(
        account_recovery,
        "CFG",
        {"runtime": {"directory": str(tmp_path)}, "account_health": health},
    )


def test_a_repeated_relogin_failure_takes_the_long_window(tmp_path, monkeypatch):
    """The bug: an identical retry 45 minutes later burned another OTP.

    Measured 2026-09-14: ``elms-dopey.8t+oai02`` was judged dead at 16:04 and
    re-run at 17:49 with a byte-identical failure, because the ordinary 300s
    window had long since expired.  The unchanged shape is what says the retry
    bought no information, so the next window has to be longer.
    """
    _patch_health_cfg(tmp_path, monkeypatch, relogin_cooldown_seconds=300, relogin_repeat_cooldown_seconds=21600)

    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())
    first = _guard_entry(tmp_path, "dead@example.com")
    assert first["cooldown_until"] - first["updated_at"] == 300
    assert "repeat_failure" not in first

    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())
    second = _guard_entry(tmp_path, "dead@example.com")
    assert second["cooldown_until"] - second["updated_at"] == 21600
    assert second["repeat_failure"] is True


def test_a_changed_relogin_failure_falls_back_to_the_ordinary_window(tmp_path, monkeypatch):
    """Negative control: fixing the cause must restore fast retries.

    Without this the long window would be a one-way ratchet, and an account
    whose blocker had been repaired would stay parked for six hours.
    """
    _patch_health_cfg(tmp_path, monkeypatch, relogin_cooldown_seconds=300, relogin_repeat_cooldown_seconds=21600)

    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())
    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())

    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed(_OTP_401))
    third = _guard_entry(tmp_path, "dead@example.com")
    assert third["cooldown_until"] - third["updated_at"] == 300
    assert "repeat_failure" not in third


def test_the_shape_ignores_per_attempt_json_bodies():
    """Raw ``error`` text can never be compared: the OTP body carries ids.

    ``session_id`` and the message differ on every attempt, so a fingerprint
    built from the raw string would report "changed" for an identical failure
    and the long window would never trigger at all.

    Two probes, because they land in different places: ``session_id`` sits past
    the 80-character head and ``endpoint`` sits inside it.  Only varying the
    first would pass even with the body kept whole -- the first version of this
    test did exactly that and let ``runtime/_mut_relogin_repeat_and_dump_diag.py``
    mutant M3 survive.
    """
    plain = _relogin_all_methods_failed()
    past_the_head = _relogin_all_methods_failed(
        _OTP_409.replace('"code": "invalid_state"', '"code": "invalid_state", "session_id": "abc123"')
    )
    inside_the_head = _relogin_all_methods_failed(
        _OTP_409.replace('"/api/accounts/email-otp/validate"', '"/api/accounts/email-otp/validate?v=2"')
    )

    assert account_recovery._relogin_failure_shape(plain) == account_recovery._relogin_failure_shape(past_the_head)
    assert account_recovery._relogin_failure_shape(plain) == account_recovery._relogin_failure_shape(inside_the_head)


def test_the_shape_separates_two_different_otp_statuses():
    """Dropping the whole JSON body is not enough -- 409 is not 401.

    ``runtime/_probe_relogin_repeat_cooldown.py`` caught this: the first version
    cut ``error`` at the first ``{``, so a 409 turning into a 401 produced the
    same fingerprint and kept the six-hour window for a *different* failure.
    """
    shape_409 = account_recovery._relogin_failure_shape(_relogin_all_methods_failed(_OTP_409))
    shape_401 = account_recovery._relogin_failure_shape(_relogin_all_methods_failed(_OTP_401))

    assert shape_409 != shape_401
    assert "status=409" in shape_409
    assert "status=401" in shape_401


def test_a_successful_relogin_clears_the_recorded_shape(tmp_path, monkeypatch):
    """A stale shape would make the next transient failure look like a repeat."""
    import json

    _patch_health_cfg(tmp_path, monkeypatch)
    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())

    account_recovery._clear_relogin_failure("dead@example.com")

    data = json.loads(account_recovery._relogin_guard_path().read_text(encoding="utf-8"))
    assert "dead@example.com" not in data


def test_clearing_one_address_leaves_the_others(tmp_path, monkeypatch):
    """``data.clear()`` instead of ``pop(key)`` would wipe every guard entry."""
    import json

    _patch_health_cfg(tmp_path, monkeypatch)
    account_recovery._record_relogin_failure("a@example.com", _relogin_all_methods_failed())
    account_recovery._record_relogin_failure("b@example.com", _relogin_all_methods_failed())

    account_recovery._clear_relogin_failure("a@example.com")

    data = json.loads(account_recovery._relogin_guard_path().read_text(encoding="utf-8"))
    assert "a@example.com" not in data
    assert "b@example.com" in data


def test_clearing_an_unknown_address_keeps_the_other_entries(tmp_path, monkeypatch):
    """Negative control: the success path runs for every recovered account."""
    import json

    _patch_health_cfg(tmp_path, monkeypatch)
    account_recovery._record_relogin_failure("dead@example.com", _relogin_all_methods_failed())

    account_recovery._clear_relogin_failure("never@example.com")

    data = json.loads(account_recovery._relogin_guard_path().read_text(encoding="utf-8"))
    assert "dead@example.com" in data


def test_the_repeat_window_switch_is_read_through_the_real_config_path():
    """End-to-end through ``CFG``, not a monkeypatched dict.

    Same contract as ``_relogin_dead_end_permanent``: a switch that only works
    in tests is not a switch.
    """
    from sms_tool.config import runtime_config_scope

    with runtime_config_scope({"chatgpt": {}, "account_health": {"relogin_repeat_cooldown_seconds": 60}}):
        assert account_recovery._relogin_repeat_cooldown_seconds() == 60
    with runtime_config_scope({"chatgpt": {}, "account_health": {}}):
        assert account_recovery._relogin_repeat_cooldown_seconds() == 21600

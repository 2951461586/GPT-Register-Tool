import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import storage
from sms_tool.batch_runner import run_batch_impl
from sms_tool.commands.registration import (
    RegistrationCommandContext,
    persist_registration_result,
    save_registration_results,
)
from sms_tool.session_builder import build_session_file
from sms_tool.config import current_config_data


def _args(batch_id="hot-test"):
    return SimpleNamespace(
        registration_batch_id=batch_id,
        buy_remail_mailbox=False,
        remail_service_mode=None,
        check_promotion_after_registration=False,
        import_cpa=False,
        workers=2,
        proxy=None,
        refresh_timeout=20,
    )


def _result(email):
    return {"success": True, "email": email, "access_token": f"at-{email}"}


def _context(cfg, upsert):
    return RegistrationCommandContext(
        proxy_pool_values=lambda _args: [],
        load_mailbox_pool=lambda _args: [],
        run_batch=run_batch_impl,
        run_email=lambda **_kwargs: {},
        build_session_file=build_session_file,
        save_results=lambda *_args, **_kwargs: {},
        check_registered_promotions=lambda *_args, **_kwargs: {},
        import_registered_accounts=lambda *_args, **_kwargs: None,
        registration_phone_pool=lambda _args: None,
        upsert_account=upsert,
        database_path=lambda: str(cfg["storage"]["sqlite_path"]),
        runtime_file=lambda name: Path(cfg["storage"]["sqlite_path"]).parent / name,
        runtime_config=cfg,
    )


def _runtime_cfg(db_path):
    def thaw(value):
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, list):
            return [thaw(item) for item in value]
        return value

    cfg = thaw(current_config_data())
    cfg["storage"] = dict(cfg.get("storage") or {})
    cfg["storage"]["sqlite_path"] = str(db_path)
    cfg["output"] = dict(cfg.get("output") or {})
    cfg["output"]["filename_pattern"] = "session_{email}_{timestamp}.json"
    return cfg


def test_batch_callback_persists_before_other_future_finishes(tmp_path):
    barrier = threading.Barrier(2)
    release_slow = threading.Event()
    slow_finished = threading.Event()
    callback_observed = []

    def run_email(*, mailbox, **_kwargs):
        barrier.wait(timeout=5)
        if mailbox.email == "slow@example.com":
            release_slow.wait(timeout=5)
            slow_finished.set()
        return _result(mailbox.email)

    def on_result(index, result):
        if result["email"] == "quick@example.com":
            callback_observed.append(not slow_finished.is_set())
            release_slow.set()

    with patch("sms_tool.batch_runner.CFG", {"email_registration": {}}):
        results = run_batch_impl(
            count=2,
            workers=2,
            mailboxes=[
                SimpleNamespace(email="quick@example.com"),
                SimpleNamespace(email="slow@example.com"),
            ],
            run_email_func=run_email,
            on_result=on_result,
        )

    assert callback_observed == [True]
    assert {item["email"] for item in results} == {"quick@example.com", "slow@example.com"}


def test_hot_persistence_is_visible_in_sqlite_and_finalization_is_idempotent(tmp_path):
    db_path = tmp_path / "accounts.sqlite3"
    cfg = _runtime_cfg(db_path)
    upsert_calls = []

    def upsert(data, *, json_path):
        upsert_calls.append(data["email"])
        return storage.upsert_account(data, json_path=json_path, runtime_config=cfg)

    ctx = _context(cfg, upsert)
    barrier = threading.Barrier(2)
    release_slow = threading.Event()
    callback_db_visible = []

    def run_email(*, mailbox, **_kwargs):
        barrier.wait(timeout=5)
        if mailbox.email == "slow@example.com":
            release_slow.wait(timeout=5)
        return _result(mailbox.email)

    args = _args()

    def on_result(_index, result):
        persist_registration_result(
            args,
            result,
            tmp_path,
            ctx,
            pipeline_timing={"total_seconds": 0.1},
        )
        if result["email"] == "quick@example.com":
            callback_db_visible.append(
                storage.get_account_record("quick@example.com", runtime_config=cfg)
            )
            assert list(tmp_path.glob("session_quick@example.com_*.json"))
            release_slow.set()

    with patch("sms_tool.batch_runner.CFG", {"email_registration": {}}):
        results = run_batch_impl(
            count=2,
            workers=2,
            mailboxes=[
                SimpleNamespace(email="quick@example.com"),
                SimpleNamespace(email="slow@example.com"),
            ],
            run_email_func=run_email,
            on_result=on_result,
        )
    report = save_registration_results(
        args,
        results,
        effective_count=2,
        base_dir=tmp_path,
        pipeline_started=time.time() - 1,
        mailbox_seconds=0,
        register_seconds=1,
        ctx=ctx,
    )

    assert callback_db_visible and callback_db_visible[0]["email"] == "quick@example.com"
    assert report["session_saved"] == 2
    assert report["db_saved"] == 2
    assert sorted(upsert_calls) == ["quick@example.com", "slow@example.com"]
    assert len(list(tmp_path.glob("session_*.json"))) == 2


def test_failed_hot_persistence_is_retried_without_stopping_batch(tmp_path):
    cfg = _runtime_cfg(tmp_path / "accounts.sqlite3")
    calls = {"count": 0}

    def upsert(data, *, json_path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temporary database failure")
        return True

    ctx = _context(cfg, upsert)
    args = _args("retry-test")
    first = _result("retry@example.com")
    outcome = persist_registration_result(args, first, tmp_path, ctx)
    assert outcome["status"] == "failed"

    second = _result("other@example.com")
    persist_registration_result(args, second, tmp_path, ctx)
    report = save_registration_results(
        args,
        [first, second],
        effective_count=2,
        base_dir=tmp_path,
        pipeline_started=time.time() - 1,
        mailbox_seconds=0,
        register_seconds=1,
        ctx=ctx,
    )

    assert report["session_saved"] == 2
    assert report["db_saved"] == 2
    assert calls["count"] == 3

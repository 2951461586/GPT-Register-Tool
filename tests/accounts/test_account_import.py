"""Pin the exported-CPA importer (``accounts/account_import.py``).

The importer is the inverse of ``session_converter``'s CPA output, and the three
rules it must never break are all failures a naive version would ship:

* an older file must not replace a newer access token (``exp`` decides, not file
  order or ``last_refresh``);
* a synthetic ``id_token`` must not be stored as a credential;
* one unusable item must not abort the other 199.

Everything here is offline: no provider is called, only the session files and
``accounts.sqlite3`` in a temporary directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms_tool.accounts.account_import import (
    AccountImportError,
    import_account_json,
    load_export_items,
    merge_items,
    session_path_for,
    token_expiry,
)
from sms_tool.storage import init_database, list_account_records

import base64


def jwt(exp: int, sub: str = "user-1") -> str:
    """A JWT-shaped string: ``parse_jwt_payload`` only needs the middle segment."""

    def enc(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'sub': sub})}.sig"


def cpa_item(**overrides) -> dict:
    item = {
        "type": "codex",
        "email": "user@icloud.com",
        "account_id": "acc-1",
        "access_token": jwt(2_000_000_000),
        "refresh_token": "rt-1",
        "session_token": "st-1",
        "id_token": jwt(2_000_000_000, "id"),
        "plan_type": "free",
        "expired": "2033-05-18T03:33:20Z",
        "last_refresh": "2026-11-19T00:00:00Z",
    }
    item.update(overrides)
    return item


# ─────────────────────────── document shapes ───────────────────────────


def test_a_single_object_is_one_item(tmp_path):
    path = tmp_path / "account-cpa-1.json"
    path.write_text(json.dumps(cpa_item()), encoding="utf-8")
    items = load_export_items(path)
    assert len(items) == 1 and items[0]["email"] == "user@icloud.com"


def test_an_array_and_an_accounts_document_both_load(tmp_path):
    array = tmp_path / "array.json"
    array.write_text(json.dumps([cpa_item(), cpa_item(email="b@x.com")]), encoding="utf-8")
    document = tmp_path / "doc.json"
    document.write_text(json.dumps({"accounts": [cpa_item()]}), encoding="utf-8")
    assert len(load_export_items(array)) == 2
    assert len(load_export_items(document)) == 1


def test_an_unreadable_or_wrong_document_raises_the_stable_code(tmp_path):
    with pytest.raises(AccountImportError, match="account_export_unreadable"):
        load_export_items(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(AccountImportError, match="account_export_unreadable"):
        load_export_items(broken)
    scalar = tmp_path / "scalar.json"
    scalar.write_text("42", encoding="utf-8")
    with pytest.raises(AccountImportError, match="account_export_invalid_json"):
        load_export_items(scalar)


# ─────────────────────────── merge rules ───────────────────────────


def test_the_newer_token_wins_regardless_of_file_order():
    older = cpa_item(access_token=jwt(1_700_000_000), last_refresh="2026-11-19T00:00:00Z")
    newer = cpa_item(access_token=jwt(1_800_000_000), last_refresh="2020-01-01T00:00:00Z")
    kept, skipped = merge_items([newer, older])
    assert token_expiry(kept["user@icloud.com"]["access_token"]) == 1_800_000_000
    assert skipped == [{"email": "user@icloud.com", "reason": "older_token"}]

    kept_reverse, _ = merge_items([older, newer])
    assert token_expiry(kept_reverse["user@icloud.com"]["access_token"]) == 1_800_000_000


def test_a_synthetic_id_token_is_not_stored():
    kept, _ = merge_items([cpa_item(id_token=jwt(2_000_000_000, "synthetic"), id_token_synthetic=True)])
    assert kept["user@icloud.com"]["id_token"] == ""


def test_items_that_cannot_be_used_report_a_reason_instead_of_aborting():
    items = [
        cpa_item(email=""),
        cpa_item(email="off@x.com", disabled=True),
        cpa_item(email="notoken@x.com", access_token="", session_token=""),
        cpa_item(email="ok@x.com"),
    ]
    kept, skipped = merge_items(items)
    assert list(kept) == ["ok@x.com"]
    assert skipped == [
        {"email": "", "reason": "missing_email"},
        {"email": "off@x.com", "reason": "disabled"},
        {"email": "notoken@x.com", "reason": "missing_token"},
    ]


def test_a_session_token_alone_is_enough():
    kept, skipped = merge_items([cpa_item(access_token="", session_token="st-only")])
    assert skipped == []
    assert kept["user@icloud.com"]["session_token"] == "st-only"


# ─────────────────────────── write path ───────────────────────────


def test_session_path_sanitises_plus_and_unsafe_characters(tmp_path):
    path = session_path_for("a+b@x.com", "session_{email}_{timestamp}.json", tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("session_ab@x.com_")
    unsafe = session_path_for("we ird/../../x@y.com", "session_{email}_{timestamp}.json", tmp_path)
    assert unsafe.parent == tmp_path, "a crafted email must not escape the sessions directory"
    assert "/" not in unsafe.name and "\\" not in unsafe.name


def test_import_writes_the_session_file_and_upserts_the_row(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    cfg = {"chatgpt": {}, "storage": {"sqlite_path": str(db)}}
    init_database(runtime_config=cfg)
    export = tmp_path / "account-cpa-20261119.json"
    export.write_text(json.dumps([cpa_item(), cpa_item(email="second@icloud.com")]), encoding="utf-8")

    report = import_account_json(export, session_dir=tmp_path / "sessions", runtime_config=cfg)

    assert report["ok"] is True
    assert report["total"] == 2
    assert report["imported"] == 2
    assert report["db_saved"] == 2
    assert report["skipped"] == []
    rows = list_account_records(runtime_config=cfg)
    assert sorted(row["email"] for row in rows) == ["second@icloud.com", "user@icloud.com"]
    assert {row["register_method"] for row in rows} == {"import"}
    for path in report["session_paths"]:
        session = json.loads(Path(path).read_text(encoding="utf-8"))
        assert session["register_method"] == "import"
        assert session["status"] == "registered"
        assert session["access_token"]


def test_reimporting_an_older_export_does_not_overwrite_the_newer_token(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    cfg = {"chatgpt": {}, "storage": {"sqlite_path": str(db)}}
    init_database(runtime_config=cfg)
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps(cpa_item(access_token=jwt(1_900_000_000))), encoding="utf-8")
    older = tmp_path / "older.json"
    older.write_text(json.dumps(cpa_item(access_token=jwt(1_600_000_000))), encoding="utf-8")

    import_account_json(newer, session_dir=tmp_path / "sessions", runtime_config=cfg)
    report = import_account_json(older, session_dir=tmp_path / "sessions", runtime_config=cfg)

    assert report["imported"] == 0
    assert report["skipped"] == [{"email": "user@icloud.com", "reason": "newer_token_in_store"}]
    rows = list_account_records(runtime_config=cfg)
    assert [row["access_token"] for row in rows] == [jwt(1_900_000_000)]

"""``upsert_account`` must not let a blank round erase a durable fact.

The bug this pins: every column was written as ``col=excluded.col``, so any
later round that did not happen to carry a value blanked it.  A relogin round
carries tokens and nothing else, which means it used to erase ``password``,
``totp_secret`` (issued once, unrecoverable -- losing it locks 2FA forever) and
the whole ``mailbox_*`` / ``purchase_*`` family.

Two directions have to hold at once, and the second is the one that is easy to
get wrong:

1. a blank incoming value must NOT overwrite a stored non-blank one, and
2. a *derived* column must stay clearable -- ``error`` in particular, because
   "this round succeeded" is expressed by writing an empty ``error``.  A blanket
   "blank never overwrites" would silently break that, which is why the guard is
   a per-column table (``store.accounts._COALESCE_IF_BLANK``) rather than a rule.

Reference for the mechanism: the third-party ``gpt-auto-register-simulated-qc-source``
project coalesces exactly ``password`` / ``totp_secret`` in its ``save_registered``
(``webui/db.py:595-620``).  Only the mechanism is borrowed -- that project is
AGPL-3.0 and this repository carries no licence, so no code was copied.  The same
shape already existed here in ``scripts/batch_enable_2fa.persist_twofa``, which
coalesces ``totp_secret`` / ``twofa_enrolled_at`` in raw SQL.
"""

from __future__ import annotations

import json
from pathlib import Path

from sms_tool.storage import get_account_record, upsert_account

EMAIL = "coalesce@example.test"


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


def _full_registration(tmp_path: Path) -> dict:
    """A round-1 payload that populates every guarded column."""
    return {
        "email": EMAIL,
        "success": True,
        "access_token": "AT1",
        "refresh_token": "RT1",
        "password": "pw-round1",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "mailbox": {
            "provider": "remail",
            "source": "remail_api",
            "token": "MB1",
            "purchase_id": "P-1",
            "project_name": "proj-1",
            "price": "0.5",
            "purchase_total_cost": "12.5",
            "balance_after": "7.5",
        },
        "registration_country": "VN",
        "batch_id": "batch-1",
        "status": "registered",
    }


def _token_only_round(**extra) -> dict:
    """A later round: fresh tokens, nothing else."""
    payload = {"email": EMAIL, "success": True, "access_token": "AT2", "status": "registered"}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------
# 1. A blank round must not erase the durable facts
# --------------------------------------------------------------------------

def test_blank_round_keeps_the_one_shot_credentials(tmp_path):
    """``password`` and ``totp_secret`` are OpenAI-side persistent state.

    A re-run that lands on the passwordless branch never sets a password, and a
    re-run of an already-enrolled account never re-binds 2FA -- so both arrive
    blank.  ``totp_secret`` is the severe case: it is issued once and the server
    cannot return it, so erasing it locks that account's 2FA permanently.
    """
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    assert upsert_account(_token_only_round(), runtime_config=config)

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["password"] == "pw-round1"
    assert record["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert record["twofa_enrolled_at"] > 0


def test_blank_round_keeps_the_mailbox_and_purchase_facts(tmp_path):
    """The mailbox is consumed at registration and absent from every later round.

    This is the highest-impact guard: ``account_recovery._has_relogin_material()``
    treats ``mailbox_token`` / ``mailbox_provider`` as recovery material, so
    erasing them makes a recoverable account look unrecoverable and it gets
    marked 掉号 on the next pass.
    """
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    assert upsert_account(_token_only_round(), runtime_config=config)

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["mailbox_provider"] == "remail"
    assert record["mailbox_source"] == "remail_api"
    assert record["mailbox_token"] == "MB1"
    assert record["purchase_id"] == "P-1"
    assert record["project_name"] == "proj-1"
    assert record["price"] == "0.5"
    assert record["purchase_total_cost"] == "12.5"
    assert record["balance_after"] == "7.5"


def test_blank_round_keeps_the_registration_provenance(tmp_path):
    """``registration_country`` feeds ``billing_country_for()``, which falls back
    to "US" when empty -- so erasing it aims the eligibility probe at the wrong
    catalog.  ``json_path`` is the pointer to the session file."""
    config = _config(tmp_path)
    session = tmp_path / "session.json"
    assert upsert_account(_full_registration(tmp_path), json_path=str(session), runtime_config=config)
    assert upsert_account(_token_only_round(), runtime_config=config)

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["registration_country"] == "VN"
    assert record["batch_id"] == "batch-1"
    assert record["json_path"] == str(session)


def test_the_preserved_mailbox_still_counts_as_recovery_material(tmp_path):
    """The end-to-end consequence, not just the column value."""
    from sms_tool.accounts.account_recovery import _has_relogin_material

    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    assert upsert_account(_token_only_round(), runtime_config=config)

    record = get_account_record(EMAIL, runtime_config=config)
    assert _has_relogin_material(record) is True


# --------------------------------------------------------------------------
# 2. The guard must stay narrow -- these are the anti-over-guard tests
# --------------------------------------------------------------------------

def test_a_blank_error_still_clears(tmp_path):
    """``error`` is deliberately NOT guarded.

    "This round succeeded" is expressed by writing an empty ``error``, and
    ``tests/test_storage_dedup.py::test_upsert_reuses_existing_email_case_insensitively``
    already pins it.  If this ever fails, the guard has been widened into a
    blanket rule and recovery/failure signalling is broken.
    """
    config = _config(tmp_path)
    assert upsert_account({"email": EMAIL, "success": False, "error": "first"}, runtime_config=config)
    assert get_account_record(EMAIL, runtime_config=config)["error"] == "first"

    assert upsert_account({"email": EMAIL, "success": True, "access_token": "tok", "error": ""}, runtime_config=config)
    assert get_account_record(EMAIL, runtime_config=config)["error"] == ""


def test_tokens_still_overwrite_and_can_be_cleared(tmp_path):
    """A stale token is worse than a missing one, so tokens are not guarded.

    The mirror image of the durable columns: they are re-minted every round and
    the newest value always wins, including a blank one.
    """
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    assert upsert_account(_token_only_round(), runtime_config=config)

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["access_token"] == "AT2"
    assert record["refresh_token"] == ""

    assert upsert_account({"email": EMAIL, "success": False}, runtime_config=config)
    record = get_account_record(EMAIL, runtime_config=config)
    assert record["access_token"] == ""
    assert record["password"] == "pw-round1", "tokens clearing must not disturb the guard"


# --------------------------------------------------------------------------
# 3. Non-blank values still win, and the insert path is untouched
# --------------------------------------------------------------------------

def test_non_blank_values_still_overwrite(tmp_path):
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    assert upsert_account(
        {
            "email": EMAIL,
            "success": True,
            "password": "pw-round2",
            "totp_secret": "NEWSECRET",
            "mailbox": {"provider": "chatai", "token": "MB2"},
            "registration_country": "PH",
            "batch_id": "batch-2",
        },
        runtime_config=config,
    )

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["password"] == "pw-round2"
    assert record["totp_secret"] == "NEWSECRET"
    assert record["mailbox_provider"] == "chatai"
    assert record["mailbox_token"] == "MB2"
    assert record["registration_country"] == "PH"
    assert record["batch_id"] == "batch-2"


def test_a_first_write_with_blank_guarded_columns_still_inserts(tmp_path):
    """The guard is a merge rule, not an insert precondition.

    ``commands/registration.py`` writes the no-output-path case with
    ``json_path=""``; on a brand-new email there is no stored row to protect and
    the row must still be created.
    """
    config = _config(tmp_path)
    assert upsert_account(
        {"email": EMAIL, "success": True, "access_token": "at"},
        json_path="",
        runtime_config=config,
    )

    record = get_account_record(EMAIL, runtime_config=config)
    assert record["email"] == EMAIL
    assert record["access_token"] == "at"
    assert record["password"] == ""
    assert record["totp_secret"] == ""
    assert record["json_path"] == ""


def test_twofa_enrolled_at_follows_the_secret(tmp_path):
    """It must not be blanked while the secret survives, and must move with it."""
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)
    first = get_account_record(EMAIL, runtime_config=config)["twofa_enrolled_at"]
    assert first > 0

    # A blank round keeps both.
    assert upsert_account(_token_only_round(), runtime_config=config)
    assert get_account_record(EMAIL, runtime_config=config)["twofa_enrolled_at"] == first

    # A round that re-enrols moves both.
    assert upsert_account(
        {"email": EMAIL, "success": True, "totp_secret": "ANOTHERSECRET"}, runtime_config=config
    )
    record = get_account_record(EMAIL, runtime_config=config)
    assert record["totp_secret"] == "ANOTHERSECRET"
    assert record["twofa_enrolled_at"] >= first


# --------------------------------------------------------------------------
# 4. The raw_json limitation, stated so it is not mistaken for coverage
# --------------------------------------------------------------------------

def test_raw_json_is_still_rebuilt_from_the_safe_snapshot_whitelist(tmp_path):
    """This module guards COLUMNS.  ``raw_json`` is a separate contract.

    ``raw_json`` is re-serialized from ``AccountSessionModel.safe_snapshot()``,
    whose key list is closed -- so a key the round does not carry is dropped from
    the JSON even though its column is now protected.  The two failure modes are
    easy to confuse, so this test pins the *actual* split: the column survives,
    the JSON does not.

    The JSON side is guarded by
    ``tests/test_account_payment_eligibility.py::test_safe_snapshot_keeps_payment_capability``
    (add any new persisted key to the whitelist), not by this change.  Recorded
    here so the gap is visible instead of being mistaken for coverage.
    """
    config = _config(tmp_path)
    assert upsert_account(_full_registration(tmp_path), runtime_config=config)

    stored = json.loads(get_account_record(EMAIL, runtime_config=config)["raw_json"])
    assert stored["mailbox"]["provider"] == "remail"
    assert "payment_capability" in stored, "always emitted by safe_snapshot()"

    assert upsert_account(_token_only_round(), runtime_config=config)
    record = get_account_record(EMAIL, runtime_config=config)

    # The column is protected...
    assert record["mailbox_provider"] == "remail"
    # ...but raw_json is rebuilt from the whitelist, which this round cannot fill.
    assert json.loads(record["raw_json"])["mailbox"]["provider"] == ""

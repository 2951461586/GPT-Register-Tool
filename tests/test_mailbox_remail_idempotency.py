"""Regression tests for ReMail batch idempotency (audit batch-3 item #2).

Before the fix, ``mailbox_remail.py`` sent a fresh ``uuid.uuid4()`` as the
``Idempotency-Key`` on every batch order request, so a retried request created a
second order instead of being de-duplicated by the server (double charge). The key
must now be stable per order intent (sha256 of mode + supply + canonical payload).
"""
import re
from types import SimpleNamespace

import pytest

import sms_tool.mailbox_remail as m


def test_remail_idempotency_key_is_stable_and_distinct():
    payload = {"quantity": 2, "emailSuffix": "outlook.com", "projectId": 5}
    k1 = m._remail_idempotency_key("purchase", "private_first", payload)
    k2 = m._remail_idempotency_key("purchase", "private_first", payload)
    assert k1 == k2
    assert re.fullmatch(r"[0-9a-f]{64}", k1), "key must be a stable sha256 hex digest"
    # Different payload -> different key (no false collision between distinct orders).
    k3 = m._remail_idempotency_key("purchase", "private_first", {**payload, "quantity": 3})
    assert k3 != k1


def test_create_remail_batch_reuses_stable_key(monkeypatch):
    headers_seen = []

    def fake_request(method, path, auth=False, headers=None, params=None, json=None, timeout=None):
        headers_seen.append(headers or {})
        return [{"status": "succeeded", "order": {"email": "x@outlook.com"}}]

    monkeypatch.setattr(m, "_remail_request", fake_request)
    monkeypatch.setattr(m, "_recover_recent_remail_batch", lambda **kw: [])
    monkeypatch.setattr(m, "filter_dead_remail_mailboxes", lambda xs: xs)
    monkeypatch.setattr(m, "_mailbox_from_order", lambda order, service_mode=None: order)
    # Pin _order_options so the test does not depend on its internals.
    monkeypatch.setattr(
        m,
        "_order_options",
        lambda args, service_mode=None: (
            "purchase",
            "private_first",
            {"quantity": 2, "emailSuffix": "outlook.com", "projectId": 5},
        ),
    )

    args = SimpleNamespace(count=2, email_suffix="outlook.com", project_id=5)
    m._create_remail_mailboxes(args, service_mode="purchase")
    m._create_remail_mailboxes(args, service_mode="purchase")

    k1 = headers_seen[0].get("Idempotency-Key")
    k2 = headers_seen[1].get("Idempotency-Key")
    assert re.fullmatch(r"[0-9a-f]{64}", k1 or ""), (
        "Idempotency-Key must be a stable sha256, not a random uuid4"
    )
    assert k1 == k2, "same order intent must reuse the same Idempotency-Key (was uuid4 before the fix)"

from sms_tool.payment_reconciliation import reconcile_payment_result


def test_generic_reconciliation_returns_unknown_when_provider_result_is_ambiguous():
    result = reconcile_payment_result("momo", {"status": "processing"})
    assert result["classification"] == "unknown"
    assert result["requires_reconciliation"] is True
    assert result["retryable"] is False


def test_generic_reconciliation_accepts_conclusive_artifact():
    result = reconcile_payment_result("momo", {"status": "completed", "qr_data": "present"})
    assert result["classification"] == "conclusive"
    assert result["outcome"] == "succeeded"

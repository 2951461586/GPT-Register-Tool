"""Tests for ``sms_tool.accounts.wham_usage``.

The module was split out of ``account_liveness`` so the liveness module owns
only the probe. These tests pin the two things the split must preserve:

* the parser still accepts every upstream spelling of the two windows;
* ``account_liveness`` keeps re-exporting the names, so existing importers do
  not break.
"""

from __future__ import annotations

from sms_tool.accounts import account_liveness, wham_usage


def test_parse_reads_short_and_weekly_windows() -> None:
    body = {
        "usage": {
            "5h": {"used": 1200, "limit": 10000},
            "7d": {"remaining": 250, "limit": 1000},
        }
    }
    parsed = wham_usage.parse_wham_usage(body)
    assert parsed is not None
    assert parsed["5h"] == {"used": 1200, "limit": 10000, "remaining": 8800, "percent": 12.0}
    assert parsed["7d"] == {"used": 750, "limit": 1000, "remaining": 250, "percent": 75.0}


def test_parse_accepts_alternate_window_spellings() -> None:
    parsed = wham_usage.parse_wham_usage(
        {"five_hours": {"tokens_used": 5, "max": 50}, "weekly": {"consumed": 1, "cap": 2}}
    )
    assert parsed is not None
    assert set(parsed) == {"5h", "7d"}
    assert parsed["5h"]["percent"] == 10.0
    assert parsed["7d"]["percent"] == 50.0


def test_parse_carries_reset_timestamp() -> None:
    parsed = wham_usage.parse_wham_usage(
        {"5h": {"used": 1, "limit": 2, "resets_at": "2026-09-25T00:00:00Z"}}
    )
    assert parsed is not None
    assert parsed["5h"]["reset_at"] == "2026-09-25T00:00:00Z"


def test_parse_accepts_json_string_and_rejects_garbage() -> None:
    assert wham_usage.parse_wham_usage('{"5h": {"used": 1, "limit": 4}}') == {
        "5h": {"used": 1, "limit": 4, "remaining": 3, "percent": 25.0}
    }
    assert wham_usage.parse_wham_usage("not json") is None
    assert wham_usage.parse_wham_usage(None) is None
    assert wham_usage.parse_wham_usage({"nothing": True}) is None


def test_format_label_compacts_token_counts() -> None:
    usage = {
        "5h": {"used": 1200, "limit": 10000, "percent": 12.0},
        "7d": {"used": 2_500_000, "limit": 10_000_000, "percent": 25.0},
    }
    assert wham_usage.format_wham_usage_label(usage) == "5h: 1.2K/10.0K (12%) | 7d: 2.5M/10.0M (25%)"
    assert wham_usage.format_wham_usage_label(None) == ""
    assert wham_usage.format_wham_usage_label({}) == ""


def test_account_liveness_reexports_split_names() -> None:
    # The split must not break ``account_liveness.parse_wham_usage`` callers.
    assert account_liveness.parse_wham_usage is wham_usage.parse_wham_usage
    assert account_liveness.format_wham_usage_label is wham_usage.format_wham_usage_label

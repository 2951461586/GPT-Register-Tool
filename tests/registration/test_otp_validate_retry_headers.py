"""Both OTP-validate attempts must carry the same per-request header pair.

The 2026-10-23 HAR baseline shows ``email-otp/validate`` sending two identifiers
that the rest of the lane also sends:

* ``x-access-flow-invocation-id`` -- minted **per request** (send, validate and
  ``create_account`` each carry a different one);
* ``x-openai-document-navigation-id`` -- the **page-level** id, identical across
  those three requests.

The first validate passed both.  The wrong-code retry (``validate_email_otp``'s
"retrying latest mailbox code once" branch) used to pass neither, so the one
request whose job is recovery was also the one request that did not look like the
browser.  This pins the retry to the same shape without pinning *which* UUID it
is: a per-request id has to differ from the first call, the navigation id has to
be equal.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from sms_tool.registration.otp_stages import validate_email_otp


class _Runtime(SimpleNamespace):
    """The exact runtime surface ``validate_email_otp`` touches."""


def _workflow(*, first_code: str = "111111", retry_code: str = "222222") -> Any:
    first = {"error": {"code": "wrong_email_otp_code"}}
    second: dict[str, Any] = {"continue_url": "https://auth.openai.com/about-you"}
    validate = Mock(side_effect=[(False, first), (True, second)])

    operations = Mock()
    operations._validate_email_otp = validate
    operations._is_wrong_email_otp_code = Mock(return_value=True)
    operations._follow_continue_url = Mock(return_value=None)
    operations.otp_poll.subject_keywords = "verification code"

    runtime = _Runtime(
        session=object(),
        auth_base="https://auth.openai.com",
        base_headers={"oai-device-id": "did-1"},
        email_code=first_code,
        otp_data={},
        sentinel_data={},
        mailbox=object(),
        mailbox_service=SimpleNamespace(poll_otp=Mock(return_value=retry_code)),
        email_cfg={"otp_timeout": 300},
        auth_flow_started=1_700_000_000,
        proxy="",
        document_navigation_id="nav-2026-10-23",
    )
    return SimpleNamespace(r=operations, runtime=runtime, config={})


def test_the_retry_carries_a_fresh_invocation_id_and_the_same_navigation_id():
    workflow = _workflow()
    validate = workflow.r._validate_email_otp

    validate_email_otp(workflow)

    assert validate.call_count == 2, "the wrong-code branch must spend one retry"
    first, retry = validate.call_args_list

    assert first.kwargs["flow_invocation_id"]
    assert retry.kwargs["flow_invocation_id"]
    assert first.kwargs["flow_invocation_id"] != retry.kwargs["flow_invocation_id"], (
        "x-access-flow-invocation-id is per request; reusing it makes two requests share one identity"
    )
    assert first.kwargs["document_navigation_id"] == retry.kwargs["document_navigation_id"] == "nav-2026-10-23"

    # The retry is a different code on the same session -- not a different request shape.
    assert first.args[3] == "111111"
    assert retry.args[3] == "222222"
    assert first.args[:3] == retry.args[:3]

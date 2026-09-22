"""Direct stage-contract tests for ``RegistrationEmailWorkflow.probe_access_token``.

This stage was the only one of the 12 ``run()`` stages with **zero** direct
tests (coverage came only from full-run integration tests). The contract it must
hold, pinned here:

- no access token → the stage is a no-op (no probe, no checkpoint);
- probe HTTP 200 → checkpoint state ``at_probe_complete``;
- any other outcome → checkpoint state ``at_probe_transport_unknown`` (the
  "transport unknown" bucket stays resumable, it is NOT a terminal failure);
- the probe receives the workflow's proxy and config.
"""

from unittest.mock import Mock

import pytest

from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationStateMachine


def _workflow():
    return RegistrationEmailWorkflow(RegistrationStateMachine(lambda *_: None), operations=Mock())


def test_no_access_token_is_a_noop():
    workflow = _workflow()
    workflow.runtime.access_token = ""
    workflow._persist_checkpoint = Mock()
    workflow.probe_access_token()
    workflow.r._probe_registration_access_token.assert_not_called()
    workflow._persist_checkpoint.assert_not_called()


def test_http_200_marks_probe_complete():
    workflow = _workflow()
    workflow.runtime.access_token = "tok-abc"
    workflow.runtime.auth_body = {"k": "v"}
    workflow.runtime.proxy = "http://proxy"
    workflow.r._probe_registration_access_token.return_value = {"status_code": 200}
    workflow._persist_checkpoint = Mock()
    workflow.probe_access_token()
    workflow.r._probe_registration_access_token.assert_called_once_with(
        "tok-abc", {"k": "v"}, proxy="http://proxy", cfg=workflow.config
    )
    workflow._persist_checkpoint.assert_called_once_with("at_probe_complete")
    assert workflow.runtime.at_probe == {"status_code": 200}


@pytest.mark.parametrize("status", [401, 403, 0, None])
def test_non_200_marks_transport_unknown(status):
    workflow = _workflow()
    workflow.runtime.access_token = "tok-abc"
    workflow.runtime.auth_body = {}
    workflow.runtime.proxy = ""
    workflow.r._probe_registration_access_token.return_value = {"status_code": status}
    workflow._persist_checkpoint = Mock()
    workflow.probe_access_token()
    workflow._persist_checkpoint.assert_called_once_with("at_probe_transport_unknown")

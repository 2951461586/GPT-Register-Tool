from sms_tool.mailbox_errors import (
    MailboxEndpointUnavailableError,
    MailboxErrorDisposition,
    mailbox_error_disposition,
)
from sms_tool.providers.mailbox_graph import (
    MailboxAuthInvalidError,
    MailboxTokenExpiredError,
)


def test_mailbox_error_policy_classifies_terminal_failures():
    assert mailbox_error_disposition(
        MailboxEndpointUnavailableError(404)
    ) is MailboxErrorDisposition.ENDPOINT_UNAVAILABLE
    assert mailbox_error_disposition(
        MailboxAuthInvalidError("user@example.com")
    ) is MailboxErrorDisposition.AUTH_INVALID
    assert mailbox_error_disposition(
        MailboxTokenExpiredError("invalid_grant")
    ) is MailboxErrorDisposition.AUTH_INVALID


def test_mailbox_error_policy_keeps_transport_retryable():
    assert mailbox_error_disposition(
        TimeoutError("provider timed out")
    ) is MailboxErrorDisposition.RETRY

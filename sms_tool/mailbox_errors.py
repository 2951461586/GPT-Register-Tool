"""Provider-independent terminal mailbox errors understood by OTP pollers."""


class MailboxEndpointUnavailableError(RuntimeError):
    """The mailbox endpoint is missing; retry only after a bounded cooldown."""

    code = "mailbox_endpoint_unavailable"

    def __init__(self, status: int = 404):
        self.status = status
        super().__init__(f"{self.code}: HTTP {status}")

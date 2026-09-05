"""Mailbox provider implementations.

Provider implementations live in this package. The top-level ``mailbox_*``
modules are compatibility facades for existing callers.
"""

__all__ = [
    "mailbox_cfworker",
    "mailbox_gmail",
    "mailbox_graph",
    "mailbox_icloud_url",
    "mailbox_remail",
    "mailbox_smailr",
    "outlook_imap",
]

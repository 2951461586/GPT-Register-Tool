"""Compatibility facade for the Gmail mailbox provider."""
from .providers import mailbox_gmail as _impl
import sys
sys.modules[__name__] = _impl

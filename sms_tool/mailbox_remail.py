"""Compatibility facade for the ReMail mailbox provider."""
from .providers import mailbox_remail as _impl
import sys
sys.modules[__name__] = _impl

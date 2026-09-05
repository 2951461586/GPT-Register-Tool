"""Compatibility facade for the Microsoft Graph mailbox provider."""
from .providers import mailbox_graph as _impl
import sys
sys.modules[__name__] = _impl

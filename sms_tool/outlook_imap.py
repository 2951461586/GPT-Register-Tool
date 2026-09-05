"""Compatibility facade for the Outlook IMAP adapter."""
from .providers import outlook_imap as _impl
import sys
sys.modules[__name__] = _impl

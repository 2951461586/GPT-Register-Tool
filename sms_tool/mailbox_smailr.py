"""Compatibility facade for the Smailr mailbox provider."""
from .providers import mailbox_smailr as _impl
import sys
sys.modules[__name__] = _impl

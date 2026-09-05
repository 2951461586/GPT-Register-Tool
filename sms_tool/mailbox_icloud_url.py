"""Compatibility facade for the iCloud URL mailbox provider."""
from .providers import mailbox_icloud_url as _impl
import sys
sys.modules[__name__] = _impl

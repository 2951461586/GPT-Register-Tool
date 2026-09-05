"""Compatibility facade for the CFWorker mailbox provider.

Implementation lives in :mod:`sms_tool.providers.mailbox_cfworker`.
"""
from .providers import mailbox_cfworker as _impl
import sys
sys.modules[__name__] = _impl

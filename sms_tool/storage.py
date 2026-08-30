"""Thin backward-compatibility shell for the former monolithic storage module.

All definitions now live in the `store` subpackage; this module re-exports them
verbatim so every `from sms_tool.storage import ...` / `sms_tool.storage.X`
reference keeps working.
"""
from .store import *  # noqa: F401,F403
from .store import __all__ as __all__  # noqa: F401

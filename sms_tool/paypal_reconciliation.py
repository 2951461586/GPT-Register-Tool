"""paypal_reconciliation.py is now a thin backward-compatibility shell; all definitions
live in the `paypal_link` subpackage and are re-exported verbatim so every
`from sms_tool.paypal_reconciliation import ...` / `sms_tool.paypal_reconciliation.X` reference keeps working.
"""

from .paypal_link import *  # noqa: F401,F403
from .paypal_link import __all__ as __all__  # noqa: F401

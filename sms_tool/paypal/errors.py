"""PayPal automation error types.

Extracted from ``sms_tool.paypal_auto`` during the module-split refactor.
This module is dependency-free so every other sub-module can import it.
"""

from __future__ import annotations

class _PayPalStepError(Exception):
    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")

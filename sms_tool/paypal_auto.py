"""PayPal auto-payment compatibility shim.

The implementation now lives in :mod:`sms_tool.paypal`, split by layer:

=========================  ============================================
module                     responsibility
=========================  ============================================
``paypal.orchestrator``    strategy selection + result persistence
``paypal.flow_steps``      step machine, SMS / human-verification gates
``paypal.form_steps``      semantic PayPal form fields
``paypal.session``         browser context helpers
``paypal.dom_fields``      generic locate / fill / read primitives
``paypal.config_picker``   card / address / phone selection
``paypal.errors``          ``_PayPalStepError``
=========================  ============================================

This module only re-exports those symbols so existing call sites and patches
keep working. New code should import from :mod:`sms_tool.paypal` directly.
"""

from __future__ import annotations

from .paypal import *  # noqa: F401,F403  (back-compat re-export surface)
from .paypal import (  # noqa: F401  (explicit primary API)
    _PayPalStepError,
    _pick_phone_and_sms,
    auto_pay,
)

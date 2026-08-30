"""Unified state machine for protocol payment-link extraction.

This module is now a thin backward-compatibility shell; all definitions
live in the `pay_link` subpackage and are re-exported verbatim so every
`from sms_tool.payment_link_manager import ...` / `sms_tool.payment_link_manager.X`
reference keeps working.
"""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from .config import ConfigError, current_config_data, resolve_runtime_config, validate_config
from .paths import project_path, runtime_file
from .payment_contracts import PaymentRequest, PaymentResult, payment_history_metadata
from .payment_catalog import (
    PAYMENT_METHODS as CATALOG_METHODS,
    normalize_payment_method as normalize_catalog_payment_method,
    validate_catalog_consistency,
)
from .payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from .payment_executor import PaymentExecutionRequest, PaymentFlowExecutor
from .payment_operation import (
    PaymentOperationConflict,
    PaymentOperationStore,
    conflict_result as payment_operation_conflict_result,
)
from .payment_routing import (
    PaymentRoutePlan,
    PaymentRoutePlanner,
    coerce_approve_country as canonical_coerce_approve_country,
    parse_proxy_pool,
    payment_proxy_pools as canonical_payment_proxy_pools,
)
from .sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text
from . import payment_egress


from .pay_link import *  # noqa: F401,F403
from .pay_link import __all__ as __all__  # noqa: F401

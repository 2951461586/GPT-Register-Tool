"""Shared timeout constants for protocol-payment extractors (seconds).

These values feed ``requests`` / ``curl_cffi`` calls, whose ``timeout`` argument
is in **seconds**. Do not reuse them for Playwright, which takes **milliseconds**
— that unit mismatch is why this module is scoped to the HTTP extractors only.

History: ``DEFAULT_TIMEOUT = 30`` and ``CHATGPT_TIMEOUT = 45`` were each defined
verbatim in blik / ideal / twint (and ``DEFAULT_TIMEOUT`` again in
``pp_link_helpers`` / ``omakse_client`` / ``smailr_client``). A single authority
means a future tuning change is one edit, not four.
"""

from __future__ import annotations

# Generic HTTP call timeout (seconds).
DEFAULT_TIMEOUT = 30

# ChatGPT-facing calls (session bootstrap, auth) get a longer budget (seconds).
CHATGPT_TIMEOUT = 45

__all__ = ["DEFAULT_TIMEOUT", "CHATGPT_TIMEOUT"]

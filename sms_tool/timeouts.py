"""Shared HTTP timeout constants (seconds) for the ``sms_tool`` package.

These feed ``requests`` / ``curl_cffi`` calls, whose ``timeout`` argument is in
**seconds**. Playwright callers take **milliseconds** and must NOT use these —
that unit split is deliberate, and is why this module only carries the HTTP
values.

History: ``DEFAULT_TIMEOUT = 30`` was defined verbatim in ``pp_link_helpers``,
``omakse_client`` and ``providers/smailr_client``; ``CHATGPT_TIMEOUT = 45`` was
duplicated in ``pp_link_helpers``. A single authority turns a tuning change into
one edit. ``geo/resolver.py`` keeps its own ``DEFAULT_TIMEOUT = 6.0``: that is a
*probe* budget (fail fast, cached), not a general HTTP default, so it stays put.
"""

from __future__ import annotations

# Generic outbound HTTP call timeout (seconds).
DEFAULT_TIMEOUT = 30

# ChatGPT-facing calls (session bootstrap, auth) get a longer budget (seconds).
CHATGPT_TIMEOUT = 45

__all__ = ["DEFAULT_TIMEOUT", "CHATGPT_TIMEOUT"]

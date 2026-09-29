"""Single authority for the OpenAI hosts the ``sms_tool`` client calls.

Why this exists
---------------
The same three hosts were hard-coded across the client: ``auth.openai.com``,
``chatgpt.com`` and ``api.openai.com`` appeared in ~150 string literals over 40
modules, some as request URLs, some as ``Referer``/``Origin`` values, some as
per-flow page URLs. A host move (an OpenAI domain migration, a staging canary)
meant finding every one, and nothing measured the complement.

This module is the ``sms_tool`` side of that authority. It is deliberately a
**parallel** module, not a shared one:
``services/protocol-payment/common/endpoints.py`` is the authority *inside the
extractor process*, and Boundary Rule 10 forbids the two processes importing
each other. The two therefore own the hosts independently and drift is policed
by a ratchet (``scripts/endpoints_literal_ratchet.py``), not by a shared import.

Scope decision, copied from the services authority: only the **hosts** and the
small set of path templates used from more than one place live here. A
one-call-site path keeps its own literal — the win is the single host, not
churning every f-string.

Not here, on purpose: ``https://api.openai.com/auth`` is the namespace claim
**key** inside an access token, not a URL to call. It is a dict key and must
never be replaced by :data:`API_BASE`.

Configuration still wins. ``chatgpt.chat_base_url`` / ``chatgpt.auth_base_url``
override these values at runtime; the constants below are the *defaults* those
reads fall back to, now declared once instead of in every caller.
"""

from __future__ import annotations

AUTH_BASE = "https://auth.openai.com"
CHATGPT_BASE = "https://chatgpt.com"
API_BASE = "https://api.openai.com"

#: ``https://chatgpt.com/`` — the trailing-slash form used for ``Referer`` /
#: ``Origin`` headers and NextAuth ``callbackUrl`` values.
CHATGPT_ORIGIN = CHATGPT_BASE + "/"

# ChatGPT API prefixes (the paths differ from :data:`API_BASE`'s host).
CHATGPT_BACKEND_API = f"{CHATGPT_BASE}/backend-api"
CHATGPT_BACKEND_ANON = f"{CHATGPT_BASE}/backend-anon"

# Auth pages that serve as the flow-binding target for Sentinel issuance and as
# the landing page a step settles on.
AUTH_EMAIL_VERIFICATION = f"{AUTH_BASE}/email-verification"
AUTH_CREATE_ACCOUNT_PASSWORD = f"{AUTH_BASE}/create-account/password"
AUTH_ABOUT_YOU = f"{AUTH_BASE}/about-you"

__all__ = [
    "API_BASE",
    "AUTH_ABOUT_YOU",
    "AUTH_BASE",
    "AUTH_CREATE_ACCOUNT_PASSWORD",
    "AUTH_EMAIL_VERIFICATION",
    "CHATGPT_BACKEND_ANON",
    "CHATGPT_BACKEND_API",
    "CHATGPT_BASE",
    "CHATGPT_ORIGIN",
]

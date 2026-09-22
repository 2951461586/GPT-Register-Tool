"""Single authority for the hosts/URLs the protocol-payment extractors call.

The extractors previously hard-coded ``https://chatgpt.com`` (and friends) in
30+ places across blik / ideal / twint / kakao / momo / pix / direct_card. A
host change (or a canary against a staging host) meant editing every one. This
module is the services-side authority; ``sms_tool`` has its own endpoints and
the two processes do NOT share (Boundary Rule 10).

Scope decision: only the *hosts* and the small set of *shared* path templates
are centralised here. Per-extractor request bodies keep their own literals where
the path is used exactly once — the win is the single host, not churning every
f-string.

All values are plain constants (no env override): these are production upstream
hosts, and making them env-tunable would let a typo silently point a payment at
the wrong origin.
"""

from __future__ import annotations

# Hosts
CHATGPT_BASE = "https://chatgpt.com"
CHATGPT_HOST = "chatgpt.com"
STRIPE_API_BASE = "https://api.stripe.com"
STRIPE_CHECKOUT_BASE = "https://checkout.stripe.com"
OPENAI_PAY_BASE = "https://pay.openai.com"

# Shared ChatGPT paths (used by ≥2 extractors)
CHATGPT_CHECKOUT_API = f"{CHATGPT_BASE}/backend-api/payments/checkout"
CHATGPT_CHECKOUT_SNAPSHOT = f"{CHATGPT_CHECKOUT_API}/snapshot"
CHATGPT_CHECKOUT_UPDATE = f"{CHATGPT_CHECKOUT_API}/update"
CHATGPT_CHECKOUT_APPROVE = f"{CHATGPT_CHECKOUT_API}/approve"
CHATGPT_CHECKOUT_TAXES = f"{CHATGPT_CHECKOUT_API}/taxes"
CHATGPT_SENTINEL_PING = f"{CHATGPT_BASE}/backend-api/sentinel/ping"

# Stripe API paths
STRIPE_PAYMENT_METHODS = f"{STRIPE_API_BASE}/v1/payment_methods"


def chatgpt_checkout_page(processor_entity: str, checkout_id: str) -> str:
    """The hosted checkout page for a given processor + session."""
    return f"{CHATGPT_BASE}/checkout/{processor_entity}/{checkout_id}"


def chatgpt_checkout_verify(cs_id: str, processor_entity: str) -> str:
    """The success/verify URL Stripe returns to after a completed payment."""
    return (
        f"{CHATGPT_BASE}/checkout/verify?stripe_session_id={cs_id}"
        f"&processor_entity={processor_entity}&plan_type=plus"
    )


def stripe_payment_page(cs_id: str, suffix: str) -> str:
    """A ``/v1/payment_pages/<cs_id>/<suffix>`` endpoint (init / confirm / ...)."""
    return f"{STRIPE_API_BASE}/v1/payment_pages/{cs_id}/{suffix}"


__all__ = [
    "CHATGPT_BASE",
    "CHATGPT_HOST",
    "STRIPE_API_BASE",
    "STRIPE_CHECKOUT_BASE",
    "OPENAI_PAY_BASE",
    "CHATGPT_CHECKOUT_API",
    "CHATGPT_CHECKOUT_SNAPSHOT",
    "CHATGPT_CHECKOUT_UPDATE",
    "CHATGPT_CHECKOUT_APPROVE",
    "CHATGPT_CHECKOUT_TAXES",
    "CHATGPT_SENTINEL_PING",
    "STRIPE_PAYMENT_METHODS",
    "chatgpt_checkout_page",
    "chatgpt_checkout_verify",
    "stripe_payment_page",
]

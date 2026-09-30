from __future__ import annotations

import os
import re


UPI_DUMP_DEFAULT_DIR = "runtime/upi_dumps"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
UPI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
UPI_CHECKOUT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
UPI_CHECKOUT_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
UPI_CHECKOUT_SNAPSHOT_URL = "https://chatgpt.com/backend-api/payments/checkout/snapshot"
UPI_SENTINEL_PING_URL = "https://chatgpt.com/backend-api/sentinel/ping"
STRIPE_PAYMENT_PAGE_INIT_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/init"
STRIPE_PAYMENT_PAGE_CONFIRM_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
STRIPE_PAYMENT_PAGE_GET_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}"
STRIPE_PAYMENT_METHODS_URL = "https://api.stripe.com/v1/payment_methods"
STRIPE_INTENT_URL_T = "https://api.stripe.com/v1/{intent_path}/{intent_id}"
#: Stripe.js 在发出任何业务请求之前先向这里注册设备指纹（HAR 实测 4 次 POST，
#: ``tag`` 依次为 stripejs-init-started / stripejs-init-complete /
#: adyen-component-loaded / payment-element-mounted）。不发也能支付成功，但「长期
#: 使用同一个 guid/muid 而从未注册过指纹」是 Stripe Radar 能直接看出来的异常。
#: 来源：blog.caowo.de《Stripe protocol payment automation deep dive 2026》§7.1。
UPI_STRIPE_FINGERPRINT_URL = "https://m.stripe.com/6"
#: 只登记我们**确实在做**的两个生命周期事件，不伪造 adyen / payment-element 挂载。
UPI_STRIPE_FINGERPRINT_TAGS: tuple[str, ...] = ("stripejs-init-started", "stripejs-init-complete")
# Stripe API version string the reference upi-zero-link rail sends (captured
# from the browser's confirm body). The ``custom_checkout_beta`` variant is the
# one the manual-approval custom-checkout flow accepts.
UPI_REFERENCE_STRIPE_VERSION = (
    "2020-08-27;custom_checkout_beta=v1; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
# Stripe.js runtime version the reference confirm body carries.
UPI_REFERENCE_STRIPE_RUNTIME_VERSION = "a34694b057"
UPI_CPMT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
UPI_CPMT_START_URL = "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start"
UPI_CONFIRMATION_TOKENS_URL = "https://api.stripe.com/v1/confirmation_tokens"
UPI_PAYMENT_INTENT_CONFIRM_URL_T = "https://api.stripe.com/v1/payment_intents/{pi_id}/confirm"
UPI_PAYMENT_INTENT_GET_URL_T = "https://api.stripe.com/v1/payment_intents/{pi_id}"
UPI_SENTINEL_APPROVAL_FLOW = "checkout_session_approval"
UPI_SENTINEL_CHECKOUT_FLOW = "chatgpt_checkout"
UPI_WARMUP_TIMEOUT = 25
UPI_ATTESTATION_ENV_KEYS = (
    "PAY153_UPI_ATTESTATION",
    "MIN_UPI_ATTESTATION",
    "MIN_OAICS_ATTESTATION",
)
_UPI_PAGE_BUILD_RE = re.compile(r'<html[^>]*\bdata-build="([^"]+)"', re.I)
_UPI_PAGE_BUILD_ANY_RE = re.compile(r'\bdata-build="([^"]+)"', re.I)
_UPI_PAGE_SEQ_RE = re.compile(r'<html[^>]*\bdata-seq="([^"]+)"', re.I)
_UPI_PAGE_SEQ_ANY_RE = re.compile(r'\bdata-seq="(\d+)"', re.I)
_UPI_PAGE_SEQ_JSON_RE = re.compile(r'"buildNumber"\s*:\s*"?(\d{5,})')
_UPI_ATTESTATION_RE = re.compile(r'"webDeploymentAttestation"\s*:\s*"([^"]+)"')
_UPI_ATTESTATION_ANY_RE = re.compile(r'[^A-Za-z0-9]webDeploymentAttestation[=:]\s*["\']?([A-Za-z0-9._-]+)')
#: Approve-endpoint retry budget. The reference (``upi-zero-link``) uses
#: ``APPROVE_ATTEMPTS = 5``; 60 was an order of magnitude past it, and because a
#: risk-control block answers ``blocked`` on *every* attempt the extra 55 were
#: measured as ~80 s of pure waste per account (2026-09-30: 3/3 accounts ran all
#: 60 attempts and still delivered only a hosted fallback). Overridable per run
#: with ``UPI_APPROVAL_MAX_ATTEMPTS``.
UPI_APPROVAL_MAX_ATTEMPTS = 5
UPI_QR_POLL_MAX_ATTEMPTS = 30
#: Direct SetupIntent mandate confirm (UPI AutoPay). Payment Page confirm drops
#: ``payment_method_options`` as an unknown parameter on this Checkout revision,
#: so an approved submission can still leave its SetupIntent at
#: ``requires_payment_method``. UPI AutoPay only signs the mandate when it is
#: submitted straight to ``/v1/setup_intents/{id}/confirm``, and *that* response
#: is what carries the ``upi://`` deep link -- without this stage every run
#: degrades to the hosted instructions page (measured 3/3 accounts, 2026-09-30).
#: Reference: ``tilian/provider_checkout.retry_approved_local_mandate``.
UPI_LOCAL_MANDATE_ENABLED = True
#: Widest first: the reference retries the ladder before dropping any field,
#: because a rejected variant answers 4xx instead of degrading by itself.
UPI_LOCAL_MANDATE_STRIPE_VERSION = "2026-06-24.dahlia"
UPI_LOCAL_MANDATE_DEFAULT_AMOUNT = 199900
UPI_LOCAL_MANDATE_END_DAYS = 365
UPI_LOCAL_MANDATE_DESCRIPTION = "Subscription payment"
#: Stripe refuses to let anyone but Checkout confirm a SetupIntent that Checkout
#: created. Measured 2026-09-30 on the ``cs_live_`` rail: all 4 mandate variants
#: x 3 ``Stripe-Version`` candidates answered the byte-identical
#: ``You cannot confirm SetupIntents created by Checkout.`` (plus
#: ``The latest attempt to set up the payment method has failed.``). Retrying a
#: different variant or API version therefore cannot help, and the ladder must
#: stop instead of spending 11 more round trips. A marker list, not an equality
#: test, so adjacent Stripe wording still trips it.
UPI_LOCAL_MANDATE_FATAL_MARKERS: tuple[str, ...] = ("created by checkout",)
#: ``generic_decline`` 重试预算（换一个**全新**支付方式，同一个 cs_id）。
#:
#: Stripe 把 ``generic_decline`` 归为可重试：它拒的是这一次支付方式，不是这个
#: Checkout Session —— "通常是最常见的风控拒绝，换一张卡几乎总能过"，而且
#: "拒卡不需要重建整个 Checkout Session，只需要换卡重建 token"。
#: 来源：blog.caowo.de《Stripe protocol payment automation deep dive 2026》
#: §9.1 Decline 错误分类 / §3.5 PI 拒卡重试。
#:
#: 旧口径把它当终态直接放弃（``upi_provider_declined``），等于丢掉 Stripe 本来
#: 允许重试的 session。可用 ``UPI_DECLINE_PM_RETRIES`` 按次覆盖。
UPI_DECLINE_PM_RETRIES = 1
UPI_CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
UPI_CHATGPT_CLIENT_BUILD_NUMBER = "7904904"
UPI_QR_POLL_INTERVAL = 1.0
UPI_FINGERPRINT_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "name": "chrome-win",
        "impersonate": "chrome136",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.114 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "locale": "en-IN",
        "elements_locale": "en",
        "timezone": "Asia/Kolkata",
        "accept_language": "en-IN,en;q=0.9",
    },
    {
        "name": "chrome-mac",
        "impersonate": "chrome124",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.6367.119 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="124", "Chromium";v="124", "Not.A/Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "locale": "en-IN",
        "elements_locale": "en",
        "timezone": "Asia/Kolkata",
        "accept_language": "en-IN,en;q=0.9",
    },
    {
        "name": "chrome-linux",
        "impersonate": "chrome136",
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.114 Safari/537.36"
        ),
        "sec_ch_ua": '"Not.A/Brand";v="99", "Chromium";v="136", "Google Chrome";v="136"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Linux"',
        "locale": "en-IN",
        "elements_locale": "en",
        "timezone": "Asia/Kolkata",
        "accept_language": "en-IN,en;q=0.9",
    },
)
UPI_DEFAULT_FINGERPRINT = UPI_FINGERPRINT_TEMPLATES[0]
UPI_BILLING_NAMES: tuple[tuple[str, str], ...] = (
    ("Aisha", "Sharma"),
    ("Arjun", "Mehta"),
    ("Kavya", "Gupta"),
    ("Rohan", "Kapoor"),
    ("Priya", "Nair"),
)
UPI_BILLING_ADDRESSES: tuple[tuple[str, str, str, str], ...] = (
    ("24 Park Street", "Kolkata", "700016", "WB"),
    ("14 MG Road", "Bengaluru", "560001", "KA"),
    ("18 Marine Drive", "Mumbai", "400020", "MH"),
    ("32 Connaught Place", "New Delhi", "110001", "DL"),
)
UPI_EMAIL_DOMAINS: tuple[str, ...] = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")
UPI_BILLING_IN = {
    "name": "Rahul Sharma",
    "email": "upi-scanner@example.com",
    "line1": "Flat 302, Sai Residency",
    "line2": "MG Road, Andheri East",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal": "400069",
    "country": "IN",
}
UPI_SECOND_CONFIRM_MARKERS = (
    "checkout_upcoming_invoice_mismatch",
    "redirect url resolution timeout",
    "missing_redirect",
)
UPI_CALL_OPTIONS: tuple[str, ...] = (
    "proxy",
    "auth_context",
    "checkout_proxy",
    "provider_proxy",
    "approve_proxy",
    "target_country",
    "checkout_country",
    "payment_country",
    "require_zero",
    "qr_path",
    "proxy_state",
    "device_id",
    "session_token",
    "wait_paid",
    "paid_timeout",
    "require_server_upi_mandate",
)

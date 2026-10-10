#!/usr/bin/env python3
r"""PayPal 协议提取核心 -- 三段式代理提链器。

从 ``gen_pp_link.py`` 纯搬迁拆分（零行为变化）。本模块拥有:

* ``PPLinkExtractor`` -- 三段式代理提链核心
  (checkout → stripe init/PM/confirm → approve/poll/redirect)

**跨泳道共享**的那部分支付能力（会话工厂、Checkout 原语、币种表、供应商阶段
顺序、五个失败词汇类、诊断助手）已于 2026-10-08 搬到中性的
``sms_tool/payment_wire.py``（逐字搬迁、逐符号 AST 等价）。本模块**再导出**
它们，所以历史导入与 ``patch(...)`` 目标不变；定义只归 ``payment_wire`` 一处。
见 ``docs/architecture.md`` 的 Boundary Rule 20。

依赖方向: ``gen_pp_link`` → 本模块; 本模块不得 import ``gen_pp_link``。
本模块 → ``payment_wire``（单向）；中性内核不得反向依赖本模块。
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    from .phone_proxy import normalize_proxy_url
    from .pp_link_helpers import (
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
        RETRY_ATTEMPTS,
        _SIDE_EFFECT_STAGES,
        stripe_confirm_error_diagnostics,
        is_paypal_ba_approve_url,
        extract_ba_token,
        extract_redirect_url,
        resolve_external_redirect,
        billing_for_country,
        reserve_billing_variant,
        stripe_amount_details,
    )
except ImportError:  # pragma: no cover - direct script execution
    from phone_proxy import normalize_proxy_url  # type: ignore
    from pp_link_helpers import (  # type: ignore
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
        RETRY_ATTEMPTS,
        _SIDE_EFFECT_STAGES,
        stripe_confirm_error_diagnostics,
        is_paypal_ba_approve_url,
        extract_ba_token,
        extract_redirect_url,
        resolve_external_redirect,
        billing_for_country,
        reserve_billing_variant,
        stripe_amount_details,
    )

try:
    from .checkout_contract import (
        PLUS_TRIAL_CAMPAIGN_ID,
        CheckoutRequestContract,
        CheckoutSessionContract,
        browser_profile_for_country,
    )
except ImportError:  # pragma: no cover - direct script execution
    from checkout_contract import (  # type: ignore
        PLUS_TRIAL_CAMPAIGN_ID,
        CheckoutRequestContract,
        CheckoutSessionContract,
        browser_profile_for_country,
    )

try:
    from .paypal_proxy import (
        PayPalProxyState,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )
except ImportError:  # pragma: no cover - direct script execution
    from paypal_proxy import (  # type: ignore
        PayPalProxyState,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )

# ─── 常量 ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─── 共享支付内核（定义在 payment_wire；此处再导出）──────────────────────────
#
# 这些符号的**定义**已搬到中性的 ``payment_wire``：UPI 泳道、账号支付资格、
# capability probe 与 PayPal 链接共用同一份会话工厂、Checkout 原语、币种表与
# 失败词汇，所以不再挂在以 PayPal 命名的模块下。本模块仍是 PayPal 提链器
# ``PPLinkExtractor`` 的归属模块，并且是这些符号的兼容面。
#
# ``PPLinkExtractor`` 在本模块内以模块全局名调用下列名字，所以
# ``patch.object(paypal_extract, "_new_session" | "_checkout_post", ...)``
# 仍然生效 —— 补丁打在本模块的命名空间上就能被看见。
try:
    from .payment_wire import (
        CURRENCY_MAP,
        CheckoutApprovalBlockedError,
        CheckoutNotZeroDueError,
        PaymentOutcomeUnknownError,
        PayPalCapabilityError,
        PayPalHttpError,
        _PROVIDER_STAGES,
        _checkout_post,
        _compact_diagnostic,
        _new_session,
    )
except ImportError:  # pragma: no cover - direct script execution
    from payment_wire import (  # type: ignore
        CURRENCY_MAP,
        CheckoutApprovalBlockedError,
        CheckoutNotZeroDueError,
        PaymentOutcomeUnknownError,
        PayPalCapabilityError,
        PayPalHttpError,
        _PROVIDER_STAGES,
        _checkout_post,
        _compact_diagnostic,
        _new_session,
    )

# 只为**外部**消费者保留的再导出（本模块自身不再引用它们）：
# ``patch.object(paypal_extract.curl_requests, "post", ...)`` 打的是 curl_cffi
# **模块对象**本身，与 ``payment_wire`` 看到的是同一个对象；
# ``_checkout_device_id`` / ``_checkout_get`` 由测试直接调用。
try:
    from .payment_wire import (  # noqa: F401
        _checkout_device_id,
        _checkout_get,
        curl_requests,
        requests,
    )
except ImportError:  # pragma: no cover - direct script execution
    from payment_wire import (  # type: ignore # noqa: F401
        _checkout_device_id,
        _checkout_get,
        curl_requests,
        requests,
    )


# ─── 核心流程 ──────────────────────────────────────────────────────────────────


class PPLinkExtractor:
    """三段式代理提链器。"""

    def __init__(
        self,
        access_token: str,
        checkout_proxy: str = "",
        provider_proxy: str = "",
        stripe_init_proxy: str = "",
        payment_method_proxy: str = "",
        confirm_proxy: str = "",
        approve_proxy: str = "",
        promotion_proxy: str = "",
        target_country: str = "DE",
        checkout_country: str = "",
        stripe_pk: str = "",
        require_zero: bool = True,
        emit: Any = None,
        cookie_header: str = "",
        promotion_taxes: bool = False,
        promo_campaign_id: str = PLUS_TRIAL_CAMPAIGN_ID,
        preflight_proxy_check: bool = False,
        rotate_proxy_sessions: bool = False,
        proxy_probe_timeout: float = 12,
        max_stage_retries: int = RETRY_ATTEMPTS,
        max_checkout_retries: int = RETRY_ATTEMPTS,
        proxy_state: PayPalProxyState | None = None,
        stage_proxy_countries: dict[str, str] | None = None,
        device_id: str = "",
        billing_variant: int | None = None,
    ):
        self.access_token = access_token
        self.checkout_proxy = normalize_proxy_url(checkout_proxy)
        self.provider_proxy = normalize_proxy_url(provider_proxy)
        self.stripe_init_proxy = normalize_proxy_url(stripe_init_proxy or provider_proxy)
        self.payment_method_proxy = normalize_proxy_url(payment_method_proxy or provider_proxy)
        self.confirm_proxy = normalize_proxy_url(confirm_proxy or provider_proxy)
        self.approve_proxy = normalize_proxy_url(approve_proxy or provider_proxy)
        # Promotion stage: apply the 0-due promo to the *existing* checkout via
        # POST /backend-api/payments/checkout/update, routed through a
        # promo-eligible region egress. Empty => stage disabled (behaviour
        # unchanged). This is what makes "0元 + PayPal" possible on one session:
        # the checkout is created in a PayPal region, then the promo is attached
        # from a promo-eligible region.
        self.promotion_proxy = normalize_proxy_url(promotion_proxy)
        self.enable_promotion = bool(self.promotion_proxy)
        self.promotion_taxes = bool(promotion_taxes)
        self.promo_campaign_id = str(promo_campaign_id or PLUS_TRIAL_CAMPAIGN_ID)
        self.target_country = target_country.upper()
        # Billing identity variant. ``None`` means "reserve one on first use":
        # ``_sync_tax_region`` and ``_create_payment_method`` are two stages of
        # the *same* checkout and must agree, so the reservation is cached on
        # this instance rather than made per call. Resolved lazily so an
        # extractor that never reaches Stripe never touches the cursor file.
        self._billing_variant = billing_variant
        self.checkout_country = (checkout_country or target_country).upper()
        self.currency = CURRENCY_MAP.get(self.target_country, "EUR")
        self.checkout_currency = CURRENCY_MAP.get(self.checkout_country, "USD")
        self.stripe_pk = stripe_pk or DEFAULT_STRIPE_PK
        self.require_zero = require_zero
        self.emit = emit or (lambda step, msg, **kw: None)
        self.cookie_header = cookie_header or ""
        self.runtime_version = "6f8494a281"
        self.stripe_js_id = str(uuid.uuid4())
        self.elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        self.elements_session_config_id = str(uuid.uuid4())
        self.preflight_proxy_check = bool(preflight_proxy_check)
        self.rotate_proxy_sessions = bool(rotate_proxy_sessions)
        self.proxy_probe_timeout = max(1.0, float(proxy_probe_timeout or 12))
        self.max_stage_retries = max(1, int(max_stage_retries or RETRY_ATTEMPTS))
        self.max_checkout_retries = max(1, int(max_checkout_retries or RETRY_ATTEMPTS))
        self.device_id = str(device_id or uuid.uuid4())
        self.last_retry_error: dict[str, Any] = {}
        self.workflow_attempt = 0
        self.promotion_applied = False
        self._chatgpt_session = None
        self.proxy_state = proxy_state or PayPalProxyState(
            Path(PROJECT_ROOT) / "runtime" / "paypal_proxy_state.json",
            enabled=False,
        )
        countries = stage_proxy_countries if isinstance(stage_proxy_countries, dict) else {}
        self.stage_proxy_countries = {
            "checkout": str(countries.get("checkout") or self.checkout_country).upper(),
            "promotion": str(countries.get("promotion") or "").upper(),
            "provider": str(countries.get("provider") or self.target_country).upper(),
            "stripe_init": str(
                countries.get("stripe_init") or countries.get("provider") or self.target_country
            ).upper(),
            "payment_method": str(
                countries.get("payment_method") or countries.get("provider") or self.target_country
            ).upper(),
            "confirm": str(countries.get("confirm") or countries.get("provider") or self.target_country).upper(),
            "approve": str(countries.get("approve") or self.target_country).upper(),
        }
        self.proxy_exits: dict[str, dict[str, Any]] = {}
        self._active_stage = ""

    def _log(self, step: str, msg: str, **kw):
        self.emit(step, msg, **kw)

    def _reset_stripe_context(self) -> None:
        self.stripe_js_id = str(uuid.uuid4())
        self.elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        self.elements_session_config_id = str(uuid.uuid4())

    def _reset_workflow_context(self) -> None:
        self._reset_stripe_context()
        self._stripe_session = None
        self._chatgpt_session = None
        self.proxy_exits = {}
        self.promotion_applied = False
        self._active_stage = ""

    def _approve_session(self) -> Any:
        session = self._chatgpt_session
        if session is None:
            session = _new_session(self.approve_proxy)
            self._chatgpt_session = session
        elif hasattr(session, "proxies"):
            session.proxies = {"http": self.approve_proxy, "https": self.approve_proxy} if self.approve_proxy else {}
        session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://chatgpt.com",
                "oai-device-id": self.device_id,
            }
        )
        if self.cookie_header:
            session.headers["Cookie"] = self.cookie_header
        return session

    def _fresh_approval_sentinel(self, session: Any) -> dict[str, str]:
        """Mint one checkout-approval Sentinel bound to this flow/session."""
        try:
            from .sentinel import issue_sentinel_flow

            issued = issue_sentinel_flow(
                flow="checkout_session_approval",
                device_id=self.device_id,
                session=session,
                proxy=self.approve_proxy,
            )
            headers = {"OpenAI-Sentinel-Token": issued.token}
            if issued.so_token:
                headers["OpenAI-Sentinel-SO-Token"] = issued.so_token
            self._log("sentinel", "checkout_session_approval Sentinel ready")
            return headers
        except Exception as exc:
            self._log("sentinel", f"Sentinel unavailable: {_compact_diagnostic(exc)}")
            return {}

    def _set_stripe_proxy(self, proxy: str) -> Any:
        stripe = getattr(self, "_stripe_session", None)
        if stripe is None:
            stripe = _new_session(proxy)
            self._stripe_session = stripe
        elif hasattr(stripe, "proxies"):
            stripe.proxies = {"http": proxy, "https": proxy} if proxy else {}
        return stripe

    def _prepare_stage_proxy(self, stage: str, proxy: str, attempt: int = 1) -> str:
        expected_country = self.stage_proxy_countries.get(stage, "")
        prepared = normalize_proxy_url(proxy)
        if prepared and self.rotate_proxy_sessions:
            prepared = rotate_stage_proxy_session(prepared, expected_country)
        label = redact_proxy_url(prepared)
        if not prepared:
            self.proxy_exits[stage] = {"ok": True, "country_code": "", "ip": "", "proxy": "DIRECT"}
            return prepared
        if not self.preflight_proxy_check:
            self._log("proxy", f"{stage} attempt={attempt} proxy={label} (preflight disabled)")
            return prepared
        self._log("proxy", f"{stage} attempt={attempt} checking expected={expected_country or 'ANY'} proxy={label}")
        result = probe_proxy(
            prepared,
            expected_country=expected_country,
            stage=stage,
            timeout=self.proxy_probe_timeout,
        )
        self.proxy_exits[stage] = {**result.to_dict(), "proxy": label}
        self.proxy_state.record_result(stage, prepared, result.ok, result.error, result.country_code)
        if not result.ok:
            raise RuntimeError(
                f"proxy_preflight_failed:{stage}:expected={expected_country or 'ANY'}:"
                f"actual={result.country_code or 'unknown'}:{result.error}"
            )
        self._log("proxy", f"{stage} exit={result.ip}/{result.country_code} {result.country}")
        return prepared

    def _record_stage_result(self, stage: str, proxy: str, success: bool, reason: str = "") -> None:
        country = str(
            (self.proxy_exits.get(stage) or {}).get("country_code") or self.stage_proxy_countries.get(stage) or ""
        )
        self.proxy_state.record_result(stage, proxy, success, reason, country)

    # ─── Stage 1: Checkout (JP/TH 代理) ───────────────────────────────────

    def _checkout_contract(self) -> CheckoutRequestContract:
        """Build the canonical checkout contract for this run.

        Locale and timezone follow the checkout egress country so the Stripe
        fingerprint matches where the session is actually created.
        """
        browser = browser_profile_for_country(self.checkout_country)
        return CheckoutRequestContract.for_payment_method(
            "paypal",
            billing_country=self.checkout_country,
            currency=self.checkout_currency,
            browser_locale=browser.browser_locale,
            browser_timezone=browser.browser_timezone,
            promo_campaign_id=self.promo_campaign_id,
        )

    def _create_checkout(self) -> dict:
        base_proxy = self.checkout_proxy
        self._log(
            "checkout",
            f"Stage 1: proxy={redact_proxy_url(base_proxy)} billing={self.checkout_country}/{self.checkout_currency} target={self.target_country}",
        )
        body = self._checkout_contract().checkout_payload()
        for attempt in range(1, self.max_stage_retries + 1):
            attempt_proxy = base_proxy
            try:
                attempt_proxy = self._prepare_stage_proxy("checkout", base_proxy, attempt)
                # The device id must match the ``chatgpt_checkout`` Sentinel pair
                # minted for this create request, so present it explicitly rather
                # than relying on the account cookie carrying ``oai-did``.
                r = _checkout_post(
                    "https://chatgpt.com/backend-api/payments/checkout",
                    body,
                    self.access_token,
                    self.cookie_header,
                    attempt_proxy,
                    CHATGPT_TIMEOUT,
                    extra_headers={"OAI-Device-Id": self.device_id} if self.device_id else None,
                )
                if r.status_code >= 400:
                    raise PayPalHttpError(
                        "checkout",
                        "https://chatgpt.com/backend-api/payments/checkout",
                        r,
                        retryable=r.status_code in {408, 425, 429} or r.status_code >= 500,
                    )
                data = r.json()
                session = CheckoutSessionContract.from_payload(
                    data,
                    billing_country=self.checkout_country,
                    fallback_publishable_key=self.stripe_pk,
                )
                cs_id = session.checkout_session_id
                processor_entity = session.processor_entity
                pk = session.publishable_key
                if pk.startswith("pk_"):
                    self.stripe_pk = pk
                else:
                    self._log(
                        "checkout",
                        "checkout 未返回 publishable_key，回退到默认 PK（若失效请设置 PP_STRIPE_PUBLISHABLE_KEY）",
                    )
                self.checkout_proxy = attempt_proxy
                self._record_stage_result("checkout", attempt_proxy, True)
                self._log("checkout", f"checkout 成功: cs_id={cs_id}")
                return {
                    "cs_id": cs_id,
                    "processor_entity": processor_entity,
                    "stripe_publishable_key": self.stripe_pk,
                    "billing_country": self.checkout_country,
                    "currency": self.checkout_currency,
                }
            except Exception as e:
                self._record_stage_result("checkout", attempt_proxy, False, str(e))
                self._log("checkout", f"checkout 第 {attempt} 次失败: {e}")
                retryable = is_retryable_network_error(e) or "proxy_preflight_failed" in str(e)
                if attempt < self.max_stage_retries and retryable:
                    self._log("checkout", f"retry {attempt + 1}/{self.max_stage_retries} with a new proxy session")
                else:
                    raise

    # ─── Stage 1.5: Promotion update (促销可用区代理) ──────────────────────

    def _checkout_page_url(self, cs_id: str, processor_entity: str) -> str:
        entity = processor_entity or ("openai_llc" if self.checkout_country == "US" else "openai_ie")
        return f"https://chatgpt.com/checkout/{entity}/{cs_id}"

    def checkout_update_promotion(self, cs_id: str, processor_entity: str) -> bool:
        """Apply the 0-due promo to an existing checkout via /checkout/update.

        Routed through ``promotion_proxy`` (a promo-eligible region egress).
        Returns True on success. Non-fatal on failure: logs and returns False so
        the downstream ``require_zero`` gate in _stripe_init decides the outcome.
        """
        self._log(
            "promotion", f"Stage 1.5: proxy={redact_proxy_url(self.promotion_proxy)} promo={self.promo_campaign_id}"
        )
        body = {
            "checkout_session_id": cs_id,
            "processor_entity": processor_entity,
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": self.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        extra_headers = {
            "Referer": self._checkout_page_url(cs_id, processor_entity),
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        }
        try:
            self.promotion_proxy = self._prepare_stage_proxy("promotion", self.promotion_proxy)
            r = _checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout/update",
                body,
                self.access_token,
                self.cookie_header,
                self.promotion_proxy,
                CHATGPT_TIMEOUT,
                extra_headers=extra_headers,
            )
        except Exception as e:
            self._record_stage_result("promotion", self.promotion_proxy, False, str(e))
            self._log("promotion", f"checkout/update 请求异常 (忽略, 由 require_zero 兜底): {e}")
            return False
        if r.status_code >= 400:
            self._record_stage_result("promotion", self.promotion_proxy, False, f"HTTP {r.status_code}")
            self._log("promotion", f"checkout/update 失败 {r.status_code}: {r.text[:200]} (忽略, 由 require_zero 兜底)")
            return False
        try:
            payload = r.json() or {}
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("success") is False:
            self._record_stage_result("promotion", self.promotion_proxy, False, "promotion_rejected")
            self._log("promotion", f"checkout/update 被拒: {json.dumps(payload, ensure_ascii=False)[:200]}")
            return False
        self._record_stage_result("promotion", self.promotion_proxy, True)
        self._log("promotion", "checkout/update 成功: 促销已应用到当前 checkout")
        return True

    def _billing_identity(self) -> dict:
        """Billing identity for this run, with the variant resolved once.

        ``_checkout_update_taxes`` and ``_create_payment_method`` are two stages
        of the *same* Checkout, so they must bill from the same address: a
        rotating default in ``billing_for_country`` would let the tax sync and
        the payment method disagree. The variant is therefore reserved on first
        use and cached on the instance. The email stays per-call, as before.
        """
        if self._billing_variant is None:
            self._billing_variant = reserve_billing_variant(self.target_country)
        return billing_for_country(self.target_country, variant=self._billing_variant)

    def _checkout_update_taxes(self, cs_id: str, processor_entity: str) -> bool:
        """Optionally sync billing/tax region via /checkout/taxes (provider 代理)."""
        try:
            taxes_proxy = self._prepare_stage_proxy("provider", self.provider_proxy)
        except Exception as e:
            self._record_stage_result("provider", self.provider_proxy, False, str(e))
            self._log("promotion", f"checkout/taxes provider proxy failed (ignored): {e}")
            return False
        billing = self._billing_identity()
        body = {
            "checkout_session_id": cs_id,
            "checkout_email": billing["email"],
            "billing_country": self.target_country,
            "billing_name": f"{billing['name'][0]} {billing['name'][1]}",
            "currency": self.currency,
            "tax_id": None,
            "processor_entity": processor_entity,
            "billing_address": {
                "line1": billing["street"],
                "city": billing["city"],
                "country": self.target_country,
                "postal_code": billing["postal"],
            },
        }
        extra_headers = {
            "Referer": self._checkout_page_url(cs_id, processor_entity),
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        }
        try:
            r = _checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout/taxes",
                body,
                self.access_token,
                self.cookie_header,
                taxes_proxy,
                CHATGPT_TIMEOUT,
                extra_headers=extra_headers,
            )
        except Exception as e:
            self._log("promotion", f"checkout/taxes 请求异常 (忽略): {e}")
            return False
        if r.status_code >= 400:
            self._log("promotion", f"checkout/taxes 失败 {r.status_code}: {r.text[:200]} (忽略)")
            return False
        self._log("promotion", "checkout/taxes 同步成功")
        return True

    # ─── Stage 2: Stripe init + create PM + confirm (目标国代理) ───────────

    def _stripe_init(self, cs_id: str, *, enforce_zero: bool | None = None) -> dict:
        self._active_stage = "stripe_init"
        self._log("stripe_init", f"Stage 2: proxy={redact_proxy_url(self.stripe_init_proxy)} Stripe init")
        stripe = self._set_stripe_proxy(self.stripe_init_proxy)
        body = self._checkout_contract().stripe_init_payload(
            self.stripe_pk,
            stripe_version=STRIPE_VERSION,
            stripe_js_id=self.stripe_js_id,
        )
        r = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code >= 400:
            raise PayPalHttpError(
                "stripe_init",
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
                r,
                retryable=r.status_code in {408, 425, 429} or r.status_code >= 500,
            )
        init = r.json()
        amount_info = stripe_amount_details(init)
        amount = amount_info.get("amount")
        self._log(
            "stripe_init", f"amount={amount} currency={amount_info.get('currency')} source={amount_info.get('source')}"
        )
        self.proxy_state.record_zero_result(self.checkout_proxy, self.checkout_country, amount)
        # amount is None = Stripe 响应里没取到金额证据，属于协议模糊。
        # 不能用 `None != 0 == True` 误判成非零、误杀可能可用的 0 元 checkout；
        # 也不能当成 0 元放行（不知道真实金额）。归一为 not_zero_due 但带 unknown 标记，
        # 由上层交账/对账决定。
        if enforce_zero is None:
            enforce_zero = self.require_zero
        if enforce_zero and self.require_zero and amount is not None and amount != 0:
            raise CheckoutNotZeroDueError(amount, amount_info.get("currency", ""))
        if enforce_zero and self.require_zero and amount is None:
            self._log(
                "stripe_init", "amount not present in stripe init response; treating as inconclusive zero-due check"
            )
            raise CheckoutNotZeroDueError(None, amount_info.get("currency", ""))
        # 检查 PayPal 是否可用
        pm_types = init.get("payment_method_types") or []
        if pm_types and "paypal" not in [str(t).lower() for t in pm_types]:
            raise PayPalCapabilityError(f"当前 checkout 不支持 PayPal, 可用: {pm_types}")
        return init

    def _create_payment_method(self, cs_id: str) -> str:
        self._active_stage = "payment_method"
        self._log("payment_method", f"创建 PayPal payment_method")
        stripe = self._set_stripe_proxy(self.payment_method_proxy)
        billing = self._billing_identity()
        body = {
            "type": "paypal",
            "billing_details[name]": f"{billing['name'][0]} {billing['name'][1]}",
            "billing_details[email]": billing["email"],
            "billing_details[address][country]": billing["country"],
            "billing_details[address][line1]": billing["street"],
            "billing_details[address][city]": billing["city"],
            "billing_details[address][state]": billing["state"],
            "billing_details[address][postal_code]": billing["postal"],
            "payment_user_agent": f"stripe.js/{self.runtime_version}; stripe-js-v3/{self.runtime_version}; payment-element; deferred-intent",
            "referrer": "https://chatgpt.com",
            "time_on_page": str(random.randint(25000, 55000)),
            "client_attribution_metadata[client_session_id]": self.stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        r = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            raise PayPalHttpError(
                "payment_method",
                "https://api.stripe.com/v1/payment_methods",
                r,
                retryable=r.status_code in {408, 425, 429} or r.status_code >= 500,
            )
        pm_id = r.json().get("id", "")
        if not pm_id.startswith("pm_"):
            raise Exception(f"payment_method 响应异常: {r.text[:200]}")
        self._log("payment_method", f"pm_id={pm_id}")
        return pm_id

    def _stripe_confirm(self, cs_id: str, pm_id: str, init: dict) -> dict:
        self._active_stage = "confirm"
        self._log("confirm", "Stripe confirm")
        stripe = self._set_stripe_proxy(self.confirm_proxy)
        processor_entity = "openai_llc" if self.checkout_country == "US" else "openai_ie"
        chatgpt_return = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor_entity}&plan_type=plus"
        hosted_url = str(init.get("stripe_hosted_url") or "")
        if hosted_url:
            hosted_url = hosted_url.replace("checkout.stripe.com", "pay.openai.com")
        else:
            hosted_url = f"https://pay.openai.com/c/pay/{cs_id}?returned_from_redirect=true&ui_mode=custom&return_url={quote(chatgpt_return, safe='')}"
        return_url = hosted_url

        amount_info = stripe_amount_details(init)
        expected = str(amount_info.get("amount") if amount_info.get("amount") is not None else 0)

        body = {
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": str(init.get("init_checksum") or ""),
            "version": self.runtime_version,
            "expected_amount": expected,
            "expected_payment_method_type": "paypal",
            "return_url": return_url,
            "elements_session_client[session_id]": self.elements_session_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": self.stripe_js_id,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "client_attribution_metadata[client_session_id]": self.stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": self.elements_session_config_id,
            "client_attribution_metadata[elements_session_id]": self.elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": self.elements_session_config_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "consent[terms_of_service]": "accepted",
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        r = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code >= 400:
            diagnostics = stripe_confirm_error_diagnostics(r, cs_id, pm_id, init)
            self._log("confirm", diagnostics)
            raise PayPalHttpError(
                "confirm",
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                r,
                retryable=r.status_code in {408, 425, 429} or r.status_code >= 500,
            )
        return r.json()

    # ─── Stage 3: Approve (目标国代理) + 轮询 redirect ─────────────────────

    def _chatgpt_approve(self, cs_id: str, processor_entity: str):
        self._active_stage = "approve"
        self._log("approve", f"Stage 3: proxy={redact_proxy_url(self.approve_proxy)} ChatGPT approve")
        cs = self._approve_session()
        cs.headers.update(
            {
                "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            }
        )
        sentinel_headers = self._fresh_approval_sentinel(cs)
        # sentinel ping
        try:
            cs.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                headers={
                    **sentinel_headers,
                    "x-openai-target-path": "/backend-api/sentinel/ping",
                    "x-openai-target-route": "/backend-api/sentinel/ping",
                },
                timeout=CHATGPT_TIMEOUT,
            )
        except Exception:
            pass
        endpoint = "https://chatgpt.com/backend-api/payments/checkout/approve"
        approval_headers = {
            **sentinel_headers,
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        }
        try:
            r = cs.post(
                endpoint,
                json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
                headers=approval_headers,
                timeout=CHATGPT_TIMEOUT,
            )
        except TypeError as exc:
            # Small injected transports used by integrations may not expose a
            # per-request headers parameter. Preserve the same session-bound
            # headers without weakening production requests.
            if "headers" not in str(exc):
                raise
            cs.headers.update(approval_headers)
            r = cs.post(
                endpoint,
                json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
                timeout=CHATGPT_TIMEOUT,
            )
        if r.status_code == 409:
            payload = {}
            try:
                payload = r.json() or {}
            except Exception:
                pass
            if (
                str(payload.get("result") or "").strip().lower() == "blocked"
                or "blocked" in str(getattr(r, "text", "") or "").lower()
            ):
                raise CheckoutApprovalBlockedError(endpoint, r)
        if r.status_code >= 400:
            raise PayPalHttpError(
                "approve",
                endpoint,
                r,
                retryable=r.status_code in {408, 425, 429} or r.status_code >= 500,
            )
        result = (r.json() or {}).get("result")
        if str(result or "").strip().lower() == "blocked":
            raise CheckoutApprovalBlockedError(endpoint, r)
        if result != "approved":
            raise PayPalHttpError("approve", endpoint, r, retryable=False)
        self._log("approve", "ChatGPT approve 成功")

    def _poll_payment_page(self, cs_id: str, timeout_seconds: float = 45) -> str:
        """轮询 Stripe payment page 获取 redirect URL。"""
        self._log("poll", f"轮询 payment page (超时 {timeout_seconds}s)")
        stripe = getattr(self, "_stripe_session", None) or _new_session(self.provider_proxy)
        deadline = time.time() + timeout_seconds
        params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": self.elements_session_id,
            "elements_session_client[stripe_js_id]": self.stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            r = stripe.get(f"https://api.stripe.com/v1/payment_pages/{cs_id}", params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                payload = r.json() or {}
                url = extract_redirect_url(payload)
                if url:
                    self._log("poll", f"第 {poll_count} 次轮询发现 redirect URL")
                    return url
                # 检查 submission 状态
                submission = payload.get("submission_attempt") or {}
                if isinstance(submission, dict):
                    state = submission.get("state")
                    if state == "requires_approval":
                        raise Exception("requires_approval")
                    if state == "failed":
                        raise Exception(f"submission failed: {submission}")
            if poll_count % 5 == 0:
                self._log("poll", f"第 {poll_count} 次轮询...")
            time.sleep(1)
        raise Exception(f"轮询超时 ({timeout_seconds}s)")

    def _run_provider_stages(self, cs_id: str) -> tuple[dict, str, dict]:
        base_proxies = {
            "provider": self.provider_proxy,
            "stripe_init": self.stripe_init_proxy,
            "payment_method": self.payment_method_proxy,
            "confirm": self.confirm_proxy,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_stage_retries + 1):
            # Each request stage claims ``_active_stage`` itself, so an
            # unclaimed marker means no provider request was sent yet.
            self._active_stage = ""
            prep_stage = ""
            try:
                # Egress preparation issues no provider request, so a failure
                # here is always safe to retry -- including for ``confirm``.
                for prep_stage in _PROVIDER_STAGES:
                    setattr(
                        self,
                        f"{prep_stage}_proxy",
                        self._prepare_stage_proxy(prep_stage, base_proxies[prep_stage], attempt),
                    )
                prep_stage = ""
                self._reset_stripe_context()
                self._stripe_session = _new_session(self.stripe_init_proxy)
                try:
                    init = self._stripe_init(cs_id, enforce_zero=False)
                except TypeError as exc:
                    # Preserve compatibility with lightweight injected stage
                    # functions that still expose the pre-qualification
                    # signature.
                    if "enforce_zero" not in str(exc):
                        raise
                    init = self._stripe_init(cs_id)
                stripe_hosted_url = str(init.get("stripe_hosted_url") or "")
                self._log("stripe_init", f"stripe_hosted_url={stripe_hosted_url[:80]}...")
                pm_id = self._create_payment_method(cs_id)
                confirm_data = self._stripe_confirm(cs_id, pm_id, init)
                for stage, proxy in (
                    ("provider", self.provider_proxy),
                    ("stripe_init", self.stripe_init_proxy),
                    ("payment_method", self.payment_method_proxy),
                    ("confirm", self.confirm_proxy),
                ):
                    self._record_stage_result(stage, proxy, True)
                return init, stripe_hosted_url, confirm_data
            except Exception as exc:
                last_error = exc
                stage = self._active_stage or prep_stage or "provider"
                failed_proxy = getattr(self, f"{stage}_proxy", self.provider_proxy)
                self._record_stage_result(stage, failed_proxy, False, str(exc))
                # confirm 是副作用阶段：请求可能已被 Stripe 接收并生效，
                # 网络抖动返回 5xx/超时时重试会重复发起支付意图。
                # 对齐 wallet_provider 的约定 —— 副作用阶段失败一律不重试，
                # 由上层标记 unknown 并要求对账后再决定是否重试。
                if not prep_stage and stage in _SIDE_EFFECT_STAGES and not isinstance(exc, PayPalHttpError):
                    self._log(
                        stage,
                        f"side-effect stage '{stage}' failed after request was sent; "
                        "not retrying to avoid duplicate payment. Requires reconciliation.",
                    )
                    raise PaymentOutcomeUnknownError(
                        f"{stage}_outcome_unknown: {exc}",
                        stage=stage,
                        error_code=f"{stage}_outcome_unknown",
                    ) from exc
                retryable = (
                    bool(getattr(exc, "retryable", False))
                    or is_retryable_network_error(exc)
                    or "proxy_preflight_failed" in str(exc)
                )
                self._log(stage, f"stage attempt {attempt}/{self.max_stage_retries} failed: {exc}")
                if not retryable or attempt >= self.max_stage_retries:
                    raise
                self._log(stage, "retrying provider stages with new proxy sessions")
        assert last_error is not None
        raise last_error

    def _prepare_approve_proxy(self) -> None:
        """Resolve the approve egress before any side effect is issued.

        Proxy preparation is the only part of Stage 3 that runs before the
        approve POST, so it is the only part that may be retried with a fresh
        session.
        """
        base_proxy = self.approve_proxy
        for attempt in range(1, self.max_stage_retries + 1):
            try:
                self.approve_proxy = self._prepare_stage_proxy("approve", base_proxy, attempt)
                return
            except Exception as exc:
                self._record_stage_result("approve", self.approve_proxy, False, str(exc))
                self._log("approve", f"approve 代理第 {attempt} 次准备失败: {exc}")
                if attempt >= self.max_stage_retries:
                    raise

    def _approve_and_poll(self, cs_id: str, processor_entity: str) -> str:
        """Approve once, then poll for the provider redirect URL.

        This never re-drives approval on the same Checkout. An explicit
        ``blocked`` response is a known rejection and is handled by rebuilding
        the complete workflow; ambiguous failures remain reconciliation cases.
        """
        self._prepare_approve_proxy()
        try:
            self._chatgpt_approve(cs_id, processor_entity)
        except CheckoutApprovalBlockedError:
            raise
        except Exception as exc:
            self._record_stage_result("approve", self.approve_proxy, False, str(exc))
            self._log("approve", f"approve 失败，不重试以避免重复授权: {exc}")
            raise PaymentOutcomeUnknownError(
                f"approve_outcome_unknown: {exc}",
                stage="approve",
                error_code="approve_outcome_unknown",
            ) from exc
        if self.enable_promotion:
            self.promotion_applied = self.checkout_update_promotion(cs_id, processor_entity)
            if self.promotion_applied and self.promotion_taxes:
                self._checkout_update_taxes(cs_id, processor_entity)
            if self.require_zero:
                self._stripe_init(cs_id, enforce_zero=True)
        try:
            redirect_url = self._poll_payment_page(cs_id, timeout_seconds=45)
        except Exception as exc:
            self._record_stage_result("approve", self.approve_proxy, False, str(exc))
            self._log("poll", f"approve 已提交但轮询未取到 redirect，不重发 approve: {exc}")
            raise PaymentOutcomeUnknownError(
                f"approve_submitted_poll_incomplete: {exc}",
                stage="poll",
                error_code="approve_poll_outcome_unknown",
            ) from exc
        self._record_stage_result("approve", self.approve_proxy, True)
        return redirect_url

    # ─── 主流程 ────────────────────────────────────────────────────────────

    def _extract_once(self) -> dict:
        """Run one isolated Checkout -> confirm -> approve workflow."""
        checkout = self._create_checkout()
        cs_id = checkout["cs_id"]
        processor_entity = checkout["processor_entity"]

        # ``oaics_`` is ChatGPT's native Checkout contract, not a Stripe
        # payment-page session. It is already a usable checkout link and must
        # never be sent to Stripe's /payment_pages/{id}/init endpoint.
        if cs_id.startswith("oaics_"):
            if self.enable_promotion:
                self.promotion_applied = self.checkout_update_promotion(cs_id, processor_entity)
            return {
                "ok": True,
                "link_type": "chatgpt_checkout_link",
                "url": self._checkout_page_url(cs_id, processor_entity),
                "ba_token": "",
                "cs_id": cs_id,
                "amount": None,
                "currency": self.checkout_currency,
                "target_country": self.target_country,
                "checkout_country": self.checkout_country,
                "side_effect_started": False,
                "checkout_proxy": redact_proxy_url(self.checkout_proxy),
                "promotion_proxy": redact_proxy_url(self.promotion_proxy),
                "promotion_applied": self.promotion_applied,
                "provider_proxy": "",
                "stripe_init_proxy": "",
                "payment_method_proxy": "",
                "confirm_proxy": "",
                "approve_proxy": "",
                "proxy_exits": self.proxy_exits,
                "workflow_attempt": self.workflow_attempt,
            }

        # Stage 2: Stripe init + create PM + confirm. Each stage can use its own
        # egress while the Stripe session keeps its cookies and identifiers.
        init, stripe_hosted_url, confirm_data = self._run_provider_stages(cs_id)

        # Standard flow: confirm is followed by exactly one approval submission.
        # A redirect returned by confirm is only a hint; approval remains the
        # authoritative ChatGPT checkout transition.
        self._log("approve", "confirm 完成，提交 ChatGPT checkout approval")
        try:
            redirect_url = self._approve_and_poll(cs_id, processor_entity)
        except PaymentOutcomeUnknownError:
            raise
        except CheckoutApprovalBlockedError:
            # A blocked approval invalidates this Checkout. Never downgrade it
            # to a hosted link and never retry approval on the same session;
            # the outer workflow rebuilds a fresh Checkout.
            raise
        except CheckoutNotZeroDueError:
            raise
        except Exception as e:
            if not stripe_hosted_url:
                raise
            self.proxy_state.record_pair_result(
                self.checkout_proxy,
                self.provider_proxy,
                self.approve_proxy,
                False,
                "approve_fallback_to_hosted",
            )
            self._log("approve", f"approve 前置失败，降级返回 stripe_hosted_url: {e}")
            return {
                "ok": True,
                "link_type": "stripe_hosted",
                "url": stripe_hosted_url,
                "ba_token": "",
                "cs_id": cs_id,
                "amount": stripe_amount_details(init).get("amount"),
                "currency": self.currency,
                "target_country": self.target_country,
                "checkout_country": self.checkout_country,
            }

        # Stage 3: 跟随 redirect 提取 PayPal BA approve URL
        # 复用 Stripe session 保持 cookies
        if not is_paypal_ba_approve_url(redirect_url):
            self._log("redirect", f"跟随 redirect 链提取 BA URL: {redirect_url[:80]}...")
            redirect_url = resolve_external_redirect(self._stripe_session, redirect_url)

        if not is_paypal_ba_approve_url(redirect_url):
            # confirm or approve already ran by this point, so a missing BA URL
            # is an unresolved authorization rather than a clean failure.
            raise PaymentOutcomeUnknownError(
                f"未提取到 PayPal BA approve URL: {redirect_url[:200]}",
                stage="follow_redirect",
                error_code="ba_redirect_outcome_unknown",
            )

        ba_token = extract_ba_token(redirect_url)
        self._log("done", "Payment approval token captured")
        self.proxy_state.record_pair_result(
            self.checkout_proxy,
            self.provider_proxy,
            self.approve_proxy,
            True,
        )

        return {
            "ok": True,
            "link_type": "paypal_ba_approve",
            "url": redirect_url,
            "ba_token": ba_token,
            "cs_id": cs_id,
            "amount": stripe_amount_details(init).get("amount"),
            "currency": self.currency,
            "target_country": self.target_country,
            "checkout_country": self.checkout_country,
            "checkout_proxy": redact_proxy_url(self.checkout_proxy),
            "promotion_proxy": redact_proxy_url(self.promotion_proxy),
            "provider_proxy": redact_proxy_url(self.provider_proxy),
            "stripe_init_proxy": redact_proxy_url(self.stripe_init_proxy),
            "payment_method_proxy": redact_proxy_url(self.payment_method_proxy),
            "confirm_proxy": redact_proxy_url(self.confirm_proxy),
            "approve_proxy": redact_proxy_url(self.approve_proxy),
            "proxy_exits": self.proxy_exits,
            "promotion_applied": self.promotion_applied,
            "workflow_attempt": self.workflow_attempt,
            "last_retry_error": self.last_retry_error,
        }

    def extract(self) -> dict:
        """Run the complete workflow, rebuilding Checkout after an explicit block."""
        for attempt in range(1, self.max_checkout_retries + 1):
            self.workflow_attempt = attempt
            self._reset_workflow_context()
            try:
                result = self._extract_once()
                result["workflow_attempt"] = self.workflow_attempt
                result["last_retry_error"] = dict(self.last_retry_error or {})
                return result
            except CheckoutApprovalBlockedError as exc:
                self.last_retry_error = {
                    "error_code": exc.error_code,
                    "error_stage": exc.error_stage,
                    "retryable": True,
                    **exc.diagnostic(),
                }
                self._log(
                    "approve",
                    f"checkout approval blocked; rebuilding OpenAI Checkout ({attempt}/{self.max_checkout_retries})",
                    **self.last_retry_error,
                )
                if attempt >= self.max_checkout_retries:
                    raise
        raise RuntimeError("paypal checkout workflow exhausted")

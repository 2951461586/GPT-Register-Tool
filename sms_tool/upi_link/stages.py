"""Upi QR link stage body, moved verbatim out of `pipeline.py` (2026-10-03).

The 1138-line body of the former `pipeline._generate_upi_qr_link_once` lives here
unchanged; every module-level callable it used to resolve is now reached through
`ops.<name>`, where `ops` is an :class:`~sms_tool.upi_link.operations.UpiOperations`
bundle assembled by `pipeline._build_upi_operations()` **at call time**.  That is
what keeps the historical `monkeypatch.setattr(pipeline, "<name>", ...)` seams in
the tests working after the split.

Only `pipeline` imports this module; it re-exports nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import json
import time
import uuid

try:  # pragma: no cover - direct script execution
    from ..checkout_contract import PLUS_TRIAL_CAMPAIGN_ID
except ImportError:  # pragma: no cover
    from checkout_contract import PLUS_TRIAL_CAMPAIGN_ID  # type: ignore

try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_STRIPE_PK, DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:  # pragma: no cover
    from pp_link_helpers import (  # type: ignore
        CHATGPT_TIMEOUT,
        DEFAULT_STRIPE_PK,
        DEFAULT_TIMEOUT,
        STRIPE_VERSION,
    )

try:  # pragma: no cover - direct script execution
    from .constants import (
        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
        STRIPE_PAYMENT_PAGE_GET_URL_T,
        UPI_CHECKOUT_APPROVE_URL,
        UPI_CHECKOUT_CONFIRM_URL,
        UPI_CHECKOUT_URL,
        UPI_SENTINEL_APPROVAL_FLOW,
    )
    from .operations import UpiOperations
    from .verify import INCONCLUSIVE as UPI_VERIFY_INCONCLUSIVE
except ImportError:  # pragma: no cover
    from constants import (  # type: ignore
        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
        STRIPE_PAYMENT_PAGE_GET_URL_T,
        UPI_CHECKOUT_APPROVE_URL,
        UPI_CHECKOUT_CONFIRM_URL,
        UPI_CHECKOUT_URL,
        UPI_SENTINEL_APPROVAL_FLOW,
    )
    from operations import UpiOperations  # type: ignore
    from verify import INCONCLUSIVE as UPI_VERIFY_INCONCLUSIVE  # type: ignore

from .context import UpiStageContext


def _absorb_mandate(ops: UpiOperations, state: UpiStageContext, *sources: Any) -> bool:
    """在 sources 里找 ``seti_`` SetupIntent 并直连补交 mandate。

    幂等：成功一次即封口；找不到 SetupIntent 时不动。返回 True 表示确实
    发起了补交（调用方据此判断是否值得再轮询一轮），**不代表补交成功**。
    """

    if state.mandate_ok or not state.local_mandate_enabled:
        return False
    setup_intent: Any = {}
    for source in sources:
        candidate = ops._upi_first_value_by_key(source, "setup_intent")
        if isinstance(candidate, Mapping) and str(candidate.get("id") or "").startswith("seti_"):
            setup_intent = candidate
            break
    if not setup_intent:
        return False
    mandate = ops._upi_confirm_local_mandate(
        state.stripe,
        setup_intent=setup_intent,
        pm_id=state.pm_id,
        return_url=state.return_url,
        stripe_pk=state.stripe_pk,
        amount=state.ctx.get("checkout_amount"),
    )
    last_variant = mandate["variants"][-1]["variant"] if mandate["variants"] else "-"
    state.emit(
        "mandate",
        "Stage 6b: SetupIntent mandate %s via %s (%s)"
        % (
            "ok" if mandate["ok"] else "failed",
            last_variant,
            str(mandate.get("error") or "no error")[:160],
        ),
    )
    state.mandate_ok = bool(mandate["ok"])
    if mandate.get("payload"):
        # 让 Stage 7 的 _absorb 吃下 mandate 响应里的 upi:// / 跳转。
        state.approval_data = {**(state.approval_data or {}), "local_mandate": mandate["payload"]}
    return True


def _stage_pre_exit(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Increment 1: India exit selection + checkout admission ──────────
    # Runs before the shared egress gate so the gate validates the exit this
    # selector actually chose (a rotated session), not the pre-rotation one.
    if ops._upi_india_exit_probe_enabled(state.upi_cfg):
        _exit_attempts = ops._upi_india_exit_attempts(state.upi_cfg)
        _exit_timeout = ops._upi_india_exit_timeout(state.upi_cfg)
        state._rc.checkout_proxy = ops._upi_select_india_exit(
            state._rc.checkout_proxy, country=state.checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=state.emit
        )
        state._rc.provider_proxy = ops._upi_select_india_exit(
            state._rc.provider_proxy, country=state.checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=state.emit
        )
        state._rc.approve_proxy = ops._upi_select_india_exit(
            state._rc.approve_proxy, country=state.checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=state.emit
        )
        state.checkout_proxy = str(state._rc.checkout_proxy or "")
        state.provider_proxy = str(state._rc.provider_proxy or "")
        state.approve_proxy = str(state._rc.approve_proxy or "")

    # First point at which the retargeted stage proxies are known, and before
    # Stage 1 creates a Checkout session -- the last moment a mis-routed exit
    # can be rejected for the price of one probe instead of a disposable
    # session.
    _egress_failure = ops._upi_assert_egress_contract(state._rc)
    if _egress_failure is not None:
        return _egress_failure
    return None


def _stage_checkout(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 1: ChatGPT checkout ────────────────────────────────────
    state.emit(
        "checkout",
        f"Stage 1: using {ops.redact_proxy_text(state.checkout_proxy or 'DIRECT', state.checkout_proxy)} for UPI checkout (ui_mode={state.checkout_ui_mode})",
    )
    state.cs = ops._upi_new_chatgpt_session(state.checkout_proxy, state.fingerprint, state.device_id, state.session_token)
    state.cs.headers.update(
        {
            "Authorization": f"Bearer {state.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
            # 网关路由头（参考实现在请求级传；本 session 只打这一个
            # 端点，放 session 头等价且不破坏 FakeSession.post 签名）
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        }
    )
    state.risk = ops._upi_capture_risk_context(
        state.cs,
        access_token=state.access_token,
        device_id=state.device_id,
        page_url=ops._upi_promo_page_url(),
        fingerprint=state.fingerprint,
        proxy=state.checkout_proxy,
        session_token=state.session_token,
        use_browser=state.browser_rail,
        sentinel_flow=ops._upi_ensure_checkout_flow(),
    )
    state.cs.headers.update(state.risk.headers(account_id=ops._upi_account_id_from_token(state.access_token)))
    state.checkout_sentinel_headers = ops._upi_sentinel_headers(
        state.cs,
        state.device_id,
        state.checkout_proxy,
        flow=ops._upi_ensure_checkout_flow(),
        supplied_token=state.risk.sentinel_tokens.get(ops._upi_ensure_checkout_flow(), ""),
        fingerprint=state.fingerprint,
        page_url=ops._upi_promo_page_url(),
    )
    state.cs.headers.update(state.checkout_sentinel_headers)
    ops._upi_sentinel_ping(state.cs, proxy=state.checkout_proxy, referer="https://chatgpt.com/")
    checkout_body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": state.checkout_country, "currency": state.currency},
        "promo_campaign": {"promo_campaign_id": PLUS_TRIAL_CAMPAIGN_ID, "is_coupon_from_query_param": False},
        "checkout_ui_mode": state.checkout_ui_mode,
    }
    r = state.cs.post(UPI_CHECKOUT_URL, json=checkout_body, timeout=CHATGPT_TIMEOUT)
    ops._upi_dump_http(r, "chatgpt_checkout", checkout_body, "POST", UPI_CHECKOUT_URL, force=r.status_code >= 400)
    if r.status_code == 401:
        return {
            "ok": False,
            "error": "access_token invalid or expired (401)",
            "error_code": "checkout_unauthorized",
            "payment_method": "upi",
        }
    if r.status_code >= 400:
        return {
            "ok": False,
            "error": f"checkout failed: {r.status_code} {r.text[:300]}",
            "error_code": "checkout_failed",
            "payment_method": "upi",
        }
    state.checkout_data = r.json() or {}
    state.cs_id = str(state.checkout_data.get("checkout_session_id") or state.checkout_data.get("id", "") or "")
    # Accept both rails the backend can pick: ``cs_*`` (Stripe payment_pages,
    # custom) and ``oaics_*`` (deferred-intent Elements). The prefix is the
    # reliable signal; the response's checkout_ui_mode string can disagree.
    if not state.cs_id.startswith(("cs_", "oaics_")):
        return {
            "ok": False,
            "error": f"checkout response missing cs_id: {json.dumps(state.checkout_data, ensure_ascii=False)[:200]}",
            "error_code": "checkout_bad_response",
            "payment_method": "upi",
        }
    state.stripe_pk = state.checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
    state.processor_entity = state.checkout_data.get("processor_entity") or (
        "openai_llc" if state.checkout_country == "US" else "openai_ie"
    )
    state.emit("checkout", f"checkout success: cs_id={state.cs_id}")
    state.is_oaics = state.cs_id.startswith("oaics_")
    state._oaics_state: dict[str, Any] = {}
    if state.is_oaics:
        # oaics_ sessions carry a CustomerSession secret instead of a
        # Checkout client secret; refresh the authenticated state once so
        # the deferred-intent rail sees the authoritative method list.
        state.checkout_ui_mode = "deferred"
        state._oaics_state = ops._upi_fetch_oaics_state(
            state.cs, state.access_token, cs_id=state.cs_id, processor_entity=state.processor_entity, proxy=state.checkout_proxy
        )
        state.emit(
            "oaics",
            f"oaics_ session: state refreshed ({len(state._oaics_state)} keys)"
            if state._oaics_state
            else "oaics_ session: state unavailable",
        )

    return None


def _stage_stripe_init(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 2: Stripe init (custom mode) ───────────────────────────
    state.emit(
        "stripe_init",
        f"Stage 2: using {ops.redact_proxy_text(state.provider_proxy or 'DIRECT', state.provider_proxy)} for Stripe init",
    )
    state.stripe = ops._new_session(state.provider_proxy)
    ops._upi_apply_fingerprint(state.stripe, state.fingerprint)
    state.stripe_js_id = uuid.uuid4().hex
    if ops._env_bool("UPI_STRIPE_FINGERPRINT", True):
        # Stripe.js 在任何业务请求之前先向 m.stripe.com/6 登记设备指纹；
        # 「从未登记过」是 Radar 能直接看出来的异常（文档 §7.1）。
        ops._upi_register_stripe_fingerprint(state.stripe, state.stripe_js_id, state.fingerprint)
    if state.is_oaics:
        customer_session_secret = str(
            state.checkout_data.get("customer_session_client_secret")
            or state.checkout_data.get("customer_session_secret")
            or (state._oaics_state.get("customer_session_client_secret") if isinstance(state._oaics_state, Mapping) else "")
            or (state._oaics_state.get("customer_session_secret") if isinstance(state._oaics_state, Mapping) else "")
            or ""
        )
        if not customer_session_secret:
            return {
                "ok": False,
                "error": "oaics_ checkout missing customer_session_client_secret",
                "error_code": "oaics_prerequisites_missing",
                "payment_method": "upi",
                "cs_id": state.cs_id,
            }
        state.init = ops._upi_elements_session(
            state.stripe,
            stripe_pk=state.stripe_pk,
            amount=(state._oaics_state.get("amount") if isinstance(state._oaics_state, Mapping) else 0),
            stripe_js_id=state.stripe_js_id,
            customer_session_secret=customer_session_secret,
            fingerprint=state.fingerprint,
        )
        if not state.init:
            return {
                "ok": False,
                "error": "oaics_ elements session negotiation failed",
                "error_code": "oaics_elements_failed",
                "payment_method": "upi",
                "cs_id": state.cs_id,
            }
    else:
        state.init = ops._upi_stripe_init(state.stripe, state.cs_id, state.stripe_pk, state.fingerprint, state.stripe_js_id)
    state.ctx = ops._upi_build_ctx(state.init, state.fingerprint, state.stripe_js_id)
    # Stripe's passive hCaptcha token cannot be forged offline; a confirm
    # without it is answered later at the payment-setup stage with
    # ``setup_attempt_failed`` / ``generic_decline``. Solve it here (once)
    # and let ``_upi_build_confirm_body`` emit the fields.
    _passive = ops._upi_passive_captcha_fields(
        state.init,
        proxy=state.provider_proxy or state.checkout_proxy,
        locale=str(state.fingerprint.get("locale") or "en-US"),
    )
    if _passive is not None:
        state.ctx["passive_captcha"] = _passive
    # Prefer the Stripe identifiers the live browser actually used so the
    # confirm request stays consistent with the page that minted them.
    for _key in ("guid", "muid", "sid"):
        if state.risk.stripe_ids.get(_key) and not state.ctx.get(_key):
            state.ctx[_key] = state.risk.stripe_ids[_key]
    state.emit("stripe_init", "init success, analyzing free trial...")
    if state.require_server_upi_mandate:
        state.mandate = ops._upi_server_mandate(state.init)
        state.emit(
            "mandate",
            "server mandate_options="
            + ("present" if state.mandate else "missing")
            + (f" end_date={state.mandate.get('end_date')}" if state.mandate else ""),
        )

    return None


def _stage_free_trial(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 3: Free trial detection ────────────────────────────────
    state.ft_status = ops._upi_get_free_trial_status(state.init)
    state.amount = state.ft_status["due"]
    state.pm_types = state.ft_status["payment_method_types"]
    state.emit(
        "stripe_init",
        f"free_trial={state.ft_status['has_free_trial']} due={state.amount} coupon={state.ft_status['coupon_name']} upi={state.ft_status['has_upi']}",
    )
    # 0 元缓存：本轮 checkout 代理有没有产出免费试用，记下来供后续批次调度。
    # 必须在 ``require_zero`` 的早退**之前**记——那次失败恰恰是最有价值的
    # 负样本（"这个代理出的是非零"），早退掉就永远学不到。
    ops._upi_record_zero_result(state.proxy_state, state.checkout_proxy, state.checkout_country, state.amount)
    if state.require_zero and not state.ft_status["has_free_trial"]:
        return {
            "ok": False,
            "error": f"no_free_trial: due={state.amount} coupon={state.ft_status['coupon_name']} percent_off={state.ft_status['percent_off']}",
            "error_code": "no_free_trial",
            "payment_method": "upi",
            "cs_id": state.cs_id,
            "amount": state.amount,
            "currency": state.payment_currency.upper(),
            "target_country": state.target_country,
            "checkout_country": state.checkout_country,
            "billing_country": state.checkout_country,
            "payment_country": state.payment_country,
            "coupon_name": state.ft_status["coupon_name"],
            "percent_off": state.ft_status["percent_off"],
        }
    if state.pm_types and not state.ft_status["has_upi"]:
        return {
            "ok": False,
            "error": f"UPI not available for checkout; payment_method_types={state.pm_types}",
            "error_code": "upi_not_available",
            "payment_method": "upi",
            "cs_id": state.cs_id,
            "payment_method_types": state.pm_types,
            "amount": state.amount,
            "currency": state.payment_currency.upper(),
            "target_country": state.target_country,
            "checkout_country": state.checkout_country,
            "billing_country": state.checkout_country,
            "payment_country": state.payment_country,
        }

    return None


def _stage_cpmt(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 3b: cpmt bypass ────────────────────────────────────────
    # When Checkout advertises a UPI custom payment method, prefer the
    # ChatGPT-side confirm+start rail. It avoids the Stripe SetupIntent
    # stage entirely, which is the known source of generic_decline.
    cpm_id = ops._upi_custom_payment_method_id(state.checkout_data.get("custom_payment_methods"))
    if cpm_id:
        state.emit("cpmt", f"Checkout advertises UPI custom method {cpm_id}; using the no-SetupIntent rail")
        return ops._upi_run_cpmt_flow(
            state.cs,
            access_token=state.access_token,
            cs_id=state.cs_id,
            cpm_id=cpm_id,
            processor_entity=state.processor_entity,
            amount=state.amount,
            payment_currency=state.payment_currency,
            target_country=state.target_country,
            checkout_country=state.checkout_country,
            payment_country=state.payment_country,
            device_id=state.device_id,
            checkout_proxy=state.checkout_proxy,
            qr_path=state.qr_path or "",
        )

    return None


def _stage_oaics(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 3c: OAICS / deferred rail ─────────────────────────────
    # An ``oaics_`` session has no Stripe Checkout client secret; its
    # confirm is the reference's two-step ``/v1/confirmation_tokens`` ->
    # ChatGPT ``/checkout/confirm`` sequence, and only that rail accepts
    # the UPI mandate acceptance fields. See ``_upi_run_oaics_flow`` for
    # the measured reachability caveat.
    if state.is_oaics:
        state.emit("oaics", "OAICS rail: confirmation_tokens -> chatgpt confirm -> payment_intent confirm")
        return ops._upi_run_oaics_flow(
            state.stripe,
            state.cs,
            access_token=state.access_token,
            device_id=state.device_id,
            cs_id=state.cs_id,
            stripe_pk=state.stripe_pk,
            processor_entity=state.processor_entity,
            billing=state.billing,
            fingerprint=state.fingerprint,
            ctx=state.ctx,
            elements=state.init if isinstance(state.init, Mapping) else {},
            oaics_state=state._oaics_state,
            amount=state.amount,
            payment_currency=state.payment_currency,
            target_country=state.target_country,
            checkout_country=state.checkout_country,
            payment_country=state.payment_country,
            checkout_proxy=state.checkout_proxy,
            provider_proxy=state.provider_proxy,
            approve_proxy=state.approve_proxy,
            qr_path=state.qr_path,
            wait_paid=state.wait_paid,
            paid_timeout=state.paid_timeout,
        )

    return None


def _stage_tax_customer(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 4: Tax region + customer data sync ─────────────────────
    state.tax_body: dict[str, str] = {}
    if state.update_tax_region:
        state.emit("tax_region", "Stage 4: updating tax region to IN")
        state.tax_body = {
            "tax_region[country]": str(state.billing.get("country") or "IN"),
            "tax_region[postal_code]": str(state.billing.get("postal_code") or ""),
            "tax_region[state]": str(state.billing.get("state") or ""),
            "tax_region[city]": str(state.billing.get("city") or ""),
            "tax_region[line1]": str(state.billing.get("line1") or ""),
            "key": state.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
            **ops._upi_elements_session_params(state.ctx),
        }
        if state.billing.get("line2"):
            state.tax_body["tax_region[line2]"] = str(state.billing["line2"])
        try:
            tax_resp = state.stripe.post(
                STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                data=state.tax_body,
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as exc:
            state.emit("tax_region", f"tax region transport error (non-fatal): {type(exc).__name__}: {exc}")
            tax_resp = None
        if tax_resp is not None:
            ops._upi_dump_http(
                tax_resp,
                "stripe_tax_region",
                state.tax_body,
                "POST",
                STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                force=tax_resp.status_code >= 400,
            )
            if tax_resp.status_code >= 400:
                state.emit(
                    "tax_region",
                    f"tax region update failed (non-fatal): {tax_resp.status_code} {tax_resp.text[:200]}",
                )
            else:
                state.emit("tax_region", "tax region updated")
                state.refreshed = tax_resp.json() or {}
                if isinstance(state.refreshed, dict) and state.refreshed:
                    state.init = state.refreshed
                    state.ctx = ops._upi_rebuild_ctx(state.ctx, state.init, state.fingerprint, state.stripe_js_id)

    return None


def _stage_repeat_tax(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 4.5 (Increment 2): re-init + re-tax before confirm ──────
    # The reference's ``init -> tax -> init -> tax`` order.  A second init
    # re-reads Stripe's price after the region change (the first tax
    # response can still carry the pre-update amount) and is measured to
    # make the approve step pass.  Both calls are non-fatal: a failed
    # re-read must not abort a run whose first tax already succeeded.
    if state.update_tax_region and ops._upi_repeat_tax_region(state.upi_cfg):
        state.emit("tax_region", "Stage 4.5: re-init + re-tax before confirm")
        try:
            state.refreshed_init = ops._upi_stripe_init(state.stripe, state.cs_id, state.stripe_pk, state.fingerprint, state.stripe_js_id)
        except Exception as exc:
            state.refreshed_init = None
            state.emit("tax_region", f"Stage 4.5 re-init skipped (non-fatal): {type(exc).__name__}: {exc}")
        if isinstance(state.refreshed_init, dict) and state.refreshed_init:
            state.init = state.refreshed_init
            state.ctx = ops._upi_rebuild_ctx(state.ctx, state.init, state.fingerprint, state.stripe_js_id)
            try:
                retax_resp = state.stripe.post(
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                    data=state.tax_body,
                    timeout=DEFAULT_TIMEOUT,
                )
                ops._upi_dump_http(
                    retax_resp,
                    "stripe_tax_region_2",
                    state.tax_body,
                    "POST",
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                    force=retax_resp.status_code >= 400,
                )
                if retax_resp.status_code < 400:
                    state.refreshed = retax_resp.json() or {}
                    if isinstance(state.refreshed, dict) and state.refreshed:
                        state.init = state.refreshed
                        state.ctx = ops._upi_rebuild_ctx(state.ctx, state.init, state.fingerprint, state.stripe_js_id)
                    state.emit("tax_region", "Stage 4.5: re-init + re-tax ok")
                else:
                    state.emit("tax_region", f"Stage 4.5 re-tax failed (non-fatal): {retax_resp.status_code}")
            except Exception as exc:
                state.emit("tax_region", f"Stage 4.5 re-tax error (non-fatal): {type(exc).__name__}: {exc}")

    if state.update_customer_data:
        state.emit("customer_data", "Stage 4: submitting IN customer_data")
        customer_body: dict[str, str] = {
            "customer_data[email]": str(state.billing.get("email") or ""),
            "customer_data[name]": str(state.billing.get("name") or ""),
            "customer_data[address][country]": str(state.billing.get("country") or "IN"),
            "customer_data[address][line1]": str(state.billing.get("line1") or ""),
            "customer_data[address][city]": str(state.billing.get("city") or ""),
            "customer_data[address][postal_code]": str(state.billing.get("postal_code") or ""),
            "expected_amount": str(state.ctx.get("checkout_amount") or 0),
            "key": state.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
            **ops._upi_elements_session_params(state.ctx),
        }
        if state.billing.get("state"):
            customer_body["customer_data[address][state]"] = str(state.billing["state"])
        if state.billing.get("line2"):
            customer_body["customer_data[address][line2]"] = str(state.billing["line2"])
        try:
            cd_resp = state.stripe.post(
                STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                data=customer_body,
                timeout=DEFAULT_TIMEOUT,
            )
            ops._upi_dump_http(
                cd_resp,
                "stripe_customer_data",
                customer_body,
                "POST",
                STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=state.cs_id),
                force=cd_resp.status_code >= 400,
            )
            if cd_resp.status_code >= 400:
                state.emit(
                    "customer_data", f"customer_data failed (non-fatal): {cd_resp.status_code} {cd_resp.text[:200]}"
                )
            else:
                state.emit("customer_data", "customer_data submitted")
        except Exception as exc:
            state.emit("customer_data", f"customer_data transport error (non-fatal): {type(exc).__name__}: {exc}")

    return None


def _stage_confirm(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 5: Stripe confirm ──────────────────────────────────────
    state.pm_id = ""
    if state.inline_pm:
        state.emit("stripe_confirm", "Stage 5: Stripe confirm with inline UPI payment method")
    else:
        state.pm_id = ops._upi_create_upi_pm(state.stripe, state.cs_id, state.stripe_pk, state.billing)
        state.emit("stripe_confirm", f"Stage 5: Stripe confirm referencing pm={state.pm_id}")

    state.return_url = (
        ops._normalize_hosted_checkout_url(str(state.init.get("stripe_hosted_url") or ""))
        or f"https://chatgpt.com/checkout/{state.processor_entity}/{state.cs_id}"
    )
    confirm_body = ops._upi_build_confirm_body(
        cs_id=state.cs_id,
        stripe_pk=state.stripe_pk,
        ctx=state.ctx,
        processor_entity=state.processor_entity,
        init_payload=state.init,
        billing=state.billing,
        fingerprint=state.fingerprint,
        pm_id=state.pm_id,
        inline_pm=state.inline_pm,
        return_url=state.return_url,
        payment_method_selection_flow=state.payment_method_selection_flow,
        reference_shape=state.approve_shape == "reference",
    )
    confirm_resp, confirm_fingerprint = ops._upi_post_with_degrade(
        state.stripe,
        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=state.cs_id),
        data=confirm_body,
        fingerprint=state.fingerprint,
        stage="stripe_confirm",
    )
    if ops._upi_is_403(confirm_resp) and confirm_fingerprint:
        # 降级重试成功后，后续阶段（approve / 二次 confirm）必须沿用同一套
        # 身份，否则又回到「confirm 用 chrome124、approve 用 chrome136」的
        # 自相矛盾状态。
        state.fingerprint = dict(confirm_fingerprint)
        ops._upi_apply_fingerprint(state.stripe, state.fingerprint)
        state.emit("stripe_confirm", "adopted degraded fingerprint for subsequent stages")
    if confirm_resp.status_code >= 400:
        state.emit("stripe_confirm", f"confirm failed: {confirm_resp.status_code} {confirm_resp.text[:300]}")
        return ops._upi_hosted_fallback_result(
            cs_id=state.cs_id,
            processor_entity=state.processor_entity,
            init=state.init,
            amount=state.amount,
            payment_currency=state.payment_currency,
            target_country=state.target_country,
            checkout_country=state.checkout_country,
            payment_country=state.payment_country,
            pm_types=state.pm_types,
            checkout_proxy=state.checkout_proxy,
            provider_proxy=state.provider_proxy,
            approve_proxy=state.approve_proxy,
            checkout_ui_mode=state.checkout_ui_mode,
            qr_path=state.qr_path,
            warning=f"stripe_confirm_failed: {confirm_resp.status_code}",
        )
    state.confirm_data = confirm_resp.json() or {}
    state.emit("stripe_confirm", "confirm success")
    # SetupIntent 失败判读（旧实现完全没有这一步）
    ops._upi_raise_if_setup_intent_blocked(state.confirm_data, "stripe confirm", current_pm_id=state.pm_id)


    return None


def _stage_approve(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 6: ChatGPT approve ─────────────────────────────────────
    # 门禁严格对齐参考实现的三分支（参见参考 idx_extract 的三段 if/elif）：
    #   1) 已有 redirect              -> 不需要 approve，直接进 Stage 7
    #   2) 无 redirect 且 requires_approval -> 必须 approve
    #   3) 无 redirect 也无 QR        -> 只轮询 payment_pages 最终确认，不发 approve
    #   4) 无 redirect 但有 QR        -> 已拿到可用物，不必再 approve
    # 旧实现用的是「状态是 requires_approval 或 没有 redirect」这种并集，
    # 会把分支 3/4 也拖进 approve，既多发无谓请求也会阻塞在 60 次重试上。
    confirm_redirect = ops._upi_extract_redirect_url(state.confirm_data)
    confirm_qr_urls = ops._upi_extract_qr_candidates(state.confirm_data)
    submission = ops._upi_find_submission_attempt(state.confirm_data)
    submission_state = str(submission.get("state") or "")
    needs_approval = not confirm_redirect and submission_state == "requires_approval"
    needs_final_poll = not confirm_redirect and not confirm_qr_urls and submission_state != "requires_approval"
    if needs_final_poll:
        state.emit(
            "approve",
            "confirm 无 redirect/QR 且非 requires_approval，跳过 approve 直接做最终确认轮询",
        )
    blocked_count = 0
    state.approval_blocked = False
    # 注意：approval_ok / approval_data 必须在这里就绑定。
    # Stage 7 会无条件遍历 (confirm_data, approval_data)，若只在
    # needs_approval 分支里赋值，跳过 approve 时会 UnboundLocalError。
    state.approval_ok = False
    state.approval_data: dict[str, Any] = {}
    if needs_approval:
        state.emit(
            "approve",
            f"Stage 6: ChatGPT approve using {ops.redact_proxy_text(state.approve_proxy or 'DIRECT', state.approve_proxy)}",
        )
        if state.browser_rail and not state.approval_ok:
            state.emit("approve", "Stage 6: headless browser approve (same-session observation)")
            browser_approve = ops._upi_browser_approve(
                access_token=state.access_token,
                session_token=state.session_token,
                device_id=state.device_id,
                proxy=state.approve_proxy,
                fingerprint=state.fingerprint,
                processor_entity=state.processor_entity,
                cs_id=state.cs_id,
            )
            state.emit(
                "approve",
                "browser approve: ok=%s result=%s obs=%s error=%s"
                % (
                    browser_approve["ok"],
                    browser_approve["result"] or "unknown",
                    "yes" if browser_approve["observation"] else "no",
                    str(browser_approve["error"] or "none")[:120],
                ),
            )
            if browser_approve["ok"]:
                state.approval_ok = True
                state.approval_data = {"result": "approved", "via": "headless_browser"}
        approve_session = ops._upi_new_chatgpt_session(state.approve_proxy, state.fingerprint, state.device_id, state.session_token)
        approve_session.headers.update(
            {
                "Authorization": f"Bearer {state.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"https://chatgpt.com/checkout/{state.processor_entity}/{state.cs_id}",
            }
        )
        # The approval endpoint is risk-gated: it needs the same deployment
        # attestation, signed observation and frontend markers as Checkout,
        # plus a fresh checkout_session_approval Sentinel. Attach the full
        # The approval endpoint is risk-gated: re-capture the browser-issued
        # observation on the Checkout page so approve does not replay the
        # create-stage context (which the risk engine rejects).
        if state.browser_rail and not state.approval_ok:
            state.risk = ops._upi_capture_risk_context(
                approve_session,
                access_token=state.access_token,
                device_id=state.device_id,
                page_url=f"https://chatgpt.com/checkout/{state.processor_entity}/{state.cs_id}",
                fingerprint=state.fingerprint,
                proxy=state.approve_proxy,
                session_token=state.session_token,
                use_browser=True,
                sentinel_flow=UPI_SENTINEL_APPROVAL_FLOW,
            )
        ops._upi_apply_approve_risk(
            approve_session,
            state.risk,
            state.access_token,
            state.device_id,
            state.approve_proxy,
            state.fingerprint,
            state.payment_country,
            checkout_sentinel=state.checkout_sentinel_headers,
            approve_shape=state.approve_shape,
        )
        # The reference rail never calls /checkout/confirm (it answers 400
        # "This endpoint is only for internal checkout sessions"); skip it in
        # reference shape so the approve session is not primed by a 4xx.
        if state.approve_shape != "reference":
            # Try confirm endpoint first
            try:
                confirm_chatgpt = approve_session.post(
                    UPI_CHECKOUT_CONFIRM_URL,
                    json={"checkout_session_id": state.cs_id, "selected_payment_method_type": "upi"},
                    timeout=CHATGPT_TIMEOUT,
                )
                confirm_json = confirm_chatgpt.json() or {} if confirm_chatgpt.status_code < 400 else {}
                ops._upi_dump_http(
                    confirm_chatgpt,
                    "chatgpt_approve_confirm",
                    {"checkout_session_id": state.cs_id, "selected_payment_method_type": "upi"},
                    "POST",
                    UPI_CHECKOUT_CONFIRM_URL,
                    force=confirm_chatgpt.status_code >= 400,
                )
                if str(confirm_json.get("result", "")).lower() == "approved":
                    state.emit("approve", "approved via confirm endpoint")
                    state.approval_data = confirm_json
                    state.approval_ok = True
                else:
                    state.approval_data = confirm_json
            except Exception as exc:
                state.emit("approve", f"confirm endpoint error (non-fatal): {type(exc).__name__}: {exc}")

        # If confirm didn't approve, try approve endpoint with retries
        if not state.approval_ok:
            for attempt in range(1, state.max_approve_attempts + 1):
                try:
                    approve_resp = approve_session.post(
                        UPI_CHECKOUT_APPROVE_URL,
                        json={"checkout_session_id": state.cs_id, "processor_entity": state.processor_entity},
                        headers={
                            "x-openai-target-path": "/backend-api/payments/checkout/approve",
                            "x-openai-target-route": "/backend-api/payments/checkout/approve",
                        },
                        timeout=CHATGPT_TIMEOUT,
                    )
                    ops._upi_dump_http(
                        approve_resp,
                        f"chatgpt_approve_{attempt:02d}",
                        {"checkout_session_id": state.cs_id, "processor_entity": state.processor_entity},
                        "POST",
                        UPI_CHECKOUT_APPROVE_URL,
                        force=approve_resp.status_code >= 400,
                    )
                    if approve_resp.status_code < 400:
                        approve_json = approve_resp.json() or {}
                        result = str(approve_json.get("result", "")).lower()
                        if result == "approved":
                            state.emit("approve", f"approved on attempt {attempt}")
                            state.approval_ok = True
                            state.approval_data = approve_json
                            break
                        if result == "blocked":
                            # 参考实现: blocked 时保留当前线路重领 SEN/SO 并轮换
                            # 观察证明重试（文档 5.3）；只换 UA 而沿用同一份
                            # 风控上下文等于原样重放，必然继续 blocked。
                            blocked_count += 1
                            state.emit("approve", f"attempt {attempt}: blocked, rotating risk context")
                            ops._upi_apply_approve_risk(
                                approve_session,
                                state.risk,
                                state.access_token,
                                state.device_id,
                                state.approve_proxy,
                                state.fingerprint,
                                state.payment_country,
                                checkout_sentinel=state.checkout_sentinel_headers,
                                approve_shape=state.approve_shape,
                            )
                            if attempt < state.max_approve_attempts:
                                time.sleep(ops._approve_backoff(attempt, state.approve_backoff_cap))
                            continue
                        state.approval_data = approve_json
                    elif attempt % 10 == 0:
                        state.emit(
                            "approve",
                            f"attempt {attempt}/{state.max_approve_attempts}: status={approve_resp.status_code}",
                        )
                except Exception as ex:
                    if attempt % 10 == 0:
                        state.emit("approve", f"attempt {attempt} exception: {type(ex).__name__}: {ex}")
                if attempt < state.max_approve_attempts:
                    time.sleep(ops._approve_backoff(attempt, state.approve_backoff_cap))

        if not state.approval_ok:
            # 参考实现的判据：全部尝试都 blocked ⇒ 这是 provider 侧风控，
            # 不是「再等等就好」，必须让上层能区分出来。
            if blocked_count and blocked_count == state.max_approve_attempts:
                state.approval_blocked = True
                state.emit(
                    "approve",
                    f"all {state.max_approve_attempts} attempts blocked (provider risk control)",
                )
            else:
                state.emit("approve", "approval failed after all attempts, continuing to extraction")

    return None


def _stage_mandate(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── Stage 6b: 直连 SetupIntent 补交 UPI AutoPay mandate ────────────
    # Payment Page 的 confirm 在当前 Checkout 版本上把
    # ``payment_method_options`` 当未知参数丢弃，所以「已批准」的提交也会停在
    # ``requires_payment_method``，而 ``upi://`` 深链只有直连 SetupIntent 再
    # confirm 一次才会出现。参考实现 ``provider_checkout`` 的
    # ``need_setup_recover`` + ``confirm_local_setup_intent``。
    #
    # 时序很关键：批准之前 ``checkout.session.setup_intent`` 是 **null**
    # （2026-09-30 实测），SetupIntent 只在批准之后才出现。所以除了 approve
    # 之后在这里试一次，还要作为 rescue 回调交给 Stage 7 的轮询再补一次。
    # 成功一次即封口。**失败的尝试刻意允许重试**——不能拿「已尝试」当闸门，
    # 否则 Stage 7 的 rescue 会被上一步的失败永久挡掉。
    state.mandate_ok = False


    if not _absorb_mandate(ops, state, state.confirm_data, state.approval_data):
        state.emit(
            "mandate",
            "Stage 6b: no SetupIntent yet (null until approval), rescue wired into the payment-page poll",
        )

    return None


def _stage_redirect(ops: UpiOperations, state: UpiStageContext) -> Any:
    def _resubmit_with_fresh_pm(_payload: Mapping[str, Any]) -> str:
        """``generic_decline`` 后换一个全新 ``pm_`` 再 confirm 同一个 cs_id。

        Stripe 口径下拒的是**这一次支付方式**，不是这个 Checkout Session：
        ``generic_decline`` 属可重试错误（"换卡几乎总能过"），而且"拒卡不需要
        重建整个 Checkout Session，只需要换卡重建 token"。见 blog.caowo.de
        《Stripe protocol payment automation deep dive 2026》§9.1 / §3.5。

        返回新 ``pm_id``（成功）或空串（换 PM / confirm 失败，调用方按原逻辑
        判终态）。固定走「引用已创建 PM」形态：只有这样才能真正**替换**被拒的
        支付方式 —— inline 形态每次提交的都是同一份 ``payment_method_data``。
        """

        try:
            fresh_pm = ops._upi_create_upi_pm(state.stripe, state.cs_id, state.stripe_pk, state.billing)
        except Exception as exc:
            state.emit("decline_retry", f"fresh payment method failed: {type(exc).__name__}: {exc}")
            return ""
        retry_body = ops._upi_build_confirm_body(
            cs_id=state.cs_id,
            stripe_pk=state.stripe_pk,
            ctx=state.ctx,
            processor_entity=state.processor_entity,
            init_payload=state.init,
            billing=state.billing,
            fingerprint=state.fingerprint,
            pm_id=fresh_pm,
            inline_pm=False,
            return_url=state.return_url,
            payment_method_selection_flow=state.payment_method_selection_flow,
            reference_shape=state.approve_shape == "reference",
        )
        retry_resp = state.stripe.post(
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=state.cs_id),
            data=retry_body,
            timeout=DEFAULT_TIMEOUT,
        )
        ops._upi_dump_http(
            retry_resp,
            "stripe_decline_retry_confirm",
            retry_body,
            "POST",
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=state.cs_id),
            force=retry_resp.status_code >= 400,
        )
        if retry_resp.status_code >= 400:
            state.emit(
                "decline_retry",
                f"fresh pm confirm failed: {retry_resp.status_code} {retry_resp.text[:200]}",
            )
            return ""
        state.pm_id = fresh_pm
        state.confirm_data = retry_resp.json() or {}
        state.emit("decline_retry", f"swapped in {fresh_pm} and re-confirmed the same session")
        return fresh_pm
    # ── Stage 7: Extract redirect / upi:// URI ───────────────────────
    state.emit("poll", "Stage 7: extracting UPI redirect / QR data")
    state.qr_data: dict[str, Any] = {}
    state.redirect_url = ""

    def _absorb(source: Any) -> None:
        """把一份响应里的跳转 / QR / upi:// 全部吸收进累积态。"""

        if not state.redirect_url:
            candidate = ops._upi_extract_redirect_url(source)
            if candidate:
                state.redirect_url = candidate
        for url in ops._upi_extract_qr_candidates(source):
            kind = ops._upi_qr_image_kind(url)
            if kind == "svg":
                state.qr_data.setdefault("qr_image_url_svg", url)
            elif kind in ("png", "jpg"):
                state.qr_data.setdefault("qr_image_url_png", url)
        for k, v in ops._upi_extract_next_action(source).items():
            if v and (k == "upi_uri" or not state.qr_data.get(k)):
                state.qr_data[k] = v

    # First check confirm/approve responses (redirect 优先, QR 补充)
    for state.source in (state.confirm_data, state.approval_data):
        _absorb(state.source)

    def _rescue_mandate(payload: Mapping[str, Any]) -> bool:
        """批准后「SetupIntent 未产出动作」的补交钩子（Stage 7 轮询回调）。

        参考实现 ``need_setup_recover``：先直连补交 mandate，再让轮询继续一轮，
        而不是把同一个 ``generic_decline`` 判成终态拒绝。
        """
        return _absorb_mandate(ops, state, payload, state.confirm_data, state.approval_data)

    # Poll Stripe payment page until a real redirect / QR / upi:// appears.
    #
    # ``_upi_poll_payment_page`` owns its own round/deadline loop (it sleeps
    # between attempts and decides terminal states), so wrapping it in an
    # outer ``for range(poll_max_attempts)`` loop was dead: the old body
    # unconditionally ``break``-ed after the first call, which made
    # ``poll_max_attempts`` a no-op and misattributed the real budget to a
    # parameter nothing read. ``poll_max_attempts`` is now passed *into* the
    # poller, where it bounds the rounds; ``UPI_POLL_TIMEOUT`` remains the
    # wall-clock ceiling for the same loop.
    if not state.redirect_url and not state.qr_data.get("upi_uri"):
        try:
            poll_redirect, poll_qr = ops._upi_poll_payment_page(
                state.stripe,
                state.cs_id,
                state.stripe_pk,
                state.ctx,
                current_pm_id=state.pm_id,
                rescue=_rescue_mandate,
                recover_decline=_resubmit_with_fresh_pm,
                max_attempts=state.poll_max_attempts,
            )
        except Exception as exc:
            if not ops._upi_should_retry_second_confirm(exc):
                raise
            state.emit("poll", f"extraction needs a second confirm: {str(exc)[:160]}")
        else:
            if poll_redirect:
                state.redirect_url = poll_redirect
            for url in poll_qr:
                kind = ops._upi_qr_image_kind(url)
                if kind == "svg":
                    state.qr_data.setdefault("qr_image_url_svg", url)
                elif kind in ("png", "jpg"):
                    state.qr_data.setdefault("qr_image_url_png", url)

    # 补交 mandate 的响应是轮询中途才到的，这里再吸收一次。
    if state.approval_data.get("local_mandate"):
        _absorb(state.approval_data)

    # If still nothing, refresh init and try a second confirm once
    if not state.redirect_url and not state.qr_data.get("upi_uri"):
        state.emit("poll", "re-init + second confirm to resolve UPI data")
        try:
            state.refreshed_init = ops._upi_stripe_init(state.stripe, state.cs_id, state.stripe_pk, state.fingerprint, state.stripe_js_id)
        except Exception as exc:
            state.emit("poll", f"re-init failed (non-fatal): {type(exc).__name__}: {exc}")
            state.refreshed_init = None
        if state.refreshed_init:
            state.init = state.refreshed_init
            state.ctx = ops._upi_rebuild_ctx(state.ctx, state.init, state.fingerprint, state.stripe_js_id)
            _absorb(state.init)
            if not state.redirect_url and not state.qr_data.get("upi_uri"):
                try:
                    second_body = ops._upi_build_confirm_body(
                        cs_id=state.cs_id,
                        stripe_pk=state.stripe_pk,
                        ctx=state.ctx,
                        processor_entity=state.processor_entity,
                        init_payload=state.init,
                        billing=state.billing,
                        fingerprint=state.fingerprint,
                        pm_id=state.pm_id,
                        inline_pm=state.inline_pm,
                        return_url=(
                            ops._normalize_hosted_checkout_url(str(state.init.get("stripe_hosted_url") or ""))
                            or state.return_url
                        ),
                        payment_method_selection_flow=state.payment_method_selection_flow,
                        reference_shape=state.approve_shape == "reference",
                    )
                    second_resp = state.stripe.post(
                        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=state.cs_id),
                        data=second_body,
                        timeout=DEFAULT_TIMEOUT,
                    )
                    ops._upi_dump_http(
                        second_resp,
                        "stripe_second_confirm",
                        second_body,
                        "POST",
                        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=state.cs_id),
                        force=second_resp.status_code >= 400,
                    )
                    if second_resp.status_code < 400:
                        state.confirm_data = second_resp.json() or {}
                        state.emit("poll", "second confirm succeeded, re-extracting")
                        _absorb(state.confirm_data)
                except Exception as exc:
                    state.emit("poll", f"second confirm failed (non-fatal): {type(exc).__name__}: {exc}")

    # Follow the external redirect to the real instructions page
    if state.redirect_url:
        resolved = ops._upi_resolve_external_redirect(state.stripe, state.redirect_url)
        if resolved and resolved != state.redirect_url:
            state.emit("redirect", f"followed redirect to {resolved[:80]}...")
            state.redirect_url = resolved
        if ops._upi_is_instructions_url(state.redirect_url):
            state.qr_data.setdefault("hosted_instructions_url", state.redirect_url)

    # Hydrate: fetch hosted_instructions_url HTML if no upi:// yet
    state.emit("hydrate", "hydrating UPI QR data from hosted instructions")
    state.qr_data = ops._upi_hydrate_qr_data(state.qr_data, state.provider_proxy, state.fingerprint)

    state.upi_uri = str(state.qr_data.get("upi_uri") or "")
    if not state.upi_uri.startswith("upi://"):
        mobile_auth = str(state.qr_data.get("mobile_auth_url") or "")
        state.upi_uri = mobile_auth if mobile_auth.startswith("upi://") else ""
    state.hosted_url = (
        ops._normalize_hosted_checkout_url(str(state.init.get("stripe_hosted_url") or ""))
        or f"https://pay.openai.com/c/pay/{state.cs_id}"
    )
    if not state.redirect_url:
        state.redirect_url = state.hosted_url
    state.expires_at = state.qr_data.get("expires_at") or int(time.time()) + 300

    if state.upi_uri:
        state.emit("done", f"UPI URI extracted: {state.upi_uri[:40]}...")
        state.qr_data_str = state.upi_uri
        state.link_type = "upi_deep_link"
    elif ops._upi_is_instructions_url(state.redirect_url):
        state.emit("done", "hosted UPI instructions page resolved")
        state.qr_data_str = state.redirect_url
        state.link_type = "upi_instructions_url"
    else:
        state.emit("done", "no upi:// URI found, falling back to hosted URL")
        state.qr_data_str = state.redirect_url or state.hosted_url
        state.link_type = "upi_hosted_fallback"

    state.written_qr_path = ops._write_qr_png(state.qr_data_str, state.qr_path or "")
    state.paid_state = {"paid": False, "payment_status": "not_requested"}
    if state.wait_paid:
        state.emit("paid", f"waiting up to {state.paid_timeout:g}s for payment on {state.cs_id[:20]}...")
        state.paid_state = ops._upi_wait_paid(state.stripe, cs_id=state.cs_id, stripe_pk=state.stripe_pk, ctx=state.ctx, timeout=state.paid_timeout)
        state.emit("paid", f"payment_status={state.paid_state.get('payment_status')} paid={state.paid_state.get('paid')}")

    return None


def _stage_verify(ops: UpiOperations, state: UpiStageContext) -> Any:
    # ── 交付前核验（step 8，移植自 upi-zero-link/verify.py）───────────
    # ``hosted_instructions_url`` 在 setup_intent **被拒**时也会返回，所以
    # 「有 URL」不等于「签出了 ₹0 委托」。必须查 intent_state + fam，否则
    # 交出去的是用户打开就看到 ₹1999 付款页的废链。
    # 读不到页面属于「没结论」（inconclusive），不是「链是假的」。
    verify_target = str(state.qr_data.get("hosted_instructions_url") or "")
    if not verify_target and ops._upi_is_instructions_url(state.redirect_url):
        verify_target = state.redirect_url
    link_verified = False
    verification_inconclusive = False
    verification_code = ""
    if verify_target:
        verified, label, verification_code = ops._upi_verify_instructions_verdict(
            verify_target, proxy=state.provider_proxy
        )
        link_verified = bool(verified)
        verification_inconclusive = (not verified) and verification_code in UPI_VERIFY_INCONCLUSIVE
        verification = label if (verified or not verification_inconclusive) else f"inconclusive:{label}"
        state.emit("verify", f"UPI link {'verified' if verified else 'NOT verified'} ({verification})")
    else:
        verification = "no_instructions_url"

    link_result: dict[str, Any] = {
        "ok": True,
        "payment_method": "upi",
        "method": "upi",
        "link_type": state.link_type,
        "url": state.upi_uri or state.redirect_url or state.hosted_url,
        "upi_uri": state.upi_uri,
        "hosted_url": state.hosted_url,
        "instructions_url": state.redirect_url if ops._upi_is_instructions_url(state.redirect_url) else "",
        "qr_data": state.qr_data_str,
        "qr_path": state.written_qr_path,
        "qr_image_url_png": state.qr_data.get("qr_image_url_png", ""),
        "qr_image_url_svg": state.qr_data.get("qr_image_url_svg", ""),
        "expires_at": state.expires_at,
        "cs_id": state.cs_id,
        "processor_entity": state.processor_entity,
        "amount": state.amount,
        "currency": state.payment_currency.upper(),
        "target_country": state.target_country,
        "checkout_country": state.checkout_country,
        "billing_country": state.checkout_country,
        "payment_country": state.payment_country,
        "payment_method_types": state.pm_types,
        "coupon_name": state.ft_status["coupon_name"],
        "approval_ok": state.approval_ok,
        "approval_blocked": state.approval_blocked,
        "paid": bool(state.paid_state.get("paid")),
        "payment_status": str(state.paid_state.get("payment_status") or ""),
        "checkout_ui_mode": state.checkout_ui_mode,
        "checkout_proxy": state.checkout_proxy,
        "provider_proxy": state.provider_proxy,
        "approve_proxy": state.approve_proxy,
        "verification": verification,
        "verification_code": verification_code,
        "link_verified": link_verified,
    }
    if verify_target and not link_verified and not verification_inconclusive:
        # 明确判定为不可交付 —— 但原因分三种，不能一律叫「废链」；
        # 映射与理由归 ``_upi_link_unverified_contract``。
        link_result["ok"] = False
        link_result.update(ops._upi_link_unverified_contract(verification_code, verification))
    return link_result
    return None



_STAGES: tuple[Any, ...] = (
    _stage_checkout,
    _stage_stripe_init,
    _stage_free_trial,
    _stage_cpmt,
    _stage_oaics,
    _stage_tax_customer,
    _stage_repeat_tax,
    _stage_confirm,
    _stage_approve,
    _stage_mandate,
    _stage_redirect,
    _stage_verify,
)

def run_upi_qr_link_once(
    ops: UpiOperations,
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    approve_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    payment_country: str | None = None,
    require_zero: bool | None = None,
    qr_path: str | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    proxy_state: Any = None,
    device_id: str | None = None,
    session_token: str | None = None,
    wait_paid: bool = False,
    paid_timeout: float = 900.0,
    require_server_upi_mandate: bool = False,
) -> dict[str, Any]:
    """Generate a UPI payment link with full Stripe Confirm + Approve flow.

    ``proxy_state`` 是可选的 ``PayPalProxyState``（或任何实现
    ``record_zero_result(proxy, country, amount)`` 的对象）。传入后，本轮
    checkout 的实付金额会被记进 0 元缓存，供后续批次做代理调度。
    **不传即完全关闭**，既有调用方行为不变。

    Implements the complete UPI extraction pipeline over the **custom checkout**
    protocol (2026-09-17 rewrite; was ``hosted``):

      1. ChatGPT checkout (create cs_id) -- ``checkout_ui_mode=custom``
      2. Stripe init (build ctx: guid/muid/sid, elements session, init_checksum)
      3. Free trial detection (coupon / discount / amount analysis)
      4. Tax region update (IN billing) + customer_data sync
      5. Stripe confirm (upi PM inline or by reference) with expected_amount +
         last_displayed_line_item_group_details
      6. ChatGPT approve (handle ``blocked`` by refreshing client ids)
      7. Poll payment_pages / setup_intent -> extract redirect / upi:// URI
         -> hydrate hosted instructions -> follow external redirect -> render QR

    Returns ``upi://`` deep link + QR PNG path on success, or the Stripe
    hosted instructions URL / hosted fallback if UPI data is not available.
    """
    state = UpiStageContext()
    state.access_token = access_token
    state.proxy = proxy
    state.checkout_proxy = checkout_proxy
    state.provider_proxy = provider_proxy
    state.approve_proxy = approve_proxy
    state.target_country = target_country
    state.checkout_country = checkout_country
    state.payment_country = payment_country
    state.require_zero = require_zero
    state.qr_path = qr_path
    state.runtime_config = runtime_config
    state.proxy_state = proxy_state
    state.device_id = device_id
    state.session_token = session_token
    state.wait_paid = wait_paid
    state.paid_timeout = paid_timeout
    state.require_server_upi_mandate = require_server_upi_mandate
    state._rc = ops._resolve_upi_runtime(
        access_token=state.access_token,
        proxy=state.proxy,
        checkout_proxy=state.checkout_proxy,
        provider_proxy=state.provider_proxy,
        approve_proxy=state.approve_proxy,
        target_country=state.target_country,
        checkout_country=state.checkout_country,
        payment_country=state.payment_country,
        require_zero=state.require_zero,
        runtime_config=state.runtime_config,
        device_id=state.device_id,
        session_token=state.session_token,
    )
    cfg = state._rc.cfg
    state.upi_cfg = state._rc.upi_cfg
    stage_proxies = state._rc.stage_proxies
    state.checkout_proxy = str(state._rc.checkout_proxy or "")
    state.provider_proxy = str(state._rc.provider_proxy or "")
    state.approve_proxy = str(state._rc.approve_proxy or "")
    regions = state._rc.regions
    state.checkout_country = str(state._rc.checkout_country or "")
    state.payment_country = str(state._rc.payment_country or "")
    state.target_country = str(state._rc.target_country or "")
    state.currency = state._rc.currency
    state.payment_currency = state._rc.payment_currency
    state.require_zero = state._rc.require_zero
    state.checkout_ui_mode = state._rc.checkout_ui_mode
    state.inline_pm = state._rc.inline_pm
    state.approve_shape = state._rc.approve_shape
    state.payment_method_selection_flow = state._rc.payment_method_selection_flow
    state.update_tax_region = state._rc.update_tax_region
    state.update_customer_data = state._rc.update_customer_data
    state.max_approve_attempts = state._rc.max_approve_attempts
    state.poll_max_attempts = state._rc.poll_max_attempts
    state.approve_backoff_cap = state._rc.approve_backoff_cap
    state.local_mandate_enabled = state._rc.local_mandate_enabled
    state.browser_rail = state._rc.browser_rail
    fingerprint_index = state._rc.fingerprint_index
    state.fingerprint = state._rc.fingerprint
    state.billing = state._rc.billing
    state.device_id = str(state._rc.device_id or "")
    state.session_token = str(state._rc.session_token or "")

    state.emit = ops._emit
    _out = _stage_pre_exit(ops, state)
    if _out is not None:
        return _out
    try:
        for _stage in _STAGES:
            _out = _stage(ops, state)
            if _out is not None:
                return _out
    except Exception as e:
        # 把失败原因压成可判读的 error_code。历史实现一律返回
        # "upi_qr_failed"，调用方只能读 error 字符串做子串匹配——
        # 而 error_classification 又不认识 generic_decline / approve blocked
        # 这些 UPI 专有说法（实测都落到 "unknown"）。
        # 这里就地给出稳定代码，让上层不必解析自然语言。
        return {
            "ok": False,
            "error": str(e),
            "error_code": ops._upi_classify_failure(e),
            "payment_method": "upi",
            "url": "",
            "qr_path": "",
        }
    return {}


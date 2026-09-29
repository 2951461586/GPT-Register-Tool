from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..endpoints import CHATGPT_ORIGIN
except ImportError:
    from endpoints import CHATGPT_ORIGIN  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:
    from pp_link_helpers import DEFAULT_TIMEOUT, STRIPE_VERSION  # type: ignore
from typing import Any
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
import json
import secrets
import time
import uuid
from ._extract import (
    _upi_confirm_amounts,
    _upi_display_amounts,
    _upi_extract_next_action,
    _upi_extract_payment_amount,
    _upi_extract_qr_candidates,
    _upi_extract_redirect_url,
    _upi_find_submission_attempt,
    _upi_first_value_by_key,
    _upi_int_value,
    _upi_is_provider_decline_text,
    _upi_provider_decline_message,
    _upi_raise_if_setup_intent_blocked,
    _upi_setup_intent_last_error,
)
from .constants import (
    STRIPE_INTENT_URL_T,
    STRIPE_PAYMENT_METHODS_URL,
    STRIPE_PAYMENT_PAGE_GET_URL_T,
    STRIPE_PAYMENT_PAGE_INIT_URL_T,
    UPI_FINGERPRINT_TEMPLATES,
    UPI_LOCAL_MANDATE_DEFAULT_AMOUNT,
    UPI_LOCAL_MANDATE_DESCRIPTION,
    UPI_LOCAL_MANDATE_END_DAYS,
    UPI_LOCAL_MANDATE_FATAL_MARKERS,
    UPI_LOCAL_MANDATE_STRIPE_VERSION,
    UPI_REFERENCE_STRIPE_RUNTIME_VERSION,
    UPI_REFERENCE_STRIPE_VERSION,
    UPI_SECOND_CONFIRM_MARKERS,
)
from .env import _emit, _env_bool, _env_int, _env_str
from .dump import _upi_dump_http
from .session import _normalize_hosted_checkout_url, _upi_apply_fingerprint, _upi_runtime_version, _write_qr_png
from .browser import _upi_browser_id


def _upi_elements_session_params(ctx: Mapping[str, Any]) -> dict[str, str]:
    """``elements_session_client`` 公共参数（init / confirm / poll 三处复用）。"""
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(
            ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"
        ),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


def _upi_build_init_body(
    stripe_pk: str,
    fingerprint: Mapping[str, str],
    stripe_js_id: str,
) -> dict[str, str]:
    """custom 模式 Stripe init 载荷。"""
    return {
        "browser_locale": str(fingerprint.get("locale") or "en-IN"),
        "browser_timezone": str(fingerprint.get("timezone") or "Asia/Kolkata"),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str(fingerprint.get("elements_locale") or "en"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }


def _upi_passive_captcha_config(init_payload: Any) -> dict[str, str]:
    """提取 Stripe 的 passive hCaptcha ``site_key`` / ``rqdata``。

    Stripe 的 ``payment_pages/init`` 顶层带 ``site_key`` + ``rqdata``（被动
    hCaptcha 配置）；参考实现从 ``elements.passive_captcha`` 读同一组值。
    两处都接受，缺失时返回空 dict（调用方按可选字段降级）。
    """
    if not isinstance(init_payload, Mapping):
        return {}
    site_key = str(init_payload.get("site_key") or "").strip()
    rqdata = str(init_payload.get("rqdata") or "").strip()
    nested = init_payload.get("passive_captcha")
    if isinstance(nested, Mapping):
        site_key = str(nested.get("site_key") or site_key).strip()
        rqdata = str(nested.get("rqdata") or rqdata).strip()
    if not site_key:
        return {}
    return {"site_key": site_key, "rqdata": rqdata}


def _upi_passive_captcha_fields(
    init_payload: Any,
    *,
    proxy: str = "",
    locale: str = "en-US",
    timeout_ms: int = 120000,
) -> dict[str, str] | None:
    """求解 Stripe 被动 hCaptcha，返回 confirm 需要的字段。

    参考实现实测（HAR #473/#500/#609）：confirm 缺少 ``passive_captcha_token``
    时 Stripe **不会**在前置校验报错，而是在支付设置阶段返回
    ``setup_attempt_failed`` / ``generic_decline``。token 无法离线伪造，
    必须跑完下发的挑战；本仓已有 Playwright 求解器
    （``captcha_solver._solve_hcaptcha``），直接复用而不另植一份。

    与 attestation/sentinel 同样的容错语义：求解失败/无 site_key 按可选字段
    降级返回 ``{}``（confirm 照常进行，只是缺 token）；显式关闭时返回
    ``None``，调用方据此**完全不新增字段**，保持旧行为。
    """
    if _env_bool("UPI_HCAPTCHA_DISABLE", False) or not _env_bool("UPI_PASSIVE_CAPTCHA", True):
        return None
    cfg = _upi_passive_captcha_config(init_payload)
    if not cfg.get("site_key"):
        _emit("captcha", "init 无 passive site_key，confirm 将缺 passive_captcha_token")
        return {}
    try:
        from ..captcha_solver import _solve_hcaptcha
    except Exception as exc:
        _emit("captcha", f"求解器不可用（非致命）: {type(exc).__name__}")
        return {}
    _emit("captcha", "开始求解 Stripe 被动 hCaptcha...")
    try:
        token, ekey = _solve_hcaptcha(
            site_key=cfg["site_key"],
            rqdata=cfg.get("rqdata", ""),
            proxy=proxy,
            headless=True,
            timeout_ms=timeout_ms,
            locale=locale,
        )
    except Exception as exc:
        _emit("captcha", f"被动 hCaptcha 失败（非致命）: {type(exc).__name__}: {str(exc)[:120]}")
        return {}
    if not token:
        _emit("captcha", "被动 hCaptcha 未返回 token")
        return {}
    out = {"passive_captcha_token": str(token)}
    if ekey:
        out["passive_captcha_ekey"] = str(ekey)
    _emit("captcha", f"被动 hCaptcha token 就绪（len={len(str(token))}, ekey={'有' if ekey else '无'}）")
    return out


def _upi_build_confirm_body(
    *,
    cs_id: str,
    stripe_pk: str,
    ctx: Mapping[str, Any],
    processor_entity: str,
    init_payload: Mapping[str, Any],
    billing: Mapping[str, str],
    fingerprint: Mapping[str, str],
    pm_id: str = "",
    inline_pm: bool = True,
    return_url: str = "",
    payment_method_selection_flow: str = "automatic",
    reference_shape: bool = False,
) -> dict[str, str]:
    """构造 custom 模式 confirm 载荷。

    相比旧实现补齐的字段（全部是 custom 模式专属）:

    * ``expected_amount`` / ``expected_amount_on_bca`` —— 金额一致性校验,
      缺失会被 Stripe 判为客户端伪造。
    * ``last_displayed_line_item_group_details[*]`` —— 屏幕上显示的行项目金额。
    * ``consent[terms_of_service] = accepted``
    * ``link_brand``
    * ``guid`` / ``muid`` / ``sid`` / ``version``（Stripe.js 身份三元组）
    * ``client_attribution_metadata[*]`` 全套
    * ``init_checksum`` —— 旧实现读了 ``init`` 局部变量, 但 ``init`` 只在税区
      更新成功时才被重新赋值, 该路径下才存在。

    ``inline_pm=True`` 走 ``payment_method_data[*]`` 内联（``UPI_CONFIRM_INLINE_PM``
    默认口径）, 否则引用已创建的 ``pm_id``。
    """
    expected_amount, expected_amount_on_bca = _upi_confirm_amounts(init_payload, ctx.get("checkout_amount"))
    displayed = _upi_display_amounts(init_payload)
    if reference_shape:
        # Exact field set of the reference upi-zero-link confirm body: a
        # separately-created PM referenced by id, the custom_checkout beta API
        # version, and none of the extra custom-mode fields (consent /
        # last_displayed_line_item_group_details / guid-muid-sid / passive
        # captcha / elements session params) this project historically added.
        body: dict[str, str] = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": str(expected_amount),
            "tax_id_collection[purchasing_as_business]": "false",
            "expected_payment_method_type": "upi",
            "return_url": return_url or f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            "key": stripe_pk,
            "_stripe_version": UPI_REFERENCE_STRIPE_VERSION,
            "version": UPI_REFERENCE_STRIPE_RUNTIME_VERSION,
            "client_attribution_metadata[client_session_id]": str(
                ctx.get("client_session_id") or ctx.get("stripe_js_id") or ""
            ),
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or uuid.uuid4()),
            "link_brand": "link",
        }
        init_checksum = str(init_payload.get("init_checksum") or ctx.get("init_checksum") or "")
        if init_checksum:
            body["init_checksum"] = init_checksum
        return body
    body: dict[str, str] = {
        "eid": "NA",
        "expected_amount": expected_amount,
        "expected_payment_method_type": "upi",
        "return_url": return_url or f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
        "_stripe_version": STRIPE_VERSION,
        "guid": str(ctx.get("guid") or _upi_browser_id()),
        "muid": str(ctx.get("muid") or _upi_browser_id()),
        "sid": str(ctx.get("sid") or _upi_browser_id()),
        "key": stripe_pk,
        "version": str(ctx.get("runtime_version") or _upi_runtime_version()),
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(
            ctx.get("client_session_id") or ctx.get("stripe_js_id") or ""
        ),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": str(payment_method_selection_flow),
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(ctx.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "last_displayed_line_item_group_details[subtotal]": displayed["subtotal"],
        "last_displayed_line_item_group_details[total_exclusive_tax]": displayed["total_exclusive_tax"],
        "last_displayed_line_item_group_details[total_inclusive_tax]": displayed["total_inclusive_tax"],
        "last_displayed_line_item_group_details[total_discount_amount]": displayed["total_discount_amount"],
        "last_displayed_line_item_group_details[shipping_rate_amount]": displayed["shipping_rate_amount"],
        "link_brand": "link",
    }
    if expected_amount_on_bca:
        body["expected_amount_on_bca"] = expected_amount_on_bca
    if ctx.get("config_id"):
        body["client_attribution_metadata[checkout_config_id]"] = str(ctx["config_id"])
    body.update(_upi_elements_session_params(ctx))

    if inline_pm:
        body.update(
            {
                "payment_method_data[type]": "upi",
                "payment_method_data[allow_redisplay]": "limited",
                "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
                "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
                "payment_method_data[billing_details][address][country]": str(billing.get("country") or "IN"),
                "payment_method_data[billing_details][address][line1]": str(billing.get("line1") or ""),
                "payment_method_data[billing_details][address][city]": str(billing.get("city") or ""),
                "payment_method_data[billing_details][address][postal_code]": str(billing.get("postal_code") or ""),
                "payment_method_data[payment_user_agent]": (
                    f"stripe.js/{_upi_runtime_version()}; stripe-js-v3/{_upi_runtime_version()}; "
                    "payment-element; deferred-intent"
                ),
                "payment_method_data[referrer]": "https://chatgpt.com",
                "payment_method_data[time_on_page]": str(secrets.randbelow(37000) + 18000),
                "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
                "payment_method_data[client_attribution_metadata][client_session_id]": str(
                    ctx.get("stripe_js_id") or ""
                ),
                "payment_method_data[client_attribution_metadata][elements_session_id]": str(
                    ctx.get("elements_session_id") or ""
                ),
                "payment_method_data[client_attribution_metadata][elements_session_config_id]": str(
                    ctx.get("elements_session_config_id") or ""
                ),
                "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
                "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
                "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
                "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
                "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": str(
                    payment_method_selection_flow
                ),
            }
        )
        if billing.get("state"):
            body["payment_method_data[billing_details][address][state]"] = str(billing["state"])
        if billing.get("line2"):
            body["payment_method_data[billing_details][address][line2]"] = str(billing["line2"])
    else:
        body["payment_method"] = pm_id

    passive = ctx.get("passive_captcha")
    if isinstance(passive, Mapping):
        if passive.get("passive_captcha_token"):
            body["passive_captcha_token"] = str(passive["passive_captcha_token"])
        # 参考实现无条件带该键（即使为空），Stripe 也接受空值。
        body["passive_captcha_ekey"] = str(passive.get("passive_captcha_ekey") or "")

    # ``browser_locale`` / ``browser_timezone`` are valid only on the init and
    # tax/customer update requests; Stripe's confirm rejects them with
    # ``parameter_unknown`` (measured live 2026-09-26). The reference confirm
    # body omits them, so they must not leak in through the shared params.
    return body


def _upi_build_ctx(init_payload: Any, fingerprint: Mapping[str, str], stripe_js_id: str) -> dict[str, Any]:
    """参考实现 ``build_ctx``: 汇总 confirm 需要的客户端身份与金额上下文。"""
    payload = init_payload if isinstance(init_payload, dict) else {}
    return {
        "stripe_js_id": str(payload.get("client_stripe_js_id") or stripe_js_id or uuid.uuid4()),
        "client_session_id": str(payload.get("client_stripe_js_id") or stripe_js_id or uuid.uuid4()),
        "guid": _upi_browser_id(),
        "muid": _upi_browser_id(),
        "sid": _upi_browser_id(),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(payload.get("config_id") or uuid.uuid4()),
        "config_id": str(payload.get("config_id") or ""),
        "init_checksum": str(payload.get("init_checksum") or ""),
        "checkout_amount": _upi_extract_payment_amount(payload),
        "locale": str(fingerprint.get("elements_locale") or "en"),
        "currency": str(payload.get("currency") or "").lower(),
        "runtime_version": _upi_runtime_version(),
        "stripe_version": STRIPE_VERSION,
    }


def _upi_degraded_template() -> dict[str, str]:
    """降级指纹模板：chrome124 + macOS UA。

    参考实现把它用作 **VN 优惠阶段的常态身份**
    （``UPI_PROMOTION_IMPERSONATE=chrome124 # VN 优惠阶段避免 chrome136 403``）——
    也就是说，chrome136 会在该阶段被上游 WAF 判 403，而 chrome124 不会。

    本项目没有独立的 promote 阶段，所以把它改为**按需降级**：一旦观察到 403，
    就换到这套身份重试一次。降级模板按名字查找而不是硬取下标 ``[1]``——
    模板顺序调整时取下标会静默换错身份。
    """
    override = _env_str("UPI_DEGRADED_FINGERPRINT", "").strip()
    wanted = override or "chrome-mac"
    for template in UPI_FINGERPRINT_TEMPLATES:
        if template.get("name") == wanted:
            return dict(template)
    return dict(UPI_FINGERPRINT_TEMPLATES[0])


def _upi_is_403(response: Any) -> bool:
    """判定响应是否为 403（含 ``cf-mitigated`` 这类 WAF 标记）。"""
    if response is None:
        return False
    if getattr(response, "status_code", None) == 403:
        return True
    headers = getattr(response, "headers", None) or {}
    try:
        mitigated = str(headers.get("cf-mitigated") or "").strip().lower()
    except Exception:
        mitigated = ""
    return mitigated == "challenge"


def _upi_post_with_degrade(
    session: Any,
    url: str,
    *,
    data: Any = None,
    json_body: Any = None,
    fingerprint: Mapping[str, str] | None = None,
    stage: str = "stripe",
    timeout: float | None = None,
) -> tuple[Any, Mapping[str, str]]:
    """POST 一次；遇 403 就换降级指纹**重试一次**，返回 ``(响应, 最终指纹)``。

    为什么只重试一次：403 是身份被拒，换身份有明确因果；但如果新身份也被拒，
    继续换就是烧配额。上限 1 次既能覆盖「chrome136 → chrome124」这条已知的
    降级路径，又不会把失败放大成无限循环。

    重试前会**注销 session 上旧的指纹 header 再写新的**——``headers.update``
    只覆盖同名键，UA 变了而 ``sec-ch-ua-platform`` 没跟上就会造出更矛盾的指纹。
    """
    kwargs: dict[str, Any] = {"timeout": timeout if timeout is not None else DEFAULT_TIMEOUT}
    if data is not None:
        kwargs["data"] = data
    if json_body is not None:
        kwargs["json"] = json_body

    current = dict(fingerprint or {})
    resp = session.post(url, **kwargs)
    _upi_dump_http(resp, stage, data if data is not None else json_body, "POST", url, force=resp.status_code >= 400)
    if not _upi_is_403(resp):
        return resp, current

    degraded = _upi_degraded_template()
    if degraded.get("name") == current.get("name"):
        # 已经在用降级身份，再换就是同一套，没有意义
        _emit(stage, "403 with degraded fingerprint, not retrying (already degraded)")
        return resp, current

    _emit(stage, f"403 detected, retrying once with degraded fingerprint {degraded.get('name')}")
    _upi_apply_fingerprint(session, degraded)
    resp_retry = session.post(url, **kwargs)
    _upi_dump_http(
        resp_retry,
        f"{stage}_degraded",
        data if data is not None else json_body,
        "POST",
        url,
        force=resp_retry.status_code >= 400,
    )
    return resp_retry, degraded


def _upi_stripe_init(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    fingerprint: Mapping[str, str],
    stripe_js_id: str,
) -> dict[str, Any]:
    """Stripe init（custom 模式）。返回 payload 并附带客户端上下文。

    403 时走一次指纹降级重试（``_upi_post_with_degrade``）；拿到的身份写回
    ``client_fingerprint_used``，让调用方后续阶段沿用同一套身份。
    """
    body = _upi_build_init_body(stripe_pk, fingerprint, stripe_js_id)
    init_url = STRIPE_PAYMENT_PAGE_INIT_URL_T.format(cs_id=cs_id)
    resp, used = _upi_post_with_degrade(
        stripe,
        init_url,
        data=body,
        fingerprint=fingerprint,
        stage="stripe_init",
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"stripe init failed: {resp.status_code} {str(getattr(resp, 'text', ''))[:300]}")
    payload = resp.json() or {}
    if not isinstance(payload, dict):
        payload = {}
    payload["client_stripe_js_id"] = stripe_js_id
    payload["client_fingerprint_used"] = dict(used)
    return payload


def _upi_create_upi_pm(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    billing: Mapping[str, str],
) -> str:
    """参考实现 ``stripe_create_upi_pm``: 先建 PM, 再让 confirm 引用。"""
    body: dict[str, str] = {
        "billing_details[name]": str(billing.get("name") or ""),
        "billing_details[email]": str(billing.get("email") or ""),
        "billing_details[address][country]": str(billing.get("country") or "IN"),
        "billing_details[address][line1]": str(billing.get("line1") or ""),
        "billing_details[address][city]": str(billing.get("city") or ""),
        "billing_details[address][postal_code]": str(billing.get("postal_code") or ""),
        "type": "upi",
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "key": stripe_pk,
    }
    if billing.get("state"):
        body["billing_details[address][state]"] = str(billing["state"])
    if billing.get("line2"):
        body["billing_details[address][line2]"] = str(billing["line2"])
    resp = stripe.post(STRIPE_PAYMENT_METHODS_URL, data=body, timeout=DEFAULT_TIMEOUT)
    _upi_dump_http(
        resp,
        "stripe_create_pm",
        body,
        "POST",
        STRIPE_PAYMENT_METHODS_URL,
        force=resp.status_code >= 400,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"create UPI payment method failed: {resp.status_code} {str(getattr(resp, 'text', ''))[:400]}"
        )
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"create UPI payment method returned bad payload: {str(getattr(resp, 'text', ''))[:300]}")
    return pm_id


_UPI_LOCAL_MANDATE_VERSIONS: tuple[str, ...] = (
    UPI_LOCAL_MANDATE_STRIPE_VERSION,
    f"{UPI_LOCAL_MANDATE_STRIPE_VERSION}; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
    STRIPE_VERSION,
)


def _upi_local_mandate_amount(amount: Any, provider: str = "upi") -> int:
    """mandate 上限金额：checkout 金额可用时用实额，否则用 provider 缺省值。

    参考实现 ``retry_approved_local_mandate``: ``max(1, int(str(amount)))``，
    结果 ``<= 1`` 时回落到 ``9990``（pix）/ ``199900``（upi）。
    """
    value, _ = _upi_int_value(amount)
    if value > 1:
        return value
    return UPI_LOCAL_MANDATE_DEFAULT_AMOUNT if provider == "upi" else 9990


def _upi_local_mandate_variants(provider: str, amount: int) -> list[tuple[str, dict[str, Any]]]:
    """mandate_options 变体阶梯（参考实现顺序，最宽的先试）。

    1 年 ``maximum`` 上限 → 1 年 ``fixed`` → ``maximum`` 去掉 ``end_date`` →
    ``upi_pm_only``（不带 ``payment_method_options``）。最后一项仍会 confirm
    SetupIntent，只是不声明 mandate，用于把卡在 ``requires_payment_method``
    的提交推下去。
    """
    if provider != "upi":
        return [("pm_only", {})]
    now_unix, _ = _upi_int_value(time.time())
    end_unix = now_unix + UPI_LOCAL_MANDATE_END_DAYS * 24 * 60 * 60
    mandate = {
        "amount": amount,
        "amount_type": "maximum",
        "description": UPI_LOCAL_MANDATE_DESCRIPTION,
        "end_date": end_unix,
    }
    return [
        ("upi_max_1y", {"mandate_options": dict(mandate)}),
        ("upi_fixed_1y", {"mandate_options": {**mandate, "amount_type": "fixed"}}),
        ("upi_max_no_end", {"mandate_options": {k: v for k, v in mandate.items() if k != "end_date"}}),
        ("upi_pm_only", {}),
    ]


def _upi_flatten_stripe_params(value: Any, prefix: str = "") -> dict[str, str]:
    """把嵌套 Stripe 参数展平成 ``a[b][c]`` 表单键（参考实现 ``flatten_stripe_params``）。"""
    out: dict[str, str] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            out.update(_upi_flatten_stripe_params(item, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            out.update(_upi_flatten_stripe_params(item, f"{prefix}[{index}]"))
    elif value is not None and prefix:
        if isinstance(value, bool):
            out[prefix] = "true" if value else "false"
        else:
            out[prefix] = str(value)
    return out


def _upi_confirm_local_mandate(
    stripe: Any,
    *,
    setup_intent: Any,
    pm_id: str,
    return_url: str,
    stripe_pk: str,
    amount: Any = 0,
    provider: str = "upi",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """直连 SetupIntent 补交本地授权，返回 mandate 响应里的 ``upi://`` 深链。

    参考实现 ``provider_checkout.retry_approved_local_mandate``：Payment Page
    的 confirm 在当前 Checkout 版本上把 ``payment_method_options`` 当作未知
    参数丢弃，所以 **商户批准之后** 还必须直接对 SetupIntent 再 confirm 一次，
    UPI AutoPay 的 mandate 才会落到响应里。缺了这一步，SetupIntent 会一直停在
    ``requires_payment_method``，链路只能退回 hosted instructions 页。

    返回 ``{"ok", "skipped", "variants", "payload", "upi_uri", "redirect_url",
    "error"}``；前置条件（``seti_`` + ``client_secret`` + ``pm_``）不满足时
    ``skipped=True``，由调用方决定是否忽略。
    """
    intent = setup_intent if isinstance(setup_intent, Mapping) else {}
    setup_id = str(intent.get("id") or "").strip()
    client_secret = str(intent.get("client_secret") or "").strip()
    intent_pm = intent.get("payment_method")
    if isinstance(intent_pm, Mapping):
        intent_pm = intent_pm.get("id")
    pm = str(pm_id or intent_pm or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "fatal": False,
        "variants": [],
        "payload": {},
        "upi_uri": "",
        "redirect_url": "",
        "error": "",
    }
    if not setup_id.startswith("seti_") or not client_secret or not pm.startswith("pm_"):
        result["skipped"] = True
        result["error"] = (
            f"local mandate conditions unmet: setup_id={setup_id[:5] or '-'} "
            f"client_secret={'yes' if client_secret else 'no'} pm={'yes' if pm.startswith('pm_') else 'no'}"
        )
        return result
    mandate_amount = _upi_local_mandate_amount(amount, provider)
    # ``STRIPE_INTENT_URL_T`` already owns the ``api.stripe.com`` host, so the
    # confirm path is appended rather than inlined as a second literal.
    url = STRIPE_INTENT_URL_T.format(intent_path="setup_intents", intent_id=setup_id) + "/confirm"
    for name, options in _upi_local_mandate_variants(provider, mandate_amount):
        body: dict[str, str] = {
            "client_secret": client_secret,
            "payment_method": pm,
            "return_url": return_url or CHATGPT_ORIGIN,
            "use_stripe_sdk": "true",
            "mandate_data[customer_acceptance][type]": "online",
            "mandate_data[customer_acceptance][online][infer_from_client]": "true",
            "key": stripe_pk,
        }
        if options:
            body.update(_upi_flatten_stripe_params(options, f"payment_method_options[{provider}]"))
        for api_version in _UPI_LOCAL_MANDATE_VERSIONS:
            entry: dict[str, Any] = {"variant": name, "api_version": api_version, "status": 0}
            result["variants"].append(entry)
            try:
                resp = stripe.post(url, data=body, headers={"Stripe-Version": api_version}, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - 传输异常按变体失败继续降级
                entry["status"] = -1
                result["error"] = f"{type(exc).__name__}: {exc}"
                continue
            status, _ = _upi_int_value(getattr(resp, "status_code", 0))
            entry["status"] = status
            text = str(getattr(resp, "text", "") or "")
            _upi_dump_http(resp, f"stripe_local_mandate_{name}", body, "POST", url, force=status >= 400)
            if status != 200:
                result["error"] = f"HTTP {status}: {text[:360]}"
                lowered = text.lower()
                if any(marker in lowered for marker in UPI_LOCAL_MANDATE_FATAL_MARKERS):
                    # 结构性拒绝（Checkout 创建的 SetupIntent 不允许直连 confirm）：
                    # 换变体或换 API 版本都是同一句话，继续试只是白烧请求。
                    result["fatal"] = True
                    return result
                continue
            payload = resp.json() or {}
            entry["intent_status"] = str(payload.get("status") or "")
            result["payload"] = payload
            next_action = _upi_extract_next_action(payload)
            upi_uri = str(next_action.get("upi_uri") or "")
            redirect = _upi_extract_redirect_url(payload)
            if upi_uri.startswith("upi://"):
                result.update({"ok": True, "upi_uri": upi_uri, "redirect_url": redirect, "error": ""})
                return result
            if redirect:
                result["redirect_url"] = redirect
            result["error"] = f"no upi:// in {name} response"
    return result


def _upi_payment_page_summary(payload: Any) -> dict[str, Any]:
    """参考实现 ``payment_page_summary``: 轮询可观测性的结构化摘要。"""
    if not isinstance(payload, dict):
        return {}
    elements_options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    submission = _upi_find_submission_attempt(payload)
    next_action = _upi_first_value_by_key(payload, "next_action")
    payment_intent = _upi_first_value_by_key(payload, "payment_intent")
    setup_intent = _upi_first_value_by_key(payload, "setup_intent")
    summary: dict[str, Any] = {
        "object": payload.get("object"),
        "id": payload.get("id"),
        "status": payload.get("status"),
        "payment_status": payload.get("payment_status"),
        "amount": elements_options.get("amount") if elements_options else _upi_first_value_by_key(payload, "amount"),
        "currency": payload.get("currency") or (elements_options.get("currency") if elements_options else None),
        "mode": elements_options.get("mode") if elements_options else payload.get("mode"),
        "payment_method_types": elements_options.get("payment_method_types") if elements_options else None,
        "submission_state": submission.get("state") if submission else None,
        "submission_status": submission.get("status") if submission else None,
        "has_next_action": isinstance(next_action, dict) and bool(next_action),
    }
    if isinstance(payment_intent, dict):
        summary["payment_intent_status"] = payment_intent.get("status")
    elif isinstance(payment_intent, str):
        summary["payment_intent"] = payment_intent
    if isinstance(setup_intent, dict):
        summary["setup_intent_status"] = setup_intent.get("status")
    elif isinstance(setup_intent, str):
        summary["setup_intent"] = setup_intent
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _upi_format_summary(summary: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in summary.items())


def _upi_intent_redirect_url(
    stripe: Any,
    intent_payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> str:
    """参考实现 ``stripe_intent_redirect_url``: 从 setup/payment intent 取跳转。"""
    if not isinstance(intent_payload, dict):
        return ""
    intent_id = str(intent_payload.get("id") or "").strip()
    client_secret = str(intent_payload.get("client_secret") or "").strip()
    if not intent_id or not client_secret:
        return ""
    intent_object = str(intent_payload.get("object") or "").strip()
    intent_path = (
        "setup_intents" if intent_object == "setup_intent" or intent_id.startswith("seti_") else "payment_intents"
    )
    params = {"key": stripe_pk, "client_secret": client_secret}
    url = STRIPE_INTENT_URL_T.format(intent_path=intent_path, intent_id=intent_id)
    try:
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except Exception as exc:
        _emit("intent", f"intent lookup failed (non-fatal): {type(exc).__name__}: {exc}")
        return ""
    if resp.status_code != 200:
        _emit("intent", f"intent lookup returned HTTP {resp.status_code} (non-fatal)")
        _upi_dump_http(resp, "intent_lookup_error", None, "GET", url, force=resp.status_code >= 400)
        return ""
    try:
        payload = resp.json() or {}
    except Exception as exc:
        # 不能静默：JSON 解析失败会让下面的 last_setup_error 判读拿到空 payload，
        # 于是「风控已拒绝」被误判成「还在等待」。这里把原文留进 payload 也留进日志。
        _emit("intent", f"intent JSON parse failed (non-fatal): {type(exc).__name__}: {exc}")
        payload = {"_raw_text": getattr(resp, "text", "")}
    _upi_raise_if_setup_intent_blocked(payload, "stripe intent", current_pm_id=current_pm_id)
    return _upi_extract_redirect_url(payload)


def _upi_payload_intent_redirect_url(
    stripe: Any,
    payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> str:
    """参考实现 ``stripe_payload_intent_redirect_url``: 遍历 payload 里的 intent。"""
    if not isinstance(payload, dict):
        return ""
    for intent_key in ("setup_intent", "payment_intent"):
        candidates: list[Any] = []
        direct = payload.get(intent_key)
        if isinstance(direct, dict):
            candidates.append(direct)
        nested = _upi_first_value_by_key(payload, intent_key)
        if isinstance(nested, dict) and all(nested is not item for item in candidates):
            candidates.append(nested)
        for intent_payload in candidates:
            redirect_url = _upi_intent_redirect_url(stripe, intent_payload, stripe_pk, current_pm_id=current_pm_id)
            if redirect_url:
                return redirect_url
    return ""


def _upi_needs_setup_recover(payload: Any) -> bool:
    """参考实现 ``need_setup_recover``（``provider_checkout``）的判据。

    批准之后的 Payment Page 常常给出「已批准但没有任何 redirect/QR」的形态，
    SetupIntent 停在 ``requires_payment_method``。UPI AutoPay 只有直连
    SetupIntent 补交 mandate 才会产出 ``upi://``。缺这道判据时，下面的
    ``failed`` 分支会把同一个 ``generic_decline`` 误判成终态拒绝（2026-09-30
    实测：approve 第 1 次就 approved，然后立刻死在这里，只拿到 hosted 兜底）。

    条件与参考实现逐条对齐：无 redirect/QR，且
    ``setup_status in {requires_payment_method, requires_confirmation, ""}``
    或存在 setup_intent 失败原因或命中 ``generic_decline``。
    """
    if not isinstance(payload, Mapping):
        return False
    if _upi_extract_redirect_url(payload) or _upi_extract_qr_candidates(payload):
        return False
    setup_intent = _upi_first_value_by_key(payload, "setup_intent")
    status = str(setup_intent.get("status") or "").lower() if isinstance(setup_intent, Mapping) else ""
    if status in ("requires_payment_method", "requires_confirmation", ""):
        return True
    failure = _upi_setup_intent_last_error(payload)
    return bool(failure)


def _upi_poll_payment_page(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    ctx: Mapping[str, Any],
    current_pm_id: str = "",
    rescue: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[str, list[str]]:
    """参考实现 ``poll_payment_page``: 轮询到「真跳转 / QR」或终态。

    返回 ``(redirect_url, qr_candidates)``。与旧实现的关键差异:

    * 识别终态 ``requires_approval``（继续等）与 ``failed``（含 generic_decline →
      直接判风控拒绝, 不再空转）。
    * 每轮都判读 SetupIntent 的 ``last_setup_error``。
    * 拿到 ``next_action`` 但没有直接可用的 URL 时, 还会追问 intent 一次。

    ``rescue`` 是批准后「SetupIntent 未产出动作」的补交回调（参考实现
    ``need_setup_recover`` + ``confirm_local_setup_intent``，见
    :func:`_upi_needs_setup_recover`）。只在第一轮命中时调一次；返回 ``True``
    表示确实补交了，值得再轮询一轮。策略留在调用方，这里只管循环控制。
    """
    deadline = time.time() + _env_int("UPI_POLL_TIMEOUT", 45)
    params = {
        **_upi_elements_session_params(ctx),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }
    url = STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id)
    last_error = ""
    last_summary = ""
    grace_deadline = 0.0
    grace_seconds = _env_int("UPI_FAILED_STATE_GRACE_POLL", 5, minimum=1)
    attempted_rescue = False

    while time.time() < deadline:
        try:
            resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except Exception as exc:
            last_error = f"poll transport error: {type(exc).__name__}"
            time.sleep(1)
            continue
        if resp.status_code >= 400:
            last_error = f"HTTP {resp.status_code}"
            _upi_dump_http(resp, "poll_error", None, "GET", url, force=True)
            if _env_bool("UPI_STOP_ON_POLL_4XX", False):
                break
            time.sleep(1)
            continue
        try:
            payload = resp.json() or {}
        except Exception as exc:
            # 静默置空会让本轮 summary 为空、状态判读跳过——看起来「没有进展」，
            # 实际是解析失败。留一行诊断，失败现场也能对着 dump 复盘。
            payload = {}
            _emit("poll", f"payment_page JSON parse failed: {type(exc).__name__}: {exc}")
        summary = _upi_payment_page_summary(payload)
        summary_text = _upi_format_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            _emit("poll", f"summary: {summary_text}")

        redirect_url = _upi_extract_redirect_url(payload)
        qr_urls = _upi_extract_qr_candidates(payload)
        if redirect_url or qr_urls:
            _upi_dump_http(resp, "poll_success", params, "GET", url, force=True)
            return redirect_url, qr_urls

        # 批准后没有任何 redirect/QR 时先补交 SetupIntent，再判失败。
        if not attempted_rescue and rescue is not None and _upi_needs_setup_recover(payload):
            attempted_rescue = True
            if rescue(payload):
                last_error = "post-approval SetupIntent rescue attempted"
                time.sleep(1)
                continue

        submission = _upi_find_submission_attempt(payload)
        state = str(submission.get("state") or "")
        if state == "requires_approval":
            last_error = "payment_pages still requires_approval"
            time.sleep(1)
            continue
        if state == "failed":
            last_message = json.dumps(submission.get("last_error") or {}, ensure_ascii=False)
            declined = _upi_is_provider_decline_text(last_message) or _upi_is_provider_decline_text(
                _upi_setup_intent_last_error(payload, current_pm_id)
            )
            if declined:
                if not grace_deadline:
                    grace_deadline = time.time() + grace_seconds
                    _emit("poll", f"observed failed/generic_decline, grace poll {grace_seconds}s")
                if time.time() < grace_deadline:
                    last_error = "failed generic_decline grace polling"
                    time.sleep(1)
                    continue
                raise RuntimeError(_upi_provider_decline_message("stripe payment_pages"))
            raise RuntimeError(f"Stripe submission failed: {submission}")

        _upi_raise_if_setup_intent_blocked(payload, "stripe payment_pages", current_pm_id=current_pm_id)
        intent_redirect = _upi_payload_intent_redirect_url(stripe, payload, stripe_pk, current_pm_id=current_pm_id)
        if intent_redirect:
            return intent_redirect, qr_urls
        last_error = str(summary_text or "waiting")
        time.sleep(1)

    raise RuntimeError(f"redirect url resolution timeout: {last_error}")


def _upi_should_retry_second_confirm(error: Any) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in UPI_SECOND_CONFIRM_MARKERS)


def _upi_hosted_fallback_result(
    *,
    cs_id: str,
    processor_entity: str,
    init: Mapping[str, Any],
    amount: int,
    payment_currency: str,
    target_country: str,
    checkout_country: str,
    payment_country: str,
    pm_types: list[str],
    checkout_proxy: str,
    provider_proxy: str,
    approve_proxy: str,
    checkout_ui_mode: str,
    qr_path: str | None,
    warning: str = "",
) -> dict[str, Any]:
    """confirm 失败时的 hosted 兜底结果（保持旧字段集, 只多增不减少）。"""
    hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or ""))
    if not hosted_url:
        hosted_url = f"https://pay.openai.com/c/pay/{cs_id}"
    written_qr_path = _write_qr_png(hosted_url, qr_path or "")
    result: dict[str, Any] = {
        "ok": True,
        "payment_method": "upi",
        "method": "upi",
        "link_type": "upi_hosted_fallback",
        "url": hosted_url,
        "qr_data": hosted_url,
        "qr_path": written_qr_path,
        "cs_id": cs_id,
        "processor_entity": processor_entity,
        "amount": amount,
        "currency": payment_currency.upper(),
        "target_country": target_country,
        "checkout_country": checkout_country,
        "billing_country": checkout_country,
        "payment_country": payment_country,
        "payment_method_types": pm_types,
        "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy,
        "approve_proxy": approve_proxy,
        "checkout_ui_mode": checkout_ui_mode,
    }
    if warning:
        result["warning"] = warning
    return result


def _upi_build_confirmation_token_body(
    *,
    cs_id: str,
    stripe_pk: str,
    billing: Mapping[str, str],
    ctx: Mapping[str, Any],
    elements: Mapping[str, Any],
    payment_method_types: Sequence[str],
) -> dict[str, str]:
    """Port of the reference ``_confirmation_token`` form (OAICS rail).

    Unlike the custom ``payment_pages/confirm`` body this one carries
    ``setup_future_usage`` + ``mandate_data[customer_acceptance]``. Stripe
    answers the custom confirm with ``400 parameter_unknown: mandate_data``
    (measured 2026-09-26), which is why a UPI SetupIntent created that way can
    still come back ``generic_decline``.
    """
    elements_session_id = str(elements.get("session_id") or ctx.get("elements_session_id") or "")
    elements_config_id = str(elements.get("config_id") or ctx.get("elements_session_config_id") or "")
    _customer = elements.get("customer")
    customer_obj: Mapping[str, Any] = _customer if isinstance(_customer, Mapping) else {}
    customer_id = str(customer_obj.get("id") or customer_obj.get("customer") or "")
    stripe_js_id = str(ctx.get("stripe_js_id") or "")
    methods = [str(m).lower() for m in (payment_method_types or []) if str(m).strip()]
    if not methods:
        methods = ["card", "link", "upi"]
    runtime_version = str(ctx.get("runtime_version") or _upi_runtime_version())
    attribution = {
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
        "elements_session_id": elements_session_id,
        "elements_session_config_id": elements_config_id,
        "merchant_integration_additional_elements[0]": "expressCheckout",
        "merchant_integration_additional_elements[1]": "payment",
        "merchant_integration_additional_elements[2]": "address",
    }
    body: dict[str, str] = {
        "payment_method_data[type]": "upi",
        "payment_method_data[allow_redisplay]": "limited",
        "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
        "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
        "payment_method_data[billing_details][address][line1]": str(billing.get("line1") or ""),
        "payment_method_data[billing_details][address][city]": str(billing.get("city") or ""),
        "payment_method_data[billing_details][address][country]": str(billing.get("country") or "IN"),
        "payment_method_data[billing_details][address][postal_code]": str(billing.get("postal_code") or ""),
        "payment_method_data[billing_details][address][state]": str(billing.get("state") or ""),
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(secrets.randbelow(37000) + 18000),
        "payment_method_data[guid]": str(ctx.get("guid") or _upi_browser_id()),
        "payment_method_data[muid]": str(ctx.get("muid") or _upi_browser_id()),
        "payment_method_data[sid]": str(ctx.get("sid") or _upi_browser_id()),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": "inr",
        "client_context[mode]": "subscription",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "set_as_default_payment_method": "false",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }
    body["payment_method_data[client_attribution_metadata][checkout_session_id]"] = cs_id
    body["payment_method_data[client_attribution_metadata][client_session_id]"] = stripe_js_id
    for key, value in attribution.items():
        body[f"payment_method_data[client_attribution_metadata][{key}]"] = value
        body[f"client_attribution_metadata[{key}]"] = value
    if billing.get("line2"):
        body["payment_method_data[billing_details][address][line2]"] = str(billing["line2"])
    if billing.get("phone"):
        body["payment_method_data[billing_details][phone]"] = str(billing["phone"])
    for index, method in enumerate(methods):
        body[f"client_context[payment_method_types][{index}]"] = method
    if customer_id:
        body["client_context[customer]"] = customer_id
    return body

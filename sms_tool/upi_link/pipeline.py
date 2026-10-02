from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..checkout_contract import PLUS_TRIAL_CAMPAIGN_ID
except ImportError:
    from checkout_contract import PLUS_TRIAL_CAMPAIGN_ID  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..paypal_extract import CURRENCY_MAP, _new_session
except ImportError:
    from paypal_extract import CURRENCY_MAP, _new_session  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..phone_proxy import redact_proxy_text
except ImportError:
    from phone_proxy import redact_proxy_text  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_STRIPE_PK, DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:
    from pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_STRIPE_PK, DEFAULT_TIMEOUT, STRIPE_VERSION  # type: ignore
try:  # pragma: no cover - direct script execution
    from .. import payment_egress
except ImportError:
    # Direct-script execution imports this module as a bare sibling, so the
    # shared gate is unavailable there.  Explicit ``None`` rather than a silent
    # skip: the package path (CLI and ``native_upi``) always has it.
    payment_egress = None  # type: ignore[assignment]
try:  # pragma: no cover - direct script execution
    from ..geo.resolver import resolve_proxy_geo
except ImportError:
    from geo.resolver import resolve_proxy_geo  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..proxy_edge_probe import CHATGPT_CHECKOUT_PATH, probe_openai_edge
except ImportError:
    from proxy_edge_probe import CHATGPT_CHECKOUT_PATH, probe_openai_edge  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..proxy_entry import rotate_session
except ImportError:
    from proxy_entry import rotate_session  # type: ignore
from typing import Any
from collections.abc import Mapping
from types import SimpleNamespace
import json
import time
import uuid
from ._extract import (
    _upi_custom_payment_method_id,
    _upi_extract_next_action,
    _upi_extract_qr_candidates,
    _upi_extract_redirect_url,
    _upi_find_submission_attempt,
    _upi_first_value_by_key,
    _upi_get_free_trial_status,
    _upi_is_instructions_url,
    _upi_qr_image_kind,
    _upi_raise_if_setup_intent_blocked,
)
from .constants import (
    DEFAULT_CONFIG_PATH,
    STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
    STRIPE_PAYMENT_PAGE_GET_URL_T,
    UPI_APPROVAL_MAX_ATTEMPTS,
    UPI_CHECKOUT_APPROVE_URL,
    UPI_CHECKOUT_CONFIRM_URL,
    UPI_CHECKOUT_URL,
    UPI_LOCAL_MANDATE_ENABLED,
    UPI_QR_POLL_MAX_ATTEMPTS,
    UPI_SENTINEL_APPROVAL_FLOW,
)
from .env import _emit, _env_bool, _env_int, _env_str, _float_env
from .dump import _approve_backoff, _upi_dump_http
from .config import _load_json, _method_cfg, _payment_stage_proxies_from_config, _upi_retarget_region
from .session import (
    _normalize_hosted_checkout_url,
    _upi_account_id_from_token,
    _upi_apply_fingerprint,
    _upi_billing_profile,
    _upi_classify_failure,
    _upi_elements_session,
    _upi_ensure_checkout_flow,
    _upi_fingerprint,
    _upi_new_chatgpt_session,
    _upi_promo_page_url,
    _upi_record_zero_result,
    _upi_server_mandate,
    _write_qr_png,
)
from .browser import _upi_browser_approve
from .stripe import (
    _upi_build_confirm_body,
    _upi_build_ctx,
    _upi_confirm_local_mandate,
    _upi_create_upi_pm,
    _upi_elements_session_params,
    _upi_hosted_fallback_result,
    _upi_is_403,
    _upi_passive_captcha_fields,
    _upi_poll_payment_page,
    _upi_post_with_degrade,
    _upi_rebuild_ctx,
    _upi_register_stripe_fingerprint,
    _upi_should_retry_second_confirm,
    _upi_stripe_init,
)
from .sentinel import (
    _upi_apply_approve_risk,
    _upi_capture_risk_context,
    _upi_fetch_oaics_state,
    _upi_sentinel_headers,
    _upi_sentinel_ping,
    _upi_wait_paid,
)
from .extract import _upi_hydrate_qr_data, _upi_resolve_external_redirect
from .flows import _upi_run_cpmt_flow, _upi_run_oaics_flow
from .verify import (
    INCONCLUSIVE as UPI_VERIFY_INCONCLUSIVE,
    verify_instructions_verdict as _upi_verify_instructions_verdict,
)


def _upi_link_unverified_contract(verification_code: str, verification: str) -> dict[str, Any]:
    """Failure fields for a link the verify gate refused to deliver.

    The gate can refuse for three different reasons and they must not collapse
    into one code:

    * ``mandate_not_signed`` -- SetupIntent stayed at ``requires_payment_method``,
      i.e. the UPI AutoPay mandate was never signed. On the ``cs_`` rail this is
      the measured norm (Stripe refuses third-party confirmation of a SetupIntent
      Checkout created; see ``constants.UPI_LOCAL_MANDATE_ENABLED``), so a rebuilt
      Checkout may land on the CPMT / OAICS rail -- mark it retryable and never
      call it a fake link.
    * ``payment_chain`` -- the intent passed but its URI addresses the ₹1999
      payment chain. That really is the wrong artifact.
    * anything else (``canceled`` / unknown / ``already_authorized``) -- terminal.
    """
    if verification_code == "mandate_not_signed":
        return {
            "error": f"UPI AutoPay 委托未签署（{verification}）；不是废链，是 mandate 未成立",
            "error_code": "mandate_not_signed",
            "error_stage": "mandate",
            "retryable": True,
        }
    if verification_code == "payment_chain":
        return {
            "error": f"UPI link 指向 ₹1999 付款链而非 ₹0 委托（{verification}）",
            "error_code": "payment_chain_link",
            "error_stage": "artifact",
            "retryable": False,
        }
    return {
        "error": f"UPI link 未通过核验（{verification}）",
        "error_code": "link_unverified",
        "error_stage": "artifact",
        "retryable": False,
    }


def _resolve_upi_runtime(
    access_token,
    proxy,
    checkout_proxy,
    provider_proxy,
    approve_proxy,
    target_country,
    checkout_country,
    payment_country,
    require_zero,
    runtime_config,
    device_id,
    session_token,
):
    """Resolve every config/env/proxy input generate_upi_qr_link reads.

    Extracted 2026-09-19 from the top of ``generate_upi_qr_link`` (the
    79-line block before the Stripe try).  Pure input resolution -- no
    network, no shared state -- so it returns a SimpleNamespace the
    caller unpacks.  Kept in this module because it reads the same
    module-level constants (CURRENCY_MAP, UPI_* caps) as the pipeline.
    """
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
    upi_cfg = _method_cfg(cfg, "upi")
    stage_proxies = _payment_stage_proxies_from_config(cfg, "upi")
    _checkout = checkout_proxy or proxy or stage_proxies["checkout"]
    _provider = provider_proxy or proxy or stage_proxies["provider"]
    _approve = approve_proxy or proxy or stage_proxies["approve"]
    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    approve_proxy = str(_approve or "").strip()
    regions = upi_cfg.get("billing_regions") if isinstance(upi_cfg.get("billing_regions"), list) else []
    checkout_country = str(
        checkout_country
        or upi_cfg.get("checkout_country")
        or upi_cfg.get("checkout_billing_country")
        or upi_cfg.get("billing_country")
        or target_country
        or upi_cfg.get("target_country")
        or (regions[0] if regions else "IN")
        or "IN"
    ).upper()
    payment_country = str(
        payment_country or upi_cfg.get("payment_country") or upi_cfg.get("payment_method_country") or "IN"
    ).upper()
    target_country = checkout_country
    currency = CURRENCY_MAP.get(checkout_country, "INR")
    payment_currency = CURRENCY_MAP.get(payment_country, "INR")
    if require_zero is None:
        _paypal_raw = cfg.get("paypal")
        paypal_cfg: dict[str, Any] = _paypal_raw if isinstance(_paypal_raw, dict) else {}
        require_zero = bool(upi_cfg.get("require_zero_due", paypal_cfg.get("require_zero_due", True)))

    # 协议与策略开关（配置段优先, 环境变量兜底）
    checkout_ui_mode = (
        str(upi_cfg.get("checkout_ui_mode") or _env_str("UPI_CHECKOUT_UI_MODE", "custom") or "custom").strip().lower()
    )
    if checkout_ui_mode not in {"custom", "hosted"}:
        checkout_ui_mode = "custom"
    inline_pm = bool(
        upi_cfg.get("confirm_inline_pm") if "confirm_inline_pm" in upi_cfg else _env_bool("UPI_CONFIRM_INLINE_PM", True)
    )
    # Confirm/approve shape. ``reference`` mirrors the upi-zero-link pure-protocol
    # rail: create the PM separately and confirm with ``payment_method: pm_id``,
    # ``payment_method_selection_flow=merchant_specified``, no synthetic
    # ``x-oai-is-client-observation``, and reuse the Checkout-stage Sentinel for
    # approve. ``current`` keeps the legacy inline-PM + fresh-approval-Sentinel
    # + synthetic-observation path for rollback/A-B.
    approve_shape = (
        str(upi_cfg.get("approve_shape") if "approve_shape" in upi_cfg else _env_str("UPI_APPROVE_SHAPE", "reference"))
        .strip()
        .lower()
    )
    if approve_shape not in {"reference", "current"}:
        approve_shape = "reference"
    if approve_shape == "reference":
        inline_pm = False
    payment_method_selection_flow = "merchant_specified" if approve_shape == "reference" else "automatic"
    update_tax_region = bool(
        upi_cfg.get("update_tax_region") if "update_tax_region" in upi_cfg else _env_bool("UPI_UPDATE_TAX_REGION", True)
    )
    update_customer_data = bool(
        upi_cfg.get("update_customer_data")
        if "update_customer_data" in upi_cfg
        else _env_bool("UPI_UPDATE_CUSTOMER_DATA", False)
    )
    max_approve_attempts = _env_int("UPI_APPROVAL_MAX_ATTEMPTS", UPI_APPROVAL_MAX_ATTEMPTS)
    poll_max_attempts = _env_int("UPI_QR_POLL_MAX_ATTEMPTS", UPI_QR_POLL_MAX_ATTEMPTS)
    # 无头浏览器 rail：曾经是唯一能签发 approve 阶段校验用的
    # x-oai-is-client-observation 的路径，但 2026-09-30 实测它已不可用 ——
    # Cloudflare 对无头 Chromium 返回 `Just a moment...` 挑战页，SDK 的
    # frame.html 被 ERR_BLOCKED_BY_RESPONSE 拦掉；换 Camoufox 过得了 CF，
    # 真站的 script-src-elem CSP 又拒绝我们注入的 sdk.js。结果 approve 必然
    # blocked（7/7 失败）。同时它白烧 ~60s，把 approval 窗口和被动 hCaptcha
    # 预算一起耗掉（hCaptcha 从 solved 变成 120s 超时）。
    #
    # 三家参考实现（upi-zero-link / chatgpt-upi-extractor / tilian）全部是纯协议，
    # 没有一家用浏览器或 observation：哨兵走 Node 桥，approve 复用 checkout 阶段的
    # Sentinel 对。本模块的 reference 形态本来就与之对齐，所以这里默认关闭浏览器轨。
    # 关掉后实测 4/4 成功。要复现旧行为设 UPI_BROWSER_RAIL=1。
    browser_rail = (
        bool(upi_cfg.get("browser_rail")) if "browser_rail" in upi_cfg else _env_bool("UPI_BROWSER_RAIL", False)
    )
    # approve 重试之间的退避上限（秒）。参考实现是 random.uniform(1, 2)，
    # 这里做成可调：默认 1.5s，测试里置 0 即可让 60 次重试瞬间跑完。
    approve_backoff_cap = _float_env("UPI_APPROVAL_BACKOFF", 1.5)
    approve_backoff_cap = max(0.0, approve_backoff_cap)
    # 本地 mandate 阶段的开关：常量只是默认值。这一阶段要么补交成功拿到 upi://，
    # 要么整轮退化成 hosted instructions 页，所以必须能按次 A/B 或回滚。
    local_mandate_enabled = (
        bool(upi_cfg.get("local_mandate_enabled"))
        if "local_mandate_enabled" in upi_cfg
        else _env_bool("UPI_LOCAL_MANDATE_ENABLED", UPI_LOCAL_MANDATE_ENABLED)
    )

    # 一套自洽的浏览器身份贯穿全流程（旧实现每个 session 各随机一个 UA ⇒ 指纹自相矛盾）
    # locale/timezone 从契约层按 payment_country 取，不在这里硬编码。
    fingerprint_index = upi_cfg.get("fingerprint")
    try:
        fingerprint = _upi_fingerprint(
            int(fingerprint_index) if fingerprint_index is not None else None,
            payment_country,
        )
    except (TypeError, ValueError):
        fingerprint = _upi_fingerprint(country=payment_country)
    billing = _upi_billing_profile(upi_cfg if "fixed_billing" in upi_cfg else None)
    # 设备身份：调用方可传账号真实 device_id（session 文件里有），
    # 不传则生成一个自洽的 UUID——风控看的是头的存在性与一致性。
    device_id = str(device_id or "").strip() or str(uuid.uuid4())
    # UPI 建单/记账/审批都必须走账单国出口；已知 region/geo/country 模板重定向
    # 到该国家，不动粘性会话。未知格式原样保留（真实出口门禁由上层负责）。
    checkout_proxy = _upi_retarget_region(checkout_proxy, checkout_country)
    provider_proxy = _upi_retarget_region(provider_proxy, checkout_country)
    approve_proxy = _upi_retarget_region(approve_proxy, checkout_country)
    session_token = str(session_token or "").strip()
    return SimpleNamespace(
        cfg=cfg,
        upi_cfg=upi_cfg,
        stage_proxies=stage_proxies,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        regions=regions,
        checkout_country=checkout_country,
        payment_country=payment_country,
        target_country=target_country,
        currency=currency,
        payment_currency=payment_currency,
        require_zero=require_zero,
        checkout_ui_mode=checkout_ui_mode,
        inline_pm=inline_pm,
        approve_shape=approve_shape,
        payment_method_selection_flow=payment_method_selection_flow,
        update_tax_region=update_tax_region,
        update_customer_data=update_customer_data,
        max_approve_attempts=max_approve_attempts,
        poll_max_attempts=poll_max_attempts,
        approve_backoff_cap=approve_backoff_cap,
        local_mandate_enabled=local_mandate_enabled,
        browser_rail=browser_rail,
        fingerprint_index=fingerprint_index,
        fingerprint=fingerprint,
        billing=billing,
        device_id=device_id,
        session_token=session_token,
    )


def upi_invocation(
    access_token: str,
    runtime_config: Mapping[str, Any] | None = None,
    *,
    proxy: Any = None,
    auth_context: Mapping[str, Any] | None = None,
    checkout_proxy: Any = None,
    provider_proxy: Any = None,
    approve_proxy: Any = None,
    target_country: Any = None,
    checkout_country: Any = None,
    payment_country: Any = None,
    require_zero: Any = None,
    qr_path: Any = None,
    proxy_state: Any = None,
    device_id: Any = None,
    session_token: Any = None,
    wait_paid: bool = False,
    paid_timeout: float = 900.0,
    require_server_upi_mandate: bool = False,
) -> dict[str, Any]:
    """Build the canonical kwargs for :func:`generate_upi_qr_link`.

    Single assembly seam shared by the CLI (``--generate-upi-qr``) and the
    ``native_upi`` pay_link adapter. Both now expose the same option set, so the
    adapter path can honour ``wait_paid`` / ``paid_timeout`` /
    ``require_server_upi_mandate`` exactly like the CLI instead of dropping them.
    """
    return {
        "access_token": access_token,
        "runtime_config": runtime_config,
        "proxy": proxy,
        "auth_context": auth_context,
        "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy,
        "approve_proxy": approve_proxy,
        "target_country": target_country,
        "checkout_country": checkout_country,
        "payment_country": payment_country,
        "require_zero": require_zero,
        "qr_path": qr_path,
        "proxy_state": proxy_state,
        "device_id": device_id,
        "session_token": session_token,
        "wait_paid": wait_paid,
        "paid_timeout": paid_timeout,
        "require_server_upi_mandate": require_server_upi_mandate,
    }


def _upi_assert_egress_contract(rt: Any) -> dict[str, Any] | None:
    """Run the shared payment egress gate for the three UPI stage proxies.

    ``native_upi`` is an **in-process** adapter, so unlike the subprocess
    extractors it never reached
    ``pay_link.adapters._prepare_extractor``'s
    ``payment_egress.assert_egress_countries`` call -- the UPI lane created a
    real Checkout session with whatever exit the pool handed it, and the only
    guard was a hand-run probe documented in ``PROXY_GUIDE.md``.
    ``_resolve_upi_runtime`` has just retargeted all three proxies to
    ``checkout_country``, so this is the first point at which the credential
    that will actually be dialed is known.  Gating any earlier would probe the
    pre-retarget proxy and wrongly reject a region-tagged pool that the
    retarget is about to make correct.

    Returns the canonical failure result when a stage egresses from the wrong
    country (or its probe fails), ``None`` when the gate passes or is disabled.
    Mirrors ``_prepare_extractor``: reject **before** the first side effect.
    """
    expected = str(rt.checkout_country or "").strip().upper()
    if not expected or payment_egress is None:
        return None
    options = {
        "checkout_proxy": rt.checkout_proxy,
        "stripe_init_proxy": rt.provider_proxy,
        "approve_proxy": rt.approve_proxy,
        # All three were retargeted to ``checkout_country`` above; the UPI lane
        # has no separate per-stage country (``payment_country`` only covers the
        # currency/billing profile).
        "stage_proxy_countries": {
            "checkout": expected,
            "stripe_init": expected,
            "approve": expected,
        },
    }
    try:
        payment_egress.assert_egress_countries(
            options,
            rt.cfg,
            stages=("checkout", "stripe_init", "approve"),
        )
    except payment_egress.EgressCheckError as exc:
        return exc.to_result("upi")
    return None


# ── Increment 1 (2026-10-01): India exit grading + checkout admission ──────
# Ported from the reference ``upi_core_local`` (``_is_india_exit`` /
# ``pick_india_proxy`` / ``select_checkout_capable_ip`` / ``probe_checkout_reachable``).
# It measured that an "IN" residential session can leak a non-IN exit and that a
# wrong/blocked exit is rejected at Checkout -- both cost a disposable session.
# Off by default because it adds one geo probe plus one edge probe per candidate;
# the shared ``payment_egress`` gate still asserts the final exit.

#: Anonymous paths a real frontend reaches during the payment flow.  A non-CF
#: reply below 500 means the exit can enter checkout.
_UPI_ADMISSION_PATHS: tuple[str, ...] = (CHATGPT_CHECKOUT_PATH, "/")


def _upi_india_exit_probe_enabled(upi_cfg: Any) -> bool:
    if isinstance(upi_cfg, Mapping) and "india_exit_probe" in upi_cfg:
        return bool(upi_cfg.get("india_exit_probe"))
    return _env_bool("UPI_INDIA_EXIT_PROBE", False)


def _upi_india_exit_attempts(upi_cfg: Any) -> int:
    raw = upi_cfg.get("india_exit_attempts") if isinstance(upi_cfg, Mapping) else None
    if raw is not None:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 2
    return max(1, _env_int("UPI_INDIA_EXIT_ATTEMPTS", 2, 1))


def _upi_india_exit_timeout(upi_cfg: Any) -> float:
    raw = upi_cfg.get("india_exit_timeout") if isinstance(upi_cfg, Mapping) else None
    if raw is not None:
        try:
            return max(3.0, min(float(raw), 20.0))
        except (TypeError, ValueError):
            pass
    return max(3.0, min(_float_env("UPI_INDIA_EXIT_TIMEOUT", 8.0), 20.0))


def _upi_exit_country(proxy: str, timeout: float) -> str:
    try:
        geo = resolve_proxy_geo(proxy, hint="IN", timeout=timeout)
    except Exception:
        return ""
    return str(getattr(geo, "country", "") or "").strip().upper()


def _upi_exit_admits_checkout(proxy: str, timeout: float) -> bool:
    """True when a non-CF, sub-500 reply comes back from an admission path.

    A transport failure is *no evidence*, not a refusal: the reference lets a
    geo-unknown exit through rather than blocking on a failed lookup, and a
    single residential flap must not kill an otherwise usable exit.
    """
    saw_response = False
    for path in _UPI_ADMISSION_PATHS:
        verdict = probe_openai_edge(proxy, path=path, timeout=timeout)
        code = int(getattr(verdict, "http_status", 0) or 0)
        if code <= 0:
            continue
        saw_response = True
        if not bool(getattr(verdict, "blocked_by_cloudflare", False)) and code < 500:
            return True
    return not saw_response


def _upi_rotate_region_session(proxy: str, country: str) -> str:
    try:
        return str(rotate_session(proxy, country) or proxy)
    except Exception:
        return proxy


def _upi_select_india_exit(
    proxy: Any,
    *,
    country: str,
    attempts: int,
    timeout: float,
    emit: Any = None,
) -> str:
    """Pick an IN exit that can actually enter checkout, rotating the session.

    Rotation stays inside the **same** proxy credential (new sticky session),
    never across a different proxy: the caller's pool ownership is untouched.
    On exhaustion the last candidate is returned so the shared egress gate --
    not this selector -- owns the final verdict.
    """
    text = str(proxy or "").strip()
    expected = str(country or "").strip().upper()
    if not text or expected != "IN":
        return text
    log = emit if callable(emit) else (lambda *_a, **_k: None)
    candidate = text
    total = max(1, int(attempts))
    for index in range(1, total + 1):
        observed = _upi_exit_country(candidate, timeout)
        if observed and observed != expected:
            log("india_exit", f"[{index}/{total}] exit is {observed}, not {expected}; rotating session")
        elif _upi_exit_admits_checkout(candidate, timeout):
            log("india_exit", f"[{index}/{total}] exit {observed or 'IN'} admits checkout")
            return candidate
        else:
            log("india_exit", f"[{index}/{total}] exit cannot enter checkout; rotating session")
        if index >= total:
            break
        rotated = _upi_rotate_region_session(candidate, expected)
        if not rotated or rotated == candidate:
            break
        candidate = rotated
    return candidate


# ── Increment 2 (2026-10-01): re-init + re-tax before confirm ─────────────
def _upi_repeat_tax_region(upi_cfg: Any) -> bool:
    """``upi.repeat_tax_region`` / ``UPI_REPEAT_TAX_REGION`` (default on).

    The reference measured (2026-09-21) that the ``init -> tax -> init -> tax``
    order before confirm is what makes approve pass reliably; the second pass is
    what this switch adds.
    """
    if isinstance(upi_cfg, Mapping) and "repeat_tax_region" in upi_cfg:
        return bool(upi_cfg.get("repeat_tax_region"))
    return _env_bool("UPI_REPEAT_TAX_REGION", True)


# ── Increment 3 (2026-10-01): reopen the Checkout on a fresh exit ─────────
#: Failures that happen **before** the Stripe confirm side effect.  Only these
#: may be retried on a fresh exit: after confirm the account's zero-eligibility
#: has been consumed and the approve action is one-shot, so
#: ``mandate_not_signed`` / ``payment_chain_link`` / ``link_unverified`` /
#: ``upi_provider_declined`` must never re-open a Checkout here.
_UPI_PRECONFIRM_RETRY_CODES: frozenset[str] = frozenset(
    {
        "no_free_trial",
        "upi_not_available",
        "checkout_failed",
        "checkout_bad_response",
        "upi_checkout_not_active",
        "oaics_prerequisites_missing",
        "oaics_elements_failed",
    }
)


def _upi_rounds(runtime_config: Any) -> int:
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
    upi_cfg = _method_cfg(cfg, "upi")
    raw = upi_cfg.get("rounds") if isinstance(upi_cfg, Mapping) else None
    if raw is None:
        return max(1, _env_int("UPI_ROUNDS", 1, 1))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _upi_round_retryable(result: Any) -> bool:
    if not isinstance(result, Mapping) or result.get("ok"):
        return False
    return str(result.get("error_code") or "") in _UPI_PRECONFIRM_RETRY_CODES


def _upi_rotate_proxy_set(values: tuple[Any, ...], country: str) -> tuple[Any, ...]:
    """Rotate each distinct non-empty proxy **once**, preserving identity.

    When the three stage proxies are the same string they must stay the same
    exit -- rotating each independently would split one exit into three.
    """
    mapping: dict[str, str] = {}
    out: list[Any] = []
    for value in values:
        text = str(value or "")
        if not text:
            out.append(value)
            continue
        if text not in mapping:
            mapping[text] = _upi_rotate_region_session(text, country)
        out.append(mapping[text])
    return tuple(out)


def _generate_upi_qr_link_once(
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
    _rc = _resolve_upi_runtime(
        access_token=access_token,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        target_country=target_country,
        checkout_country=checkout_country,
        payment_country=payment_country,
        require_zero=require_zero,
        runtime_config=runtime_config,
        device_id=device_id,
        session_token=session_token,
    )
    cfg = _rc.cfg
    upi_cfg = _rc.upi_cfg
    stage_proxies = _rc.stage_proxies
    checkout_proxy = str(_rc.checkout_proxy or "")
    provider_proxy = str(_rc.provider_proxy or "")
    approve_proxy = str(_rc.approve_proxy or "")
    regions = _rc.regions
    checkout_country = str(_rc.checkout_country or "")
    payment_country = str(_rc.payment_country or "")
    target_country = str(_rc.target_country or "")
    currency = _rc.currency
    payment_currency = _rc.payment_currency
    require_zero = _rc.require_zero
    checkout_ui_mode = _rc.checkout_ui_mode
    inline_pm = _rc.inline_pm
    approve_shape = _rc.approve_shape
    payment_method_selection_flow = _rc.payment_method_selection_flow
    update_tax_region = _rc.update_tax_region
    update_customer_data = _rc.update_customer_data
    max_approve_attempts = _rc.max_approve_attempts
    poll_max_attempts = _rc.poll_max_attempts
    approve_backoff_cap = _rc.approve_backoff_cap
    local_mandate_enabled = _rc.local_mandate_enabled
    browser_rail = _rc.browser_rail
    fingerprint_index = _rc.fingerprint_index
    fingerprint = _rc.fingerprint
    billing = _rc.billing
    device_id = str(_rc.device_id or "")
    session_token = str(_rc.session_token or "")

    emit = _emit

    # ── Increment 1: India exit selection + checkout admission ──────────
    # Runs before the shared egress gate so the gate validates the exit this
    # selector actually chose (a rotated session), not the pre-rotation one.
    if _upi_india_exit_probe_enabled(upi_cfg):
        _exit_attempts = _upi_india_exit_attempts(upi_cfg)
        _exit_timeout = _upi_india_exit_timeout(upi_cfg)
        _rc.checkout_proxy = _upi_select_india_exit(
            _rc.checkout_proxy, country=checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=emit
        )
        _rc.provider_proxy = _upi_select_india_exit(
            _rc.provider_proxy, country=checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=emit
        )
        _rc.approve_proxy = _upi_select_india_exit(
            _rc.approve_proxy, country=checkout_country, attempts=_exit_attempts, timeout=_exit_timeout, emit=emit
        )
        checkout_proxy = str(_rc.checkout_proxy or "")
        provider_proxy = str(_rc.provider_proxy or "")
        approve_proxy = str(_rc.approve_proxy or "")

    # First point at which the retargeted stage proxies are known, and before
    # Stage 1 creates a Checkout session -- the last moment a mis-routed exit
    # can be rejected for the price of one probe instead of a disposable
    # session.
    _egress_failure = _upi_assert_egress_contract(_rc)
    if _egress_failure is not None:
        return _egress_failure

    try:
        # ── Stage 1: ChatGPT checkout ────────────────────────────────────
        emit(
            "checkout",
            f"Stage 1: using {redact_proxy_text(checkout_proxy or 'DIRECT', checkout_proxy)} for UPI checkout (ui_mode={checkout_ui_mode})",
        )
        cs = _upi_new_chatgpt_session(checkout_proxy, fingerprint, device_id, session_token)
        cs.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": "https://chatgpt.com/",
                # 网关路由头（参考实现在请求级传；本 session 只打这一个
                # 端点，放 session 头等价且不破坏 FakeSession.post 签名）
                "x-openai-target-path": "/backend-api/payments/checkout",
                "x-openai-target-route": "/backend-api/payments/checkout",
            }
        )
        risk = _upi_capture_risk_context(
            cs,
            access_token=access_token,
            device_id=device_id,
            page_url=_upi_promo_page_url(),
            fingerprint=fingerprint,
            proxy=checkout_proxy,
            session_token=session_token,
            use_browser=browser_rail,
            sentinel_flow=_upi_ensure_checkout_flow(),
        )
        cs.headers.update(risk.headers(account_id=_upi_account_id_from_token(access_token)))
        checkout_sentinel_headers = _upi_sentinel_headers(
            cs,
            device_id,
            checkout_proxy,
            flow=_upi_ensure_checkout_flow(),
            supplied_token=risk.sentinel_tokens.get(_upi_ensure_checkout_flow(), ""),
            fingerprint=fingerprint,
            page_url=_upi_promo_page_url(),
        )
        cs.headers.update(checkout_sentinel_headers)
        _upi_sentinel_ping(cs, proxy=checkout_proxy, referer="https://chatgpt.com/")
        checkout_body: dict[str, Any] = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": PLUS_TRIAL_CAMPAIGN_ID, "is_coupon_from_query_param": False},
            "checkout_ui_mode": checkout_ui_mode,
        }
        r = cs.post(UPI_CHECKOUT_URL, json=checkout_body, timeout=CHATGPT_TIMEOUT)
        _upi_dump_http(r, "chatgpt_checkout", checkout_body, "POST", UPI_CHECKOUT_URL, force=r.status_code >= 400)
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
        checkout_data = r.json() or {}
        cs_id = str(checkout_data.get("checkout_session_id") or checkout_data.get("id", "") or "")
        # Accept both rails the backend can pick: ``cs_*`` (Stripe payment_pages,
        # custom) and ``oaics_*`` (deferred-intent Elements). The prefix is the
        # reliable signal; the response's checkout_ui_mode string can disagree.
        if not cs_id.startswith(("cs_", "oaics_")):
            return {
                "ok": False,
                "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}",
                "error_code": "checkout_bad_response",
                "payment_method": "upi",
            }
        stripe_pk = checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
        processor_entity = checkout_data.get("processor_entity") or (
            "openai_llc" if checkout_country == "US" else "openai_ie"
        )
        emit("checkout", f"checkout success: cs_id={cs_id}")
        is_oaics = cs_id.startswith("oaics_")
        _oaics_state: dict[str, Any] = {}
        if is_oaics:
            # oaics_ sessions carry a CustomerSession secret instead of a
            # Checkout client secret; refresh the authenticated state once so
            # the deferred-intent rail sees the authoritative method list.
            checkout_ui_mode = "deferred"
            _oaics_state = _upi_fetch_oaics_state(
                cs, access_token, cs_id=cs_id, processor_entity=processor_entity, proxy=checkout_proxy
            )
            emit(
                "oaics",
                f"oaics_ session: state refreshed ({len(_oaics_state)} keys)"
                if _oaics_state
                else "oaics_ session: state unavailable",
            )

        # ── Stage 2: Stripe init (custom mode) ───────────────────────────
        emit(
            "stripe_init",
            f"Stage 2: using {redact_proxy_text(provider_proxy or 'DIRECT', provider_proxy)} for Stripe init",
        )
        stripe = _new_session(provider_proxy)
        _upi_apply_fingerprint(stripe, fingerprint)
        stripe_js_id = uuid.uuid4().hex
        if _env_bool("UPI_STRIPE_FINGERPRINT", True):
            # Stripe.js 在任何业务请求之前先向 m.stripe.com/6 登记设备指纹；
            # 「从未登记过」是 Radar 能直接看出来的异常（文档 §7.1）。
            _upi_register_stripe_fingerprint(stripe, stripe_js_id, fingerprint)
        if is_oaics:
            customer_session_secret = str(
                checkout_data.get("customer_session_client_secret")
                or checkout_data.get("customer_session_secret")
                or (_oaics_state.get("customer_session_client_secret") if isinstance(_oaics_state, Mapping) else "")
                or (_oaics_state.get("customer_session_secret") if isinstance(_oaics_state, Mapping) else "")
                or ""
            )
            if not customer_session_secret:
                return {
                    "ok": False,
                    "error": "oaics_ checkout missing customer_session_client_secret",
                    "error_code": "oaics_prerequisites_missing",
                    "payment_method": "upi",
                    "cs_id": cs_id,
                }
            init = _upi_elements_session(
                stripe,
                stripe_pk=stripe_pk,
                amount=(_oaics_state.get("amount") if isinstance(_oaics_state, Mapping) else 0),
                stripe_js_id=stripe_js_id,
                customer_session_secret=customer_session_secret,
                fingerprint=fingerprint,
            )
            if not init:
                return {
                    "ok": False,
                    "error": "oaics_ elements session negotiation failed",
                    "error_code": "oaics_elements_failed",
                    "payment_method": "upi",
                    "cs_id": cs_id,
                }
        else:
            init = _upi_stripe_init(stripe, cs_id, stripe_pk, fingerprint, stripe_js_id)
        ctx = _upi_build_ctx(init, fingerprint, stripe_js_id)
        # Stripe's passive hCaptcha token cannot be forged offline; a confirm
        # without it is answered later at the payment-setup stage with
        # ``setup_attempt_failed`` / ``generic_decline``. Solve it here (once)
        # and let ``_upi_build_confirm_body`` emit the fields.
        _passive = _upi_passive_captcha_fields(
            init,
            proxy=provider_proxy or checkout_proxy,
            locale=str(fingerprint.get("locale") or "en-US"),
        )
        if _passive is not None:
            ctx["passive_captcha"] = _passive
        # Prefer the Stripe identifiers the live browser actually used so the
        # confirm request stays consistent with the page that minted them.
        for _key in ("guid", "muid", "sid"):
            if risk.stripe_ids.get(_key) and not ctx.get(_key):
                ctx[_key] = risk.stripe_ids[_key]
        emit("stripe_init", "init success, analyzing free trial...")
        if require_server_upi_mandate:
            mandate = _upi_server_mandate(init)
            emit(
                "mandate",
                "server mandate_options="
                + ("present" if mandate else "missing")
                + (f" end_date={mandate.get('end_date')}" if mandate else ""),
            )

        # ── Stage 3: Free trial detection ────────────────────────────────
        ft_status = _upi_get_free_trial_status(init)
        amount = ft_status["due"]
        pm_types = ft_status["payment_method_types"]
        emit(
            "stripe_init",
            f"free_trial={ft_status['has_free_trial']} due={amount} coupon={ft_status['coupon_name']} upi={ft_status['has_upi']}",
        )
        # 0 元缓存：本轮 checkout 代理有没有产出免费试用，记下来供后续批次调度。
        # 必须在 ``require_zero`` 的早退**之前**记——那次失败恰恰是最有价值的
        # 负样本（"这个代理出的是非零"），早退掉就永远学不到。
        _upi_record_zero_result(proxy_state, checkout_proxy, checkout_country, amount)
        if require_zero and not ft_status["has_free_trial"]:
            return {
                "ok": False,
                "error": f"no_free_trial: due={amount} coupon={ft_status['coupon_name']} percent_off={ft_status['percent_off']}",
                "error_code": "no_free_trial",
                "payment_method": "upi",
                "cs_id": cs_id,
                "amount": amount,
                "currency": payment_currency.upper(),
                "target_country": target_country,
                "checkout_country": checkout_country,
                "billing_country": checkout_country,
                "payment_country": payment_country,
                "coupon_name": ft_status["coupon_name"],
                "percent_off": ft_status["percent_off"],
            }
        if pm_types and not ft_status["has_upi"]:
            return {
                "ok": False,
                "error": f"UPI not available for checkout; payment_method_types={pm_types}",
                "error_code": "upi_not_available",
                "payment_method": "upi",
                "cs_id": cs_id,
                "payment_method_types": pm_types,
                "amount": amount,
                "currency": payment_currency.upper(),
                "target_country": target_country,
                "checkout_country": checkout_country,
                "billing_country": checkout_country,
                "payment_country": payment_country,
            }

        # ── Stage 3b: cpmt bypass ────────────────────────────────────────
        # When Checkout advertises a UPI custom payment method, prefer the
        # ChatGPT-side confirm+start rail. It avoids the Stripe SetupIntent
        # stage entirely, which is the known source of generic_decline.
        cpm_id = _upi_custom_payment_method_id(checkout_data.get("custom_payment_methods"))
        if cpm_id:
            emit("cpmt", f"Checkout advertises UPI custom method {cpm_id}; using the no-SetupIntent rail")
            return _upi_run_cpmt_flow(
                cs,
                access_token=access_token,
                cs_id=cs_id,
                cpm_id=cpm_id,
                processor_entity=processor_entity,
                amount=amount,
                payment_currency=payment_currency,
                target_country=target_country,
                checkout_country=checkout_country,
                payment_country=payment_country,
                device_id=device_id,
                checkout_proxy=checkout_proxy,
                qr_path=qr_path or "",
            )

        # ── Stage 3c: OAICS / deferred rail ─────────────────────────────
        # An ``oaics_`` session has no Stripe Checkout client secret; its
        # confirm is the reference's two-step ``/v1/confirmation_tokens`` ->
        # ChatGPT ``/checkout/confirm`` sequence, and only that rail accepts
        # the UPI mandate acceptance fields. See ``_upi_run_oaics_flow`` for
        # the measured reachability caveat.
        if is_oaics:
            emit("oaics", "OAICS rail: confirmation_tokens -> chatgpt confirm -> payment_intent confirm")
            return _upi_run_oaics_flow(
                stripe,
                cs,
                access_token=access_token,
                device_id=device_id,
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                processor_entity=processor_entity,
                billing=billing,
                fingerprint=fingerprint,
                ctx=ctx,
                elements=init if isinstance(init, Mapping) else {},
                oaics_state=_oaics_state,
                amount=amount,
                payment_currency=payment_currency,
                target_country=target_country,
                checkout_country=checkout_country,
                payment_country=payment_country,
                checkout_proxy=checkout_proxy,
                provider_proxy=provider_proxy,
                approve_proxy=approve_proxy,
                qr_path=qr_path,
                wait_paid=wait_paid,
                paid_timeout=paid_timeout,
            )

        # ── Stage 4: Tax region + customer data sync ─────────────────────
        tax_body: dict[str, str] = {}
        if update_tax_region:
            emit("tax_region", "Stage 4: updating tax region to IN")
            tax_body = {
                "tax_region[country]": str(billing.get("country") or "IN"),
                "tax_region[postal_code]": str(billing.get("postal_code") or ""),
                "tax_region[state]": str(billing.get("state") or ""),
                "tax_region[city]": str(billing.get("city") or ""),
                "tax_region[line1]": str(billing.get("line1") or ""),
                "key": stripe_pk,
                "_stripe_version": STRIPE_VERSION,
                **_upi_elements_session_params(ctx),
            }
            if billing.get("line2"):
                tax_body["tax_region[line2]"] = str(billing["line2"])
            try:
                tax_resp = stripe.post(
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    data=tax_body,
                    timeout=DEFAULT_TIMEOUT,
                )
            except Exception as exc:
                emit("tax_region", f"tax region transport error (non-fatal): {type(exc).__name__}: {exc}")
                tax_resp = None
            if tax_resp is not None:
                _upi_dump_http(
                    tax_resp,
                    "stripe_tax_region",
                    tax_body,
                    "POST",
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    force=tax_resp.status_code >= 400,
                )
                if tax_resp.status_code >= 400:
                    emit(
                        "tax_region",
                        f"tax region update failed (non-fatal): {tax_resp.status_code} {tax_resp.text[:200]}",
                    )
                else:
                    emit("tax_region", "tax region updated")
                    refreshed = tax_resp.json() or {}
                    if isinstance(refreshed, dict) and refreshed:
                        init = refreshed
                        ctx = _upi_rebuild_ctx(ctx, init, fingerprint, stripe_js_id)

        # ── Stage 4.5 (Increment 2): re-init + re-tax before confirm ──────
        # The reference's ``init -> tax -> init -> tax`` order.  A second init
        # re-reads Stripe's price after the region change (the first tax
        # response can still carry the pre-update amount) and is measured to
        # make the approve step pass.  Both calls are non-fatal: a failed
        # re-read must not abort a run whose first tax already succeeded.
        if update_tax_region and _upi_repeat_tax_region(upi_cfg):
            emit("tax_region", "Stage 4.5: re-init + re-tax before confirm")
            try:
                refreshed_init = _upi_stripe_init(stripe, cs_id, stripe_pk, fingerprint, stripe_js_id)
            except Exception as exc:
                refreshed_init = None
                emit("tax_region", f"Stage 4.5 re-init skipped (non-fatal): {type(exc).__name__}: {exc}")
            if isinstance(refreshed_init, dict) and refreshed_init:
                init = refreshed_init
                ctx = _upi_rebuild_ctx(ctx, init, fingerprint, stripe_js_id)
                try:
                    retax_resp = stripe.post(
                        STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                        data=tax_body,
                        timeout=DEFAULT_TIMEOUT,
                    )
                    _upi_dump_http(
                        retax_resp,
                        "stripe_tax_region_2",
                        tax_body,
                        "POST",
                        STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                        force=retax_resp.status_code >= 400,
                    )
                    if retax_resp.status_code < 400:
                        refreshed = retax_resp.json() or {}
                        if isinstance(refreshed, dict) and refreshed:
                            init = refreshed
                            ctx = _upi_rebuild_ctx(ctx, init, fingerprint, stripe_js_id)
                        emit("tax_region", "Stage 4.5: re-init + re-tax ok")
                    else:
                        emit("tax_region", f"Stage 4.5 re-tax failed (non-fatal): {retax_resp.status_code}")
                except Exception as exc:
                    emit("tax_region", f"Stage 4.5 re-tax error (non-fatal): {type(exc).__name__}: {exc}")

        if update_customer_data:
            emit("customer_data", "Stage 4: submitting IN customer_data")
            customer_body: dict[str, str] = {
                "customer_data[email]": str(billing.get("email") or ""),
                "customer_data[name]": str(billing.get("name") or ""),
                "customer_data[address][country]": str(billing.get("country") or "IN"),
                "customer_data[address][line1]": str(billing.get("line1") or ""),
                "customer_data[address][city]": str(billing.get("city") or ""),
                "customer_data[address][postal_code]": str(billing.get("postal_code") or ""),
                "expected_amount": str(ctx.get("checkout_amount") or 0),
                "key": stripe_pk,
                "_stripe_version": STRIPE_VERSION,
                **_upi_elements_session_params(ctx),
            }
            if billing.get("state"):
                customer_body["customer_data[address][state]"] = str(billing["state"])
            if billing.get("line2"):
                customer_body["customer_data[address][line2]"] = str(billing["line2"])
            try:
                cd_resp = stripe.post(
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    data=customer_body,
                    timeout=DEFAULT_TIMEOUT,
                )
                _upi_dump_http(
                    cd_resp,
                    "stripe_customer_data",
                    customer_body,
                    "POST",
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    force=cd_resp.status_code >= 400,
                )
                if cd_resp.status_code >= 400:
                    emit(
                        "customer_data", f"customer_data failed (non-fatal): {cd_resp.status_code} {cd_resp.text[:200]}"
                    )
                else:
                    emit("customer_data", "customer_data submitted")
            except Exception as exc:
                emit("customer_data", f"customer_data transport error (non-fatal): {type(exc).__name__}: {exc}")

        # ── Stage 5: Stripe confirm ──────────────────────────────────────
        pm_id = ""
        if inline_pm:
            emit("stripe_confirm", "Stage 5: Stripe confirm with inline UPI payment method")
        else:
            pm_id = _upi_create_upi_pm(stripe, cs_id, stripe_pk, billing)
            emit("stripe_confirm", f"Stage 5: Stripe confirm referencing pm={pm_id}")

        return_url = (
            _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or ""))
            or f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
        )
        confirm_body = _upi_build_confirm_body(
            cs_id=cs_id,
            stripe_pk=stripe_pk,
            ctx=ctx,
            processor_entity=processor_entity,
            init_payload=init,
            billing=billing,
            fingerprint=fingerprint,
            pm_id=pm_id,
            inline_pm=inline_pm,
            return_url=return_url,
            payment_method_selection_flow=payment_method_selection_flow,
            reference_shape=approve_shape == "reference",
        )
        confirm_resp, confirm_fingerprint = _upi_post_with_degrade(
            stripe,
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
            data=confirm_body,
            fingerprint=fingerprint,
            stage="stripe_confirm",
        )
        if _upi_is_403(confirm_resp) and confirm_fingerprint:
            # 降级重试成功后，后续阶段（approve / 二次 confirm）必须沿用同一套
            # 身份，否则又回到「confirm 用 chrome124、approve 用 chrome136」的
            # 自相矛盾状态。
            fingerprint = dict(confirm_fingerprint)
            _upi_apply_fingerprint(stripe, fingerprint)
            emit("stripe_confirm", "adopted degraded fingerprint for subsequent stages")
        if confirm_resp.status_code >= 400:
            emit("stripe_confirm", f"confirm failed: {confirm_resp.status_code} {confirm_resp.text[:300]}")
            return _upi_hosted_fallback_result(
                cs_id=cs_id,
                processor_entity=processor_entity,
                init=init,
                amount=amount,
                payment_currency=payment_currency,
                target_country=target_country,
                checkout_country=checkout_country,
                payment_country=payment_country,
                pm_types=pm_types,
                checkout_proxy=checkout_proxy,
                provider_proxy=provider_proxy,
                approve_proxy=approve_proxy,
                checkout_ui_mode=checkout_ui_mode,
                qr_path=qr_path,
                warning=f"stripe_confirm_failed: {confirm_resp.status_code}",
            )
        confirm_data = confirm_resp.json() or {}
        emit("stripe_confirm", "confirm success")
        # SetupIntent 失败判读（旧实现完全没有这一步）
        _upi_raise_if_setup_intent_blocked(confirm_data, "stripe confirm", current_pm_id=pm_id)

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
            nonlocal pm_id, confirm_data
            try:
                fresh_pm = _upi_create_upi_pm(stripe, cs_id, stripe_pk, billing)
            except Exception as exc:
                emit("decline_retry", f"fresh payment method failed: {type(exc).__name__}: {exc}")
                return ""
            retry_body = _upi_build_confirm_body(
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                ctx=ctx,
                processor_entity=processor_entity,
                init_payload=init,
                billing=billing,
                fingerprint=fingerprint,
                pm_id=fresh_pm,
                inline_pm=False,
                return_url=return_url,
                payment_method_selection_flow=payment_method_selection_flow,
                reference_shape=approve_shape == "reference",
            )
            retry_resp = stripe.post(
                STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
                data=retry_body,
                timeout=DEFAULT_TIMEOUT,
            )
            _upi_dump_http(
                retry_resp,
                "stripe_decline_retry_confirm",
                retry_body,
                "POST",
                STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
                force=retry_resp.status_code >= 400,
            )
            if retry_resp.status_code >= 400:
                emit(
                    "decline_retry",
                    f"fresh pm confirm failed: {retry_resp.status_code} {retry_resp.text[:200]}",
                )
                return ""
            pm_id = fresh_pm
            confirm_data = retry_resp.json() or {}
            emit("decline_retry", f"swapped in {fresh_pm} and re-confirmed the same session")
            return fresh_pm

        # ── Stage 6: ChatGPT approve ─────────────────────────────────────
        # 门禁严格对齐参考实现的三分支（参见参考 idx_extract 的三段 if/elif）：
        #   1) 已有 redirect              -> 不需要 approve，直接进 Stage 7
        #   2) 无 redirect 且 requires_approval -> 必须 approve
        #   3) 无 redirect 也无 QR        -> 只轮询 payment_pages 最终确认，不发 approve
        #   4) 无 redirect 但有 QR        -> 已拿到可用物，不必再 approve
        # 旧实现用的是「状态是 requires_approval 或 没有 redirect」这种并集，
        # 会把分支 3/4 也拖进 approve，既多发无谓请求也会阻塞在 60 次重试上。
        confirm_redirect = _upi_extract_redirect_url(confirm_data)
        confirm_qr_urls = _upi_extract_qr_candidates(confirm_data)
        submission = _upi_find_submission_attempt(confirm_data)
        submission_state = str(submission.get("state") or "")
        needs_approval = not confirm_redirect and submission_state == "requires_approval"
        needs_final_poll = not confirm_redirect and not confirm_qr_urls and submission_state != "requires_approval"
        if needs_final_poll:
            emit(
                "approve",
                "confirm 无 redirect/QR 且非 requires_approval，跳过 approve 直接做最终确认轮询",
            )
        blocked_count = 0
        approval_blocked = False
        # 注意：approval_ok / approval_data 必须在这里就绑定。
        # Stage 7 会无条件遍历 (confirm_data, approval_data)，若只在
        # needs_approval 分支里赋值，跳过 approve 时会 UnboundLocalError。
        approval_ok = False
        approval_data: dict[str, Any] = {}
        if needs_approval:
            emit(
                "approve",
                f"Stage 6: ChatGPT approve using {redact_proxy_text(approve_proxy or 'DIRECT', approve_proxy)}",
            )
            if browser_rail and not approval_ok:
                emit("approve", "Stage 6: headless browser approve (same-session observation)")
                browser_approve = _upi_browser_approve(
                    access_token=access_token,
                    session_token=session_token,
                    device_id=device_id,
                    proxy=approve_proxy,
                    fingerprint=fingerprint,
                    processor_entity=processor_entity,
                    cs_id=cs_id,
                )
                emit(
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
                    approval_ok = True
                    approval_data = {"result": "approved", "via": "headless_browser"}
            approve_session = _upi_new_chatgpt_session(approve_proxy, fingerprint, device_id, session_token)
            approve_session.headers.update(
                {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
                }
            )
            # The approval endpoint is risk-gated: it needs the same deployment
            # attestation, signed observation and frontend markers as Checkout,
            # plus a fresh checkout_session_approval Sentinel. Attach the full
            # The approval endpoint is risk-gated: re-capture the browser-issued
            # observation on the Checkout page so approve does not replay the
            # create-stage context (which the risk engine rejects).
            if browser_rail and not approval_ok:
                risk = _upi_capture_risk_context(
                    approve_session,
                    access_token=access_token,
                    device_id=device_id,
                    page_url=f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
                    fingerprint=fingerprint,
                    proxy=approve_proxy,
                    session_token=session_token,
                    use_browser=True,
                    sentinel_flow=UPI_SENTINEL_APPROVAL_FLOW,
                )
            _upi_apply_approve_risk(
                approve_session,
                risk,
                access_token,
                device_id,
                approve_proxy,
                fingerprint,
                payment_country,
                checkout_sentinel=checkout_sentinel_headers,
                approve_shape=approve_shape,
            )
            # The reference rail never calls /checkout/confirm (it answers 400
            # "This endpoint is only for internal checkout sessions"); skip it in
            # reference shape so the approve session is not primed by a 4xx.
            if approve_shape != "reference":
                # Try confirm endpoint first
                try:
                    confirm_chatgpt = approve_session.post(
                        UPI_CHECKOUT_CONFIRM_URL,
                        json={"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                        timeout=CHATGPT_TIMEOUT,
                    )
                    confirm_json = confirm_chatgpt.json() or {} if confirm_chatgpt.status_code < 400 else {}
                    _upi_dump_http(
                        confirm_chatgpt,
                        "chatgpt_approve_confirm",
                        {"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                        "POST",
                        UPI_CHECKOUT_CONFIRM_URL,
                        force=confirm_chatgpt.status_code >= 400,
                    )
                    if str(confirm_json.get("result", "")).lower() == "approved":
                        emit("approve", "approved via confirm endpoint")
                        approval_data = confirm_json
                        approval_ok = True
                    else:
                        approval_data = confirm_json
                except Exception as exc:
                    emit("approve", f"confirm endpoint error (non-fatal): {type(exc).__name__}: {exc}")

            # If confirm didn't approve, try approve endpoint with retries
            if not approval_ok:
                for attempt in range(1, max_approve_attempts + 1):
                    try:
                        approve_resp = approve_session.post(
                            UPI_CHECKOUT_APPROVE_URL,
                            json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
                            headers={
                                "x-openai-target-path": "/backend-api/payments/checkout/approve",
                                "x-openai-target-route": "/backend-api/payments/checkout/approve",
                            },
                            timeout=CHATGPT_TIMEOUT,
                        )
                        _upi_dump_http(
                            approve_resp,
                            f"chatgpt_approve_{attempt:02d}",
                            {"checkout_session_id": cs_id, "processor_entity": processor_entity},
                            "POST",
                            UPI_CHECKOUT_APPROVE_URL,
                            force=approve_resp.status_code >= 400,
                        )
                        if approve_resp.status_code < 400:
                            approve_json = approve_resp.json() or {}
                            result = str(approve_json.get("result", "")).lower()
                            if result == "approved":
                                emit("approve", f"approved on attempt {attempt}")
                                approval_ok = True
                                approval_data = approve_json
                                break
                            if result == "blocked":
                                # 参考实现: blocked 时保留当前线路重领 SEN/SO 并轮换
                                # 观察证明重试（文档 5.3）；只换 UA 而沿用同一份
                                # 风控上下文等于原样重放，必然继续 blocked。
                                blocked_count += 1
                                emit("approve", f"attempt {attempt}: blocked, rotating risk context")
                                _upi_apply_approve_risk(
                                    approve_session,
                                    risk,
                                    access_token,
                                    device_id,
                                    approve_proxy,
                                    fingerprint,
                                    payment_country,
                                    checkout_sentinel=checkout_sentinel_headers,
                                    approve_shape=approve_shape,
                                )
                                if attempt < max_approve_attempts:
                                    time.sleep(_approve_backoff(attempt, approve_backoff_cap))
                                continue
                            approval_data = approve_json
                        elif attempt % 10 == 0:
                            emit(
                                "approve",
                                f"attempt {attempt}/{max_approve_attempts}: status={approve_resp.status_code}",
                            )
                    except Exception as ex:
                        if attempt % 10 == 0:
                            emit("approve", f"attempt {attempt} exception: {type(ex).__name__}: {ex}")
                    if attempt < max_approve_attempts:
                        time.sleep(_approve_backoff(attempt, approve_backoff_cap))

            if not approval_ok:
                # 参考实现的判据：全部尝试都 blocked ⇒ 这是 provider 侧风控，
                # 不是「再等等就好」，必须让上层能区分出来。
                if blocked_count and blocked_count == max_approve_attempts:
                    approval_blocked = True
                    emit(
                        "approve",
                        f"all {max_approve_attempts} attempts blocked (provider risk control)",
                    )
                else:
                    emit("approve", "approval failed after all attempts, continuing to extraction")

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
        mandate_ok = False

        def _absorb_mandate(*sources: Any) -> bool:
            """在 sources 里找 ``seti_`` SetupIntent 并直连补交 mandate。

            幂等：成功一次即封口；找不到 SetupIntent 时不动。返回 True 表示确实
            发起了补交（调用方据此判断是否值得再轮询一轮），**不代表补交成功**。
            """
            nonlocal approval_data, mandate_ok
            if mandate_ok or not local_mandate_enabled:
                return False
            setup_intent: Any = {}
            for source in sources:
                candidate = _upi_first_value_by_key(source, "setup_intent")
                if isinstance(candidate, Mapping) and str(candidate.get("id") or "").startswith("seti_"):
                    setup_intent = candidate
                    break
            if not setup_intent:
                return False
            mandate = _upi_confirm_local_mandate(
                stripe,
                setup_intent=setup_intent,
                pm_id=pm_id,
                return_url=return_url,
                stripe_pk=stripe_pk,
                amount=ctx.get("checkout_amount"),
            )
            last_variant = mandate["variants"][-1]["variant"] if mandate["variants"] else "-"
            emit(
                "mandate",
                "Stage 6b: SetupIntent mandate %s via %s (%s)"
                % (
                    "ok" if mandate["ok"] else "failed",
                    last_variant,
                    str(mandate.get("error") or "no error")[:160],
                ),
            )
            mandate_ok = bool(mandate["ok"])
            if mandate.get("payload"):
                # 让 Stage 7 的 _absorb 吃下 mandate 响应里的 upi:// / 跳转。
                approval_data = {**(approval_data or {}), "local_mandate": mandate["payload"]}
            return True

        if not _absorb_mandate(confirm_data, approval_data):
            emit(
                "mandate",
                "Stage 6b: no SetupIntent yet (null until approval), rescue wired into the payment-page poll",
            )

        # ── Stage 7: Extract redirect / upi:// URI ───────────────────────
        emit("poll", "Stage 7: extracting UPI redirect / QR data")
        qr_data: dict[str, Any] = {}
        redirect_url = ""

        def _absorb(source: Any) -> None:
            """把一份响应里的跳转 / QR / upi:// 全部吸收进累积态。"""
            nonlocal redirect_url
            if not redirect_url:
                candidate = _upi_extract_redirect_url(source)
                if candidate:
                    redirect_url = candidate
            for url in _upi_extract_qr_candidates(source):
                kind = _upi_qr_image_kind(url)
                if kind == "svg":
                    qr_data.setdefault("qr_image_url_svg", url)
                elif kind in ("png", "jpg"):
                    qr_data.setdefault("qr_image_url_png", url)
            for k, v in _upi_extract_next_action(source).items():
                if v and (k == "upi_uri" or not qr_data.get(k)):
                    qr_data[k] = v

        # First check confirm/approve responses (redirect 优先, QR 补充)
        for source in (confirm_data, approval_data):
            _absorb(source)

        def _rescue_mandate(payload: Mapping[str, Any]) -> bool:
            """批准后「SetupIntent 未产出动作」的补交钩子（Stage 7 轮询回调）。

            参考实现 ``need_setup_recover``：先直连补交 mandate，再让轮询继续一轮，
            而不是把同一个 ``generic_decline`` 判成终态拒绝。
            """
            return _absorb_mandate(payload, confirm_data, approval_data)

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
        if not redirect_url and not qr_data.get("upi_uri"):
            try:
                poll_redirect, poll_qr = _upi_poll_payment_page(
                    stripe,
                    cs_id,
                    stripe_pk,
                    ctx,
                    current_pm_id=pm_id,
                    rescue=_rescue_mandate,
                    recover_decline=_resubmit_with_fresh_pm,
                    max_attempts=poll_max_attempts,
                )
            except Exception as exc:
                if not _upi_should_retry_second_confirm(exc):
                    raise
                emit("poll", f"extraction needs a second confirm: {str(exc)[:160]}")
            else:
                if poll_redirect:
                    redirect_url = poll_redirect
                for url in poll_qr:
                    kind = _upi_qr_image_kind(url)
                    if kind == "svg":
                        qr_data.setdefault("qr_image_url_svg", url)
                    elif kind in ("png", "jpg"):
                        qr_data.setdefault("qr_image_url_png", url)

        # 补交 mandate 的响应是轮询中途才到的，这里再吸收一次。
        if approval_data.get("local_mandate"):
            _absorb(approval_data)

        # If still nothing, refresh init and try a second confirm once
        if not redirect_url and not qr_data.get("upi_uri"):
            emit("poll", "re-init + second confirm to resolve UPI data")
            try:
                refreshed_init = _upi_stripe_init(stripe, cs_id, stripe_pk, fingerprint, stripe_js_id)
            except Exception as exc:
                emit("poll", f"re-init failed (non-fatal): {type(exc).__name__}: {exc}")
                refreshed_init = None
            if refreshed_init:
                init = refreshed_init
                ctx = _upi_rebuild_ctx(ctx, init, fingerprint, stripe_js_id)
                _absorb(init)
                if not redirect_url and not qr_data.get("upi_uri"):
                    try:
                        second_body = _upi_build_confirm_body(
                            cs_id=cs_id,
                            stripe_pk=stripe_pk,
                            ctx=ctx,
                            processor_entity=processor_entity,
                            init_payload=init,
                            billing=billing,
                            fingerprint=fingerprint,
                            pm_id=pm_id,
                            inline_pm=inline_pm,
                            return_url=(
                                _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or return_url
                            ),
                            payment_method_selection_flow=payment_method_selection_flow,
                            reference_shape=approve_shape == "reference",
                        )
                        second_resp = stripe.post(
                            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
                            data=second_body,
                            timeout=DEFAULT_TIMEOUT,
                        )
                        _upi_dump_http(
                            second_resp,
                            "stripe_second_confirm",
                            second_body,
                            "POST",
                            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
                            force=second_resp.status_code >= 400,
                        )
                        if second_resp.status_code < 400:
                            confirm_data = second_resp.json() or {}
                            emit("poll", "second confirm succeeded, re-extracting")
                            _absorb(confirm_data)
                    except Exception as exc:
                        emit("poll", f"second confirm failed (non-fatal): {type(exc).__name__}: {exc}")

        # Follow the external redirect to the real instructions page
        if redirect_url:
            resolved = _upi_resolve_external_redirect(stripe, redirect_url)
            if resolved and resolved != redirect_url:
                emit("redirect", f"followed redirect to {resolved[:80]}...")
                redirect_url = resolved
            if _upi_is_instructions_url(redirect_url):
                qr_data.setdefault("hosted_instructions_url", redirect_url)

        # Hydrate: fetch hosted_instructions_url HTML if no upi:// yet
        emit("hydrate", "hydrating UPI QR data from hosted instructions")
        qr_data = _upi_hydrate_qr_data(qr_data, provider_proxy, fingerprint)

        upi_uri = str(qr_data.get("upi_uri") or "")
        if not upi_uri.startswith("upi://"):
            mobile_auth = str(qr_data.get("mobile_auth_url") or "")
            upi_uri = mobile_auth if mobile_auth.startswith("upi://") else ""
        hosted_url = (
            _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or ""))
            or f"https://pay.openai.com/c/pay/{cs_id}"
        )
        if not redirect_url:
            redirect_url = hosted_url
        expires_at = qr_data.get("expires_at") or int(time.time()) + 300

        if upi_uri:
            emit("done", f"UPI URI extracted: {upi_uri[:40]}...")
            qr_data_str = upi_uri
            link_type = "upi_deep_link"
        elif _upi_is_instructions_url(redirect_url):
            emit("done", "hosted UPI instructions page resolved")
            qr_data_str = redirect_url
            link_type = "upi_instructions_url"
        else:
            emit("done", "no upi:// URI found, falling back to hosted URL")
            qr_data_str = redirect_url or hosted_url
            link_type = "upi_hosted_fallback"

        written_qr_path = _write_qr_png(qr_data_str, qr_path or "")
        paid_state = {"paid": False, "payment_status": "not_requested"}
        if wait_paid:
            emit("paid", f"waiting up to {paid_timeout:g}s for payment on {cs_id[:20]}...")
            paid_state = _upi_wait_paid(stripe, cs_id=cs_id, stripe_pk=stripe_pk, ctx=ctx, timeout=paid_timeout)
            emit("paid", f"payment_status={paid_state.get('payment_status')} paid={paid_state.get('paid')}")

        # ── 交付前核验（step 8，移植自 upi-zero-link/verify.py）───────────
        # ``hosted_instructions_url`` 在 setup_intent **被拒**时也会返回，所以
        # 「有 URL」不等于「签出了 ₹0 委托」。必须查 intent_state + fam，否则
        # 交出去的是用户打开就看到 ₹1999 付款页的废链。
        # 读不到页面属于「没结论」（inconclusive），不是「链是假的」。
        verify_target = str(qr_data.get("hosted_instructions_url") or "")
        if not verify_target and _upi_is_instructions_url(redirect_url):
            verify_target = redirect_url
        link_verified = False
        verification_inconclusive = False
        verification_code = ""
        if verify_target:
            verified, label, verification_code = _upi_verify_instructions_verdict(verify_target, proxy=provider_proxy)
            link_verified = bool(verified)
            verification_inconclusive = (not verified) and verification_code in UPI_VERIFY_INCONCLUSIVE
            verification = label if (verified or not verification_inconclusive) else f"inconclusive:{label}"
            emit("verify", f"UPI link {'verified' if verified else 'NOT verified'} ({verification})")
        else:
            verification = "no_instructions_url"

        link_result: dict[str, Any] = {
            "ok": True,
            "payment_method": "upi",
            "method": "upi",
            "link_type": link_type,
            "url": upi_uri or redirect_url or hosted_url,
            "upi_uri": upi_uri,
            "hosted_url": hosted_url,
            "instructions_url": redirect_url if _upi_is_instructions_url(redirect_url) else "",
            "qr_data": qr_data_str,
            "qr_path": written_qr_path,
            "qr_image_url_png": qr_data.get("qr_image_url_png", ""),
            "qr_image_url_svg": qr_data.get("qr_image_url_svg", ""),
            "expires_at": expires_at,
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "amount": amount,
            "currency": payment_currency.upper(),
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "payment_country": payment_country,
            "payment_method_types": pm_types,
            "coupon_name": ft_status["coupon_name"],
            "approval_ok": approval_ok,
            "approval_blocked": approval_blocked,
            "paid": bool(paid_state.get("paid")),
            "payment_status": str(paid_state.get("payment_status") or ""),
            "checkout_ui_mode": checkout_ui_mode,
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "approve_proxy": approve_proxy,
            "verification": verification,
            "verification_code": verification_code,
            "link_verified": link_verified,
        }
        if verify_target and not link_verified and not verification_inconclusive:
            # 明确判定为不可交付 —— 但原因分三种，不能一律叫「废链」；
            # 映射与理由归 ``_upi_link_unverified_contract``。
            link_result["ok"] = False
            link_result.update(_upi_link_unverified_contract(verification_code, verification))
        return link_result
    except Exception as e:
        # 把失败原因压成可判读的 error_code。历史实现一律返回
        # "upi_qr_failed"，调用方只能读 error 字符串做子串匹配——
        # 而 error_classification 又不认识 generic_decline / approve blocked
        # 这些 UPI 专有说法（实测都落到 "unknown"）。
        # 这里就地给出稳定代码，让上层不必解析自然语言。
        return {
            "ok": False,
            "error": str(e),
            "error_code": _upi_classify_failure(e),
            "payment_method": "upi",
            "url": "",
            "qr_path": "",
        }


def generate_upi_qr_link(
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
    """Generate a UPI payment link (public entrypoint).

    Increment 3 (2026-10-01): the single-attempt body moved to
    :func:`_generate_upi_qr_link_once`; this wrapper reopens the **whole**
    Checkout on a fresh exit when an attempt failed *before* any Stripe confirm
    side effect (``no_free_trial`` / ``upi_not_available`` / checkout-create /
    transport).  The reference measured that a different exit can turn a
    ``nonzero_due`` round into a linked one, and that retrying is only safe
    while the account's zero-eligibility has not been consumed.

    ``rounds`` comes from ``upi.rounds`` / ``UPI_ROUNDS`` (default 1 = previous
    behaviour).  Post-confirm verdicts (``mandate_not_signed`` /
    ``payment_chain_link`` / ``link_unverified`` / ``upi_provider_declined``)
    are never retried here -- the account's approval is one-shot.

    The public signature, ``__module__`` and ``UPI_CALL_OPTIONS`` parity are
    pinned by ``tests/test_upi_link_entrypoint_unique.py``.
    """
    total_rounds = _upi_rounds(runtime_config)
    country = str(checkout_country or target_country or "").strip().upper()
    current = (proxy, checkout_proxy, provider_proxy, approve_proxy)
    result: dict[str, Any] | None = None
    attempt = 0
    while attempt < total_rounds:
        attempt += 1
        if attempt > 1:
            current = _upi_rotate_proxy_set(current, country)
            _emit(
                "round",
                f"round {attempt}/{total_rounds}: fresh exit "
                f"{redact_proxy_text(current[1] or current[0] or 'DIRECT', current[1] or current[0])}",
            )
        result = _generate_upi_qr_link_once(
            access_token=access_token,
            proxy=current[0],
            auth_context=auth_context,
            checkout_proxy=current[1],
            provider_proxy=current[2],
            approve_proxy=current[3],
            target_country=target_country,
            checkout_country=checkout_country,
            payment_country=payment_country,
            require_zero=require_zero,
            qr_path=qr_path,
            runtime_config=runtime_config,
            proxy_state=proxy_state,
            device_id=device_id,
            session_token=session_token,
            wait_paid=wait_paid,
            paid_timeout=paid_timeout,
            require_server_upi_mandate=require_server_upi_mandate,
        )
        if result.get("ok") or not _upi_round_retryable(result):
            break
        if attempt >= total_rounds:
            break
        _emit(
            "round",
            f"round {attempt}/{total_rounds} failed pre-confirm "
            f"({result.get('error_code') or 'unknown'}); reopening on a fresh exit",
        )
    return result if result is not None else {"ok": False, "error_code": "upi_qr_failed"}

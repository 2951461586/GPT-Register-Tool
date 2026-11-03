from __future__ import annotations

import contextlib
from ..checkout_contract import PLUS_TRIAL_CAMPAIGN_ID
from ..payment.wire import CURRENCY_MAP, _new_session
from ..phone_proxy import redact_proxy_text
from ..pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_STRIPE_PK, DEFAULT_TIMEOUT, STRIPE_VERSION
from ..payment import egress as payment_egress
from ..geo.resolver import resolve_proxy_geo
from ..proxy.edge_probe import CHATGPT_CHECKOUT_PATH, probe_openai_edge
from ..proxy.entry import rotate_session
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
from ..payment.upi_identity import AccountIdentity as IdentityLike
from ..payment.upi_identity import resolve_account_identity
from ..payment.upi_identity_store import account_key as _identity_account_key
from ..payment.upi_fingerprint import (
    cooldown_status as _fingerprint_cooldown,
    record_outcome as _record_fingerprint_outcome,
)
from ..payment import upi_exit_health as exit_health
from ..payment.upi_zero import clear_zero_ineligible as _clear_zero_ineligible
from ..payment.upi_zero import mark_zero_ineligible as _mark_zero_ineligible
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


from . import india_exit
from .india_exit import (
    ExitOps,
    _UPI_ADMISSION_PATHS,
    _upi_india_exit_attempts,
    _upi_india_exit_probe_enabled,
    _upi_india_exit_timeout,
)
from .operations import UpiOperations, build_upi_operations
from .stage_config import (
    _UPI_PRECONFIRM_RETRY_CODES,
    _upi_link_unverified_contract,
    _upi_repeat_tax_region,
    _upi_round_retryable,
    _upi_rounds,
)

from . import stages as _stages


def _upi_resolve_identity(
    *,
    access_token: Any,
    auth_context: Any,
    device_id: Any,
    session_token: Any,
    payment_country: str,
    fingerprint_index: Any,
    upi_cfg: Any,
    cfg: Any,
    rotate_fingerprint: bool,
) -> SimpleNamespace:
    """Account identity + browser fingerprint + billing identity for one run.

    Split out of :func:`_resolve_upi_runtime` (P0-1/P0-2/P0-3): that function had
    grown past its frozen length, and this block is a self-contained policy --
    *who* is running -- as opposed to the switches that decide *how*.
    """
    # 账号身份：``auth_context`` 从 UPI_CALL_OPTIONS 第一天就声明、每个入口也都
    # 传下来了，但历史上没有任何 stage 读过它 —— 于是每轮都拿 ``uuid4()`` 当设备
    # ID，并用合成的 ``Cookie: oai-did=<uuid>`` 覆盖掉账号真实会话 Cookie。
    # 见 ``identity.py`` 与 ``payment/qualification.py:_device_id_and_cookie`` 记录的
    # 2026-10-27 实测：只带 ``oai-did`` 而没有 AT 签发时的会话 Cookie，Checkout 回
    # ``400 Our systems have detected unusual activity``。
    identity = resolve_account_identity(
        auth_context,
        device_id=device_id,
        session_token=session_token,
    )
    # 设备身份：调用方可传账号真实 device_id（session 文件里有），
    # 不传则生成一个自洽的 UUID——风控看的是头的存在性与一致性。
    device_id = identity.device_id or str(uuid.uuid4())
    session_token = str(identity.session_token or "").strip()
    cookie_header = identity.cookie_header
    account_id = identity.account_id

    # 一套自洽的浏览器身份贯穿全流程（旧实现每个 session 各随机一个 UA ⇒ 指纹自相矛盾）
    # locale/timezone 从契约层按 payment_country 取，不在这里硬编码。
    #
    # P0-3：指纹改为**按账号粘性**（落 ``runtime/upi_identity.json``），只在重试
    # （``rotate_fingerprint``）时推进到下一个模板。旧实现无论什么情况都是
    # ``secrets.randbelow``，于是同一账号重试一次就换 UA/sec-ch-ua/时区，而
    # device id 与 Sentinel 对不变 —— 「固定设备 ID 但 UA 变了」是最廉价的矛盾特征。
    # 配置里显式写了 ``upi.fingerprint`` 时仍然以它为准（运维可钉死单个模板）。
    account_key = _upi_account_key(access_token, auth_context, identity=identity)
    fingerprint_index = upi_cfg.get("fingerprint")
    try:
        fingerprint = _upi_fingerprint(
            int(fingerprint_index) if fingerprint_index is not None else None,
            payment_country,
            account_key=account_key,
            rotate=bool(rotate_fingerprint),
            cfg=cfg,
        )
    except (TypeError, ValueError):
        fingerprint = _upi_fingerprint(
            country=payment_country,
            account_key=account_key,
            rotate=bool(rotate_fingerprint),
            cfg=cfg,
        )
    # 账单同理：按账号粘性 + 全局去重（旧实现 5 人名 × 4 地址 = 20 种组合，
    # 4 并发批次必然撞地址，且同一账号重试一次就换姓名与地址）。
    billing = _upi_billing_profile(
        upi_cfg if "fixed_billing" in upi_cfg else None,
        account_key=account_key,
    )
    return SimpleNamespace(
        device_id=device_id,
        session_token=session_token,
        cookie_header=cookie_header,
        account_id=account_id,
        account_key=account_key,
        fingerprint_index=fingerprint_index,
        fingerprint=fingerprint,
        billing=billing,
    )


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
    auth_context=None,
    rotate_fingerprint=False,
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
    _raw_regions: object = upi_cfg.get("billing_regions")
    regions: list = _raw_regions if isinstance(_raw_regions, list) else []
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
    # P1-2：approve 分轮。总尝试数不变（``max_approve_attempts``），轮数只决定
    # 这些尝试被切成几个**新 session**（新 Sentinel）。默认 1 轮 = 改动前行为。
    approve_cycles = _env_int("UPI_APPROVE_CYCLES", 1, minimum=1)
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

    _ident = _upi_resolve_identity(
        access_token=access_token,
        auth_context=auth_context,
        device_id=device_id,
        session_token=session_token,
        payment_country=payment_country,
        fingerprint_index=upi_cfg.get("fingerprint"),
        upi_cfg=upi_cfg,
        cfg=cfg,
        rotate_fingerprint=bool(rotate_fingerprint),
    )
    # UPI 建单/记账/审批都必须走账单国出口；已知 region/geo/country 模板重定向
    # 到该国家，不动粘性会话。未知格式原样保留（真实出口门禁由上层负责）。
    checkout_proxy = _upi_retarget_region(checkout_proxy, checkout_country)
    provider_proxy = _upi_retarget_region(provider_proxy, checkout_country)
    approve_proxy = _upi_retarget_region(approve_proxy, checkout_country)
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
        approve_cycles=approve_cycles,
        poll_max_attempts=poll_max_attempts,
        approve_backoff_cap=approve_backoff_cap,
        local_mandate_enabled=local_mandate_enabled,
        browser_rail=browser_rail,
        fingerprint_index=_ident.fingerprint_index,
        fingerprint=_ident.fingerprint,
        billing=_ident.billing,
        device_id=_ident.device_id,
        session_token=_ident.session_token,
        cookie_header=_ident.cookie_header,
        account_id=_ident.account_id,
        account_key=_ident.account_key,
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


def _upi_account_key(
    access_token: str,
    auth_context: Mapping[str, Any] | None = None,
    *,
    identity: Any = None,
) -> str:
    """Stable per-account key for the identity ledger (``""`` when unknowable).

    One implementation shared by ``_resolve_upi_runtime`` (allocation) and
    ``generate_upi_qr_link`` (outcome recording), so a run can never allocate an
    identity under one key and record its verdict under another.  The caller may
    pass its already-resolved ``identity`` to avoid resolving it twice; both
    paths produce the same key from the same inputs.
    """
    resolved = identity if isinstance(identity, IdentityLike) else resolve_account_identity(auth_context)
    return _identity_account_key(
        account_id=resolved.account_id,
        access_token=access_token,
        session_token=resolved.session_token,
    )


def _upi_record_attempt_outcome(
    account_key: str,
    result: Any,
    runtime_config: Any = None,
    checkout_proxy: Any = "",
) -> None:
    """Record this attempt's verdict on the account **and** on the exit.

    Best-effort telemetry: a failure here must never change the extraction
    result, so every path suppresses.

    * account → ``fingerprint_identity`` (30-minute cooldown on ``blocked``);
    * exit → ``exit_health`` (the reference's ``record_exit_risk_block``).
      A ``blocked`` verdict is a property of the exit; before P1-3 the next
      account handed the same exit started from zero and re-learned it.
    """
    if not isinstance(result, Mapping):
        return
    cfg = runtime_config if isinstance(runtime_config, Mapping) else None
    code = str(result.get("error_code") or "")
    text = f"{code} {result.get('error') or ''}".lower()
    ok = bool(result.get("ok"))
    blocked = (not ok) and ("blocked" in text or code == "upi_provider_declined")
    reason = code or str(result.get("error") or "")
    if account_key:
        with contextlib.suppress(Exception):
            _record_fingerprint_outcome(account_key, success=ok, blocked=blocked, reason=reason, cfg=cfg)
    # P2：0 元资格负缓存。``no_free_trial``（require_zero 下实付非 0）是**账号级**
    # 结论，记一天；成功出链则把标记清掉。失败一律吞（纯调度优化）。
    require_zero = bool(result.get("require_zero", True))
    if account_key and require_zero and code == "no_free_trial":
        with contextlib.suppress(Exception):
            _mark_zero_ineligible(account_key, cfg=cfg)
    elif account_key and ok:
        with contextlib.suppress(Exception):
            _clear_zero_ineligible(account_key, cfg=cfg)
    exit_text = str(checkout_proxy or "").strip()
    if exit_text:
        with contextlib.suppress(Exception):
            if blocked:
                exit_health.record_block(exit_text, reason, cfg=cfg)
            elif ok:
                exit_health.clear(exit_text, cfg=cfg)


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


# ── Split-out dependency seams (2026-10-03) ────────────────────────────────
# The stage body lives in `stages.py`; everything it resolves is read from THIS
# module's namespace at call time, so `monkeypatch.setattr(pipeline, "<name>")`
# and `patch("sms_tool.upi_link.pipeline.<name>")` keep working.  Design and the
# per-test migration list: docs/audits/plans/plan-2026-10-03-upi-pipeline-stage-split.md


def _exit_ops() -> ExitOps:
    """Exit-selection dependencies, read at call time so patches still land."""
    return ExitOps(
        resolve_proxy_geo=resolve_proxy_geo,
        probe_openai_edge=probe_openai_edge,
        rotate_session=rotate_session,
    )


def _upi_exit_country(proxy: str, timeout: float) -> str:
    return india_exit._upi_exit_country(proxy, timeout, _exit_ops())


def _upi_exit_admits_checkout(proxy: str, timeout: float) -> bool:
    return india_exit._upi_exit_admits_checkout(proxy, timeout, _exit_ops())


def _upi_rotate_region_session(proxy: str, country: str) -> str:
    return india_exit._upi_rotate_region_session(proxy, country, _exit_ops())


def _upi_select_india_exit(
    proxy: Any,
    *,
    country: str,
    attempts: int,
    timeout: float,
    emit: Any = None,
) -> str:
    return india_exit._upi_select_india_exit(
        proxy, country=country, attempts=attempts, timeout=timeout, emit=emit, ops=_exit_ops()
    )


def _upi_rotate_proxy_set(values: tuple[Any, ...], country: str) -> tuple[Any, ...]:
    return india_exit._upi_rotate_proxy_set(values, country, _exit_ops())


def _build_upi_operations() -> UpiOperations:
    """Read every stage dependency from this module's globals, now."""
    return build_upi_operations(globals())


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
    rotate_fingerprint: bool = False,
) -> dict[str, Any]:
    """Thin seam: assemble the dependency bundle and run the stage body.

    Stays in this module (single owner, unchanged signature) so the historical
    ``patch("sms_tool.upi_link.pipeline._generate_upi_qr_link_once")`` targets in
    ``tests/payment/test_upi_rounds.py`` / ``test_upi_tax_repeat.py`` still intercept.
    """
    return _stages.run_upi_qr_link_once(
        _build_upi_operations(),
        access_token,
        proxy,
        auth_context,
        checkout_proxy,
        provider_proxy,
        approve_proxy,
        target_country,
        checkout_country,
        payment_country,
        require_zero,
        qr_path,
        runtime_config,
        proxy_state,
        device_id,
        session_token,
        wait_paid,
        paid_timeout,
        require_server_upi_mandate,
        rotate_fingerprint,
    )


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
    pinned by ``tests/payment/test_upi_link_entrypoint_unique.py``.
    """
    total_rounds = _upi_rounds(runtime_config)
    country = str(checkout_country or target_country or "").strip().upper()
    current = (proxy, checkout_proxy, provider_proxy, approve_proxy)
    result: dict[str, Any] | None = None
    attempt = 0
    # 账号身份：与 ``_resolve_upi_runtime`` 共用 ``_upi_account_key``，保证分配与
    # 记账落在同一个键上。``""``（无可识别账号）时不读写 ledger。
    account_key = _upi_account_key(access_token, auth_context)
    _ledger_cfg = runtime_config if isinstance(runtime_config, Mapping) else None
    cooling, cooling_left = _fingerprint_cooldown(account_key, _ledger_cfg) if account_key else (False, 0.0)
    if cooling:
        _emit(
            "identity",
            f"fingerprint in cooldown ({max(0.0, cooling_left):.0f}s left); rotating to the next template",
        )
    while attempt < total_rounds:
        attempt += 1
        if attempt > 1:
            current = _upi_rotate_proxy_set(current, country)
            _emit(
                "round",
                f"round {attempt}/{total_rounds}: fresh exit "
                f"{redact_proxy_text(current[1] or current[0] or 'DIRECT', current[1] or current[0])}",
            )
        # ── P1-3：出口风控记忆 ──────────────────────────────────────
        # 上一次在这个出口上被判 blocked 之后仍在冷却窗口内。这里只**告知**；
        # 除非运维显式打开 ``UPI_EXIT_HEALTH_SKIP``（调用方拥有出口池时才有意义），
        # 否则不据此改变控制流 —— 本链路拿到的是单个凭据，无权替调用方换池。
        _exit = str(current[1] or current[0] or "").strip()
        _exit_reason = exit_health.block_reason(_exit, cfg=_ledger_cfg) if _exit else ""
        if _exit_reason:
            _emit("exit", f"exit still cooling from an earlier block: {_exit_reason}")
            if exit_health.skip_enabled(_ledger_cfg):
                result = {
                    "ok": False,
                    "error": f"exit_health_blocked: {_exit_reason}",
                    "error_code": "exit_health_blocked",
                    "payment_method": "upi",
                }
                _upi_record_attempt_outcome(account_key, result, _ledger_cfg, _exit)
                if attempt >= total_rounds:
                    return result
                continue
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
            # 重试（或指纹正处冷却）才换模板；首轮必须是粘性的那一套。
            rotate_fingerprint=bool(attempt > 1 or cooling),
        )
        # P1-3：blocked 是**出口**的属性 —— 记下来，下一个用同一出口的账号
        # 就不必再花一次 Checkout + Sentinel + 5 次 approve 重新学一遍。
        _upi_record_attempt_outcome(account_key, result, _ledger_cfg, current[1] or current[0])
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

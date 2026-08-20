#!/usr/bin/env python3
r"""PP 直链生成器 -- 分段代理池版。

参考 F:\epsoft\app\app.py 的三段式代理路由:
  Stage 1: checkout (JP/TH 代理) → 创建 ChatGPT checkout session
  Stage 2: provider (目标国代理) → Stripe init + create PM + confirm
  Stage 3: approve  (目标国代理) → ChatGPT approve + 轮询 redirect → 提取 BA 链

用法:
  # 分段代理模式 (checkout→JP, provider/approve→GB)
  python pp_link_v2.py <token> --checkout-proxy "****************************** --provider-proxy "****************************** --target GB

  # 代理模板批量模式 (自动替换国家码)
  python pp_link_v2.py <token> --proxy-template "user:pass-XX@gate:1000" --batch --target-countries AU,GB,DE

  # 单代理模式 (所有阶段用同一代理)
  python pp_link_v2.py <token> --proxy "***************************

配置说明:
  --checkout-proxy   Stage 1 代理 (默认 JP 出口)
  --provider-proxy   Stage 2 代理 (目标国出口)
  --approve-proxy    Stage 3 代理 (目标国出口，默认同 provider)
  --proxy            单代理模式，所有阶段用同一代理
  --proxy-template   代理模板，配合 --batch 使用
  --target           目标国家 (默认 DE)
  --batch            批量矩阵模式
  --no-require-zero  允许非零金额 (默认要求 0 元)

模块拆分 (纯重构, 零行为变化):
  paypal_extract.py   PPLinkExtractor 提取核心 + _checkout_post/_new_session 传输助手
  upi_link.py         UPI QR 生成子系统 (generate_upi_qr_link + _upi_* 助手)
  paypal_proxy.py     阶段代理配置解析 (_proxies_from_config 等, 与 PayPalProxyState 同驻)
本模块保留全部公共入口与现有名字的兼容 re-export。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .checkout_contract import CheckoutRequestContract, CheckoutSessionContract
    from .phone_proxy import normalize_proxy_url
    from .pp_link_helpers import (
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
        RETRY_ATTEMPTS,
        _SIDE_EFFECT_STAGES,
        normalize_proxy_template,
        proxy_for_country_template,
        rotate_proxy_session,
        find_submission_attempt,
        stripe_confirm_error_diagnostics,
        is_paypal_ba_approve_url,
        extract_ba_token,
        find_url_in_value,
        extract_redirect_url,
        resolve_external_redirect,
        billing_for_country,
        stripe_amount_details,
        BILLING_DATA,
        PM_REDIRECT_RE,
        PAYPAL_BA_RE,
    )
except ImportError:  # pragma: no cover - direct script execution
    from checkout_contract import CheckoutRequestContract, CheckoutSessionContract  # type: ignore
    from phone_proxy import normalize_proxy_url  # type: ignore
    from pp_link_helpers import (  # type: ignore
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
        RETRY_ATTEMPTS,
        _SIDE_EFFECT_STAGES,
        normalize_proxy_template,
        proxy_for_country_template,
        rotate_proxy_session,
        find_submission_attempt,
        stripe_confirm_error_diagnostics,
        is_paypal_ba_approve_url,
        extract_ba_token,
        find_url_in_value,
        extract_redirect_url,
        resolve_external_redirect,
        billing_for_country,
        stripe_amount_details,
        BILLING_DATA,
        PM_REDIRECT_RE,
        PAYPAL_BA_RE,
    )

try:
    from .paypal_proxy import (
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
        _PAYPAL_PROXY_STATE_CACHE,
        _proxy_health_cfg,
        _paypal_proxy_state,
        _proxy_pool_values,
        _rank_stage_proxy,
        _proxies_from_config,
        _stage_proxy_is_configured,
        _resolve_stage_proxy,
    )
except ImportError:  # pragma: no cover - direct script execution
    from paypal_proxy import (  # type: ignore
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
        _PAYPAL_PROXY_STATE_CACHE,
        _proxy_health_cfg,
        _paypal_proxy_state,
        _proxy_pool_values,
        _rank_stage_proxy,
        _proxies_from_config,
        _stage_proxy_is_configured,
        _resolve_stage_proxy,
    )


def _paypal_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return PayPal config with the canonical protocol section applied.

    The legacy top-level ``paypal`` block is still accepted, but it must not
    reintroduce stale stage-country values when a canonical protocol method
    configuration is present.
    """
    source = config if isinstance(config, Mapping) else {}
    try:
        from .payment_routing import method_payment_config

        return method_payment_config(source, "paypal")
    except (ImportError, TypeError, AttributeError):  # pragma: no cover - direct script execution
        value = source.get("paypal")
        return dict(value) if isinstance(value, Mapping) else {}

try:
    from .paypal_extract import (
        CURRENCY_MAP,
        CheckoutNotZeroDueError,
        PPLinkExtractor,
        PaymentOutcomeUnknownError,
        _checkout_post,
        _compact_diagnostic,
        _new_session,
    )
except ImportError:  # pragma: no cover - direct script execution
    from paypal_extract import (  # type: ignore
        CURRENCY_MAP,
        CheckoutNotZeroDueError,
        PPLinkExtractor,
        PaymentOutcomeUnknownError,
        _checkout_post,
        _compact_diagnostic,
        _new_session,
    )

try:
    from .upi_link import (
        UPI_CHECKOUT_URL,
        UPI_CHECKOUT_CONFIRM_URL,
        UPI_CHECKOUT_APPROVE_URL,
        STRIPE_PAYMENT_PAGE_INIT_URL_T,
        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
        STRIPE_PAYMENT_PAGE_GET_URL_T,
        UPI_APPROVAL_MAX_ATTEMPTS,
        UPI_QR_POLL_MAX_ATTEMPTS,
        UPI_QR_POLL_INTERVAL,
        UPI_BILLING_IN,
        _default_qr_path,
        _write_qr_png,
        _upi_nested_get,
        _upi_amount_minor,
        _upi_extract_payment_amount,
        _upi_get_payment_method_types,
        _upi_scan_free_trial,
        _upi_get_free_trial_status,
        _upi_merge_qr_key,
        _upi_extract_next_action,
        _upi_extract_qr_from_html,
        _upi_hydrate_qr_data,
        _method_cfg,
        _payment_stage_proxies_from_config,
        generate_upi_qr_link,
    )
except ImportError:  # pragma: no cover - direct script execution
    from upi_link import (  # type: ignore
        UPI_CHECKOUT_URL,
        UPI_CHECKOUT_CONFIRM_URL,
        UPI_CHECKOUT_APPROVE_URL,
        STRIPE_PAYMENT_PAGE_INIT_URL_T,
        STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
        STRIPE_PAYMENT_PAGE_GET_URL_T,
        UPI_APPROVAL_MAX_ATTEMPTS,
        UPI_QR_POLL_MAX_ATTEMPTS,
        UPI_QR_POLL_INTERVAL,
        UPI_BILLING_IN,
        _default_qr_path,
        _write_qr_png,
        _upi_nested_get,
        _upi_amount_minor,
        _upi_extract_payment_amount,
        _upi_get_payment_method_types,
        _upi_scan_free_trial,
        _upi_get_free_trial_status,
        _upi_merge_qr_key,
        _upi_extract_next_action,
        _upi_extract_qr_from_html,
        _upi_hydrate_qr_data,
        _method_cfg,
        _payment_stage_proxies_from_config,
        generate_upi_qr_link,
    )

try:
    from .sanitizer import sanitize_text
except ImportError:  # pragma: no cover - direct script execution
    from sanitizer import sanitize_text  # type: ignore

# ─── 输出 ────────────────────────────────────────────────────────────────────


def _emit(step: str, msg: str, **kw: Any) -> None:
    """Top-level progress/error sink used by every pipeline entry point.

    Centralised so all five ``def emit(...)`` closures (one per pipeline
    function) share identical semantics and a single place to extend with
    structured logging/progress reporting.
    """
    print(f"[{step}] {msg}", file=sys.stderr)


# ─── 常量 ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULT_TARGET_COUNTRIES = ("AU", "TH", "US", "GB", "DE", "JP", "SG", "NZ", "CA", "IE")
DEFAULT_CHECKOUT_COUNTRIES = ("JP", "TH")


# ─── 兼容函数 (供 CLI 和旧集成调用) ──────────────────────────────────────────


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_token(raw: str) -> str | None:
    """解析 access token，支持 JWT 格式。"""
    token = str(raw or "").strip()
    if not token:
        return None
    # JWT 格式: header.payload.signature
    parts = token.split(".")
    if len(parts) == 3 and all(parts):
        return token
    return None


# ─── 批量矩阵 ──────────────────────────────────────────────────────────────────


def run_batch(
    access_token: str,
    proxy_template: str,
    target_countries: list[str] | None = None,
    checkout_countries: list[str] | None = None,
    require_zero: bool = True,
    emit: Any = None,
    promotion_country: str = "",
    promotion_countries: list[str] | None = None,
) -> dict:
    """批量矩阵提链: 成功即停。

    两种矩阵模式:

    - 默认 (target × checkout): 沿用旧行为, 遍历 target 国 × checkout 出口国。
      ``promotion_country`` 非空时对每个组合启用促销更新 (/checkout/update)。
    - 促销矩阵 (paypal_region × promotion_region): 当 ``promotion_countries``
      非空时启用。对齐参考实现的 zero-amount matrix: checkout/provider/approve
      都走 PayPal 支持区 (target), promotion 走促销可用区, 目标是同一 checkout
      同时拿到 0元 + PayPal BA 直链。返回结果附带 ``matrix`` 明细。
    """
    log = emit or (lambda step, msg, **kw: print(f"[{step}] {msg}", file=sys.stderr))
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = _paypal_config(cfg)
    state = _paypal_proxy_state(paypal_cfg)
    preflight = bool(paypal_cfg.get("preflight_proxy_check", False))
    rotate_sessions = bool(paypal_cfg.get("rotate_proxy_sessions", False))
    probe_timeout = float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12)
    max_stage_retries = int(paypal_cfg.get("max_stage_retries", RETRY_ATTEMPTS) or RETRY_ATTEMPTS)

    # ── 促销矩阵模式: paypal_region × promotion_region ──────────────────────
    if promotion_countries:
        paypal_regions = target_countries or list(DEFAULT_TARGET_COUNTRIES)
        promo_regions = [c for c in promotion_countries if c]
        combos = [(pp, promo) for pp in paypal_regions for promo in promo_regions]
        combos.sort(
            key=lambda item: state.pair_score(
                proxy_for_country_template(proxy_template, item[0]),
                proxy_for_country_template(proxy_template, item[0]),
            ),
            reverse=True,
        )
        log("batch", f"促销矩阵: {len(combos)} 个组合 (PayPal 区 × promotion 区), 0元+BA 成功即停")
        matrix: list[dict[str, Any]] = []
        for index, (pp_region, promo_region) in enumerate(combos, 1):
            label = f"{pp_region}<-promo:{promo_region}"
            log("batch", f"任务 {index}/{len(combos)}: paypal={pp_region} promotion={promo_region}")
            region_proxy = proxy_for_country_template(proxy_template, pp_region)
            promotion_proxy = proxy_for_country_template(proxy_template, promo_region)
            row: dict[str, Any] = {
                "paypal_region": pp_region, "promotion_region": promo_region,
                "amount": None, "link_type": "", "status": "failed", "error": "",
            }
            try:
                extractor = PPLinkExtractor(
                    access_token=access_token,
                    checkout_proxy=region_proxy,
                    provider_proxy=region_proxy,
                    stripe_init_proxy=region_proxy,
                    payment_method_proxy=region_proxy,
                    confirm_proxy=region_proxy,
                    approve_proxy=region_proxy,
                    promotion_proxy=promotion_proxy,
                    target_country=pp_region,
                    checkout_country=pp_region,
                    require_zero=require_zero,
                    emit=log,
                    preflight_proxy_check=preflight,
                    rotate_proxy_sessions=rotate_sessions,
                    proxy_probe_timeout=probe_timeout,
                    max_stage_retries=max_stage_retries,
                    proxy_state=state,
                    stage_proxy_countries={
                        "checkout": pp_region,
                        "promotion": promo_region,
                        "provider": pp_region,
                        "stripe_init": pp_region,
                        "payment_method": pp_region,
                        "confirm": pp_region,
                        "approve": pp_region,
                    },
                )
                result = extractor.extract()
                row["amount"] = result.get("amount")
                row["link_type"] = result.get("link_type", "")
                is_zero = result.get("amount") == 0
                is_ba = "paypal_ba" in str(result.get("link_type") or "")
                if result.get("ok") and is_zero and is_ba:
                    row["status"] = "success"
                    matrix.append(row)
                    log("batch", f"任务 {label} 成功! 0元+BA url={str(result.get('url'))[:80]}...")
                    return {"ok": True, "tasks_attempted": index, "tasks_total": len(combos),
                            "winning_combo": label, "matrix": matrix, **result}
                row["status"] = "partial" if result.get("ok") else "failed"
                log("batch", f"任务 {label}: amount={row['amount']} link_type={row['link_type']} (未同时满足 0元+BA)")
            except Exception as e:
                row["error"] = str(e)
                log("batch", f"任务 {label} 失败: {e}")
            matrix.append(row)
        return {"ok": False, "error": f"所有 {len(combos)} 个促销矩阵组合均未同时满足 0元+BA",
                "tasks_attempted": len(combos), "matrix": matrix}

    # ── 默认模式: target × checkout ─────────────────────────────────────────
    targets = target_countries or list(DEFAULT_TARGET_COUNTRIES)
    checkouts = checkout_countries or list(DEFAULT_CHECKOUT_COUNTRIES)

    tasks = [(t, c) for t in targets for c in checkouts]
    tasks.sort(
        key=lambda item: (
            state.pair_score(
                proxy_for_country_template(proxy_template, item[1]),
                proxy_for_country_template(proxy_template, item[0]),
            ),
            1 if state.zero_status(proxy_for_country_template(proxy_template, item[1]), item[1])[0] == "ok" else 0,
        ),
        reverse=True,
    )
    log("batch", f"批量任务: {len(tasks)} 个组合, 提取到第一个 BA 链后停止")

    for index, (target, checkout) in enumerate(tasks, 1):
        task_label = f"{target}-{checkout}"
        log("batch", f"任务 {index}/{len(tasks)}: target={target} checkout_proxy={checkout}")

        checkout_proxy = proxy_for_country_template(proxy_template, checkout)
        target_proxy = proxy_for_country_template(proxy_template, target)
        promotion_proxy = (
            proxy_for_country_template(proxy_template, promotion_country)
            if promotion_country else ""
        )

        try:
            extractor = PPLinkExtractor(
                access_token=access_token,
                checkout_proxy=checkout_proxy,
                provider_proxy=target_proxy,
                stripe_init_proxy=target_proxy,
                payment_method_proxy=target_proxy,
                confirm_proxy=target_proxy,
                approve_proxy=target_proxy,
                promotion_proxy=promotion_proxy,
                target_country=target,
                checkout_country=checkout,
                require_zero=require_zero,
                emit=log,
                preflight_proxy_check=preflight,
                rotate_proxy_sessions=rotate_sessions,
                proxy_probe_timeout=probe_timeout,
                max_stage_retries=max_stage_retries,
                proxy_state=state,
                stage_proxy_countries={
                    "checkout": checkout,
                    "promotion": promotion_country,
                    "provider": target,
                    "stripe_init": target,
                    "payment_method": target,
                    "confirm": target,
                    "approve": target,
                },
            )
            result = extractor.extract()
            log("batch", f"任务 {task_label} 成功! url={result['url'][:80]}...")
            return {"ok": True, "tasks_attempted": index, "tasks_total": len(tasks), "winning_combo": task_label, **result}
        except Exception as e:
            log("batch", f"任务 {task_label} 失败: {e}")
            continue

    return {"ok": False, "error": f"所有 {len(tasks)} 个组合均失败", "tasks_attempted": len(tasks)}


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PP 直链生成器 -- 分段代理池版")
    parser.add_argument("token", nargs="?", help="OpenAI Access Token")
    parser.add_argument("--token", dest="token_flag", help="Access Token (alternative)")
    parser.add_argument("--proxy", default="", help="单代理模式 (所有阶段)")
    parser.add_argument("--checkout-proxy", default="", help="Checkout 阶段代理 (JP)")
    parser.add_argument("--provider-proxy", default="", help="Provider/Stripe 阶段代理 (目标国)")
    parser.add_argument("--approve-proxy", default="", help="Approve 阶段代理 (目标国)")
    parser.add_argument("--promotion-proxy", default="", help="促销更新阶段代理 (促销可用区出口, 如 VN/TH; 用于 /checkout/update 打 0元)")
    parser.add_argument("--promotion-country", default="", help="批量模式促销更新出口国家 (如 VN/TH)")
    parser.add_argument("--promotion-countries", default="", help="促销矩阵模式: promotion 出口国列表 (逗号分隔, 如 JP,TH,VN)。设置后 run_batch 走 PayPal区×promotion区 组合搜索")
    parser.add_argument("--proxy-template", default="", help="代理模板 (自动替换国家码)")
    parser.add_argument("--target", default="DE", help="目标国家 (单次模式)")
    parser.add_argument("--checkout-country", default="", help="Checkout 阶段账单国家 (默认同 target, 如 JP/TR)")
    parser.add_argument("--batch", action="store_true", help="批量矩阵模式")
    parser.add_argument("--target-countries", default="", help="批量模式目标国家 (逗号分隔)")
    parser.add_argument("--checkout-countries", default="JP,TH", help="批量模式 checkout 出口 (逗号分隔)")
    parser.add_argument("--no-require-zero", action="store_true", help="不要求 0 元金额")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args()
    token = args.token or args.token_flag
    if not token:
        parser.error("请提供 Access Token")

    emit = _emit

    require_zero = not args.no_require_zero

    if args.batch or args.proxy_template:
        # 批量模式
        template = args.proxy_template or args.proxy
        if not template:
            parser.error("批量模式需要 --proxy-template")
        targets = [c.strip().upper() for c in args.target_countries.split(",") if c.strip()] if args.target_countries else list(DEFAULT_TARGET_COUNTRIES)
        checkouts = [c.strip().upper() for c in args.checkout_countries.split(",") if c.strip()]
        promotion_countries = [c.strip().upper() for c in args.promotion_countries.split(",") if c.strip()]
        result = run_batch(token, template, targets, checkouts, require_zero=require_zero, emit=emit,
                           promotion_country=(args.promotion_country or "").strip().upper(),
                           promotion_countries=promotion_countries or None)
    else:
        # 单次模式
        checkout_proxy = args.checkout_proxy or args.proxy
        provider_proxy = args.provider_proxy or args.proxy
        approve_proxy = args.approve_proxy or args.proxy
        promotion_proxy = args.promotion_proxy or ""
        if not checkout_proxy and not provider_proxy:
            parser.error("请提供代理 (--proxy 或 --checkout-proxy + --provider-proxy)")
        extractor = PPLinkExtractor(
            access_token=token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            approve_proxy=approve_proxy,
            promotion_proxy=promotion_proxy,
            target_country=args.target,
            checkout_country=args.checkout_country,
            require_zero=require_zero,
            emit=emit,
        )
        result = extractor.extract()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            print(f"\n✅ PP 直链提取成功!")
            print(f"   URL: {sanitize_text(result['url'])}")
            if result.get("ba_token"):
                print("   BA Token: [REDACTED]")
            print(f"   cs_id: {result['cs_id']}")
            print(f"   金额: {result.get('amount')} {result.get('currency')}")
            print(f"   目标国: {result.get('target_country')}")
            print(f"   链接类型: {result.get('link_type')}")
        else:
            print(f"\n❌ 提取失败: {result.get('error')}")
            sys.exit(1)


def generate_pp_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    stripe_init_proxy: str | None = None,
    payment_method_proxy: str | None = None,
    confirm_proxy: str | None = None,
    approve_proxy: str | None = None,
    promotion_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    require_zero: bool | None = None,
    require_ba_token: bool | None = None,
    stage_proxy_countries: dict[str, str] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 PayPal BA 直链 (兼容旧接口)。

    Args:
        access_token: OpenAI access token (JWT)
        proxy: 单代理 URL (所有阶段)
        auth_context: 认证上下文 (包含 email 等)
        paypal_generation_type: 链接类型 (已废弃，保留兼容)
        checkout_proxy: Stage 1 代理 (checkout)
        provider_proxy: Stage 2 代理 (Stripe)
        approve_proxy: Stage 3 代理 (approve)
        require_zero: 是否要求 0 元金额 (None 则从配置文件读取)

    Returns:
        {"ok": bool, "url": str, "ba_token": str, "cs_id": str, ...}
    """
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = _paypal_config(cfg)
    target_country = str(target_country or paypal_cfg.get("target_country") or "GB").upper()
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
    ).strip().upper()
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)

    single_proxy_overrides = bool(paypal_cfg.get("explicit_proxy_overrides_stage_proxies", False))
    _checkout = _resolve_stage_proxy(
        checkout_proxy,
        proxy,
        stage_proxies["checkout"],
        _stage_proxy_is_configured(paypal_cfg, "checkout"),
        single_proxy_overrides,
    )
    _provider = _resolve_stage_proxy(
        provider_proxy,
        proxy,
        stage_proxies["provider"],
        _stage_proxy_is_configured(paypal_cfg, "provider", "stripe_init"),
        single_proxy_overrides,
    )
    _stripe_init = _resolve_stage_proxy(
        stripe_init_proxy,
        provider_proxy or proxy,
        stage_proxies.get("stripe_init", _provider),
        _stage_proxy_is_configured(paypal_cfg, "stripe_init"),
        single_proxy_overrides,
    )
    _payment_method = _resolve_stage_proxy(
        payment_method_proxy,
        provider_proxy or proxy,
        stage_proxies.get("payment_method", _provider),
        _stage_proxy_is_configured(paypal_cfg, "payment_method"),
        single_proxy_overrides,
    )
    _confirm = _resolve_stage_proxy(
        confirm_proxy,
        provider_proxy or proxy,
        stage_proxies.get("confirm", _provider),
        _stage_proxy_is_configured(paypal_cfg, "confirm"),
        single_proxy_overrides,
    )
    _approve = _resolve_stage_proxy(
        approve_proxy,
        proxy,
        stage_proxies["approve"],
        _stage_proxy_is_configured(paypal_cfg, "approve", "confirm"),
        single_proxy_overrides,
    )
    # Promotion 阶段 opt-in: 仅显式传入或配置 promotion 代理时启用 (不回退到单代理/默认)
    _promotion = promotion_proxy if promotion_proxy is not None else stage_proxies.get("promotion", "")

    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    stripe_init_proxy = str(_stripe_init or provider_proxy).strip()
    payment_method_proxy = str(_payment_method or provider_proxy).strip()
    confirm_proxy = str(_confirm or provider_proxy).strip()
    approve_proxy = str(_approve or "").strip()
    promotion_proxy = str(_promotion or "").strip()
    promotion_taxes = bool(paypal_cfg.get("promotion_taxes", False))
    promo_campaign_id = str(paypal_cfg.get("promo_campaign_id") or "plus-1-month-free")

    generation_type = _normalized_generation_type(paypal_cfg, paypal_generation_type)
    if _is_chatgpt_checkout_link_generation_type(generation_type):
        return generate_chatgpt_checkout_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            checkout_proxy=checkout_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
            stage_proxy_countries=stage_proxy_countries,
        )
    if _is_hosted_generation_type(generation_type):
        return generate_hosted_long_url(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            stripe_init_proxy=stripe_init_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
            require_zero=require_zero,
            stage_proxy_countries=stage_proxy_countries,
        )

    if _is_zero_due_generation_type(generation_type):
        require_zero = True
    elif require_zero is None:
        require_zero = bool(paypal_cfg.get("require_zero_due", True))
    if _is_paypal_direct_generation_type(generation_type):
        require_ba_token = True
    elif require_ba_token is None:
        require_ba_token = bool(paypal_cfg.get("require_ba_token", False))

    # 从 auth_context 提取 email 和 cookie_header
    email = ""
    cookie_header = ""
    if isinstance(auth_context, dict):
        email = str(auth_context.get("email") or "")
        cookie_header = str(auth_context.get("cookie_header") or "")

    emit = _emit

    state = _paypal_proxy_state(paypal_cfg)
    configured_countries = paypal_cfg.get("stage_proxy_countries") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else {}
    country_overrides = stage_proxy_countries if isinstance(stage_proxy_countries, dict) else {}
    stage_proxy_countries = {
        "checkout": str(country_overrides.get("checkout") or configured_countries.get("checkout") or infer_proxy_country(checkout_proxy) or checkout_country).upper(),
        "promotion": str(country_overrides.get("promotion") or configured_countries.get("promotion") or infer_proxy_country(promotion_proxy) or "").upper(),
        "provider": str(country_overrides.get("provider") or configured_countries.get("provider") or infer_proxy_country(provider_proxy) or target_country).upper(),
        "stripe_init": str(country_overrides.get("stripe_init") or configured_countries.get("stripe_init") or infer_proxy_country(stripe_init_proxy) or target_country).upper(),
        "payment_method": str(country_overrides.get("payment_method") or configured_countries.get("payment_method") or infer_proxy_country(payment_method_proxy) or target_country).upper(),
        "confirm": str(country_overrides.get("confirm") or configured_countries.get("confirm") or infer_proxy_country(confirm_proxy) or target_country).upper(),
        "approve": str(country_overrides.get("approve") or configured_countries.get("approve") or infer_proxy_country(approve_proxy) or target_country).upper(),
    }
    extractor = None
    try:
        device_id = ""
        if isinstance(auth_context, dict):
            device_id = str(
                auth_context.get("oai_did")
                or auth_context.get("oai-device-id")
                or auth_context.get("device_id")
                or ""
            ).strip()
        extractor = PPLinkExtractor(
            access_token=access_token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            stripe_init_proxy=stripe_init_proxy,
            payment_method_proxy=payment_method_proxy,
            confirm_proxy=confirm_proxy,
            approve_proxy=approve_proxy,
            promotion_proxy=promotion_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
            require_zero=require_zero,
            emit=emit,
            cookie_header=cookie_header,
            promotion_taxes=promotion_taxes,
            promo_campaign_id=promo_campaign_id,
            preflight_proxy_check=bool(paypal_cfg.get("preflight_proxy_check", False)),
            rotate_proxy_sessions=bool(paypal_cfg.get("rotate_proxy_sessions", False)),
            proxy_probe_timeout=float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12),
            max_stage_retries=int(paypal_cfg.get("max_stage_retries", paypal_cfg.get("max_checkout_retries", RETRY_ATTEMPTS)) or RETRY_ATTEMPTS),
            max_checkout_retries=int(paypal_cfg.get("max_checkout_retries", RETRY_ATTEMPTS) or RETRY_ATTEMPTS),
            proxy_state=state,
            stage_proxy_countries=stage_proxy_countries,
            device_id=device_id,
        )
        result = extractor.extract()
        ba_token = str(result.get("ba_token") or "").strip()
        url = str(result.get("url") or "").strip()
        link_type = str(result.get("link_type") or "").strip()
        if require_ba_token and (not ba_token or "paypal_ba" not in link_type):
            return {
                "ok": False,
                "error": "ba_not_resolved",
                "error_code": "ba_not_resolved",
                "url": "",
                "ba_token": "",
                "cs_id": result.get("cs_id", ""),
                "link_type": link_type,
                "amount": result.get("amount"),
                "currency": result.get("currency", ""),
                "target_country": result.get("target_country", ""),
                "checkout_country": result.get("checkout_country", ""),
                "checkout_proxy": result.get("checkout_proxy", ""),
                "provider_proxy": result.get("provider_proxy", ""),
                "stripe_init_proxy": result.get("stripe_init_proxy", ""),
                "payment_method_proxy": result.get("payment_method_proxy", ""),
                "confirm_proxy": result.get("confirm_proxy", ""),
                "approve_proxy": result.get("approve_proxy", ""),
                "promotion_proxy": result.get("promotion_proxy", ""),
                "proxy_exits": result.get("proxy_exits", {}),
                "fallback_url": url,
            }

        # 兼容旧格式
        return {
            "ok": result.get("ok", False),
            "url": url,
            "ba_token": ba_token,
            "cs_id": result.get("cs_id", ""),
            "link_type": link_type,
            "amount": result.get("amount"),
            "currency": result.get("currency", ""),
            "target_country": result.get("target_country", ""),
            "checkout_country": result.get("checkout_country", ""),
            "checkout_proxy": result.get("checkout_proxy", ""),
            "provider_proxy": result.get("provider_proxy", ""),
            "stripe_init_proxy": result.get("stripe_init_proxy", ""),
            "payment_method_proxy": result.get("payment_method_proxy", ""),
            "confirm_proxy": result.get("confirm_proxy", ""),
            "approve_proxy": result.get("approve_proxy", ""),
            "promotion_proxy": result.get("promotion_proxy", ""),
            "proxy_exits": result.get("proxy_exits", {}),
            "side_effect_started": bool(result.get("side_effect_started", False)),
            "promotion_applied": bool(result.get("promotion_applied", False)),
            "workflow_attempt": int(result.get("workflow_attempt") or 1),
            "last_retry_error": result.get("last_retry_error") if isinstance(result.get("last_retry_error"), dict) else {},
        }
    except PaymentOutcomeUnknownError as e:
        # A side-effect stage already ran; report the unresolved outcome instead
        # of a plain failure so the caller reconciles and does not retry.
        if extractor is not None:
            extractor.proxy_state.record_pair_result(
                extractor.checkout_proxy,
                extractor.provider_proxy,
                extractor.approve_proxy,
                False,
                str(e),
            )
        return {
            "ok": False,
            "status": "unknown",
            "error": str(e),
            "error_code": e.error_code,
            "error_stage": e.error_stage,
            "retryable": False,
            "side_effect_started": True,
            "requires_reconciliation": True,
            "url": "",
            "ba_token": "",
            "target_country": target_country,
            "checkout_country": checkout_country,
        }
    except CheckoutNotZeroDueError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "checkout_not_zero_due",
            "error_stage": "eligibility",
            "status": "failed",
            "retryable": False,
            "eligible": False,
            "classification": "ineligible",
            "decision": "nonzero_offer",
            "url": "",
            "ba_token": "",
            "amount": e.amount,
            "currency": e.currency,
            "target_country": target_country,
            "checkout_country": checkout_country,
        }
    except Exception as e:
        if extractor is not None:
            extractor.proxy_state.record_pair_result(
                extractor.checkout_proxy,
                extractor.provider_proxy,
                extractor.approve_proxy,
                False,
                str(e),
            )
        diagnostic = e.diagnostic() if hasattr(e, "diagnostic") else {}
        return {
            "ok": False,
            "error": str(e),
            "error_code": str(getattr(e, "error_code", "") or "payment_link_extraction_failed"),
            "error_stage": str(getattr(e, "error_stage", "") or "adapter"),
            "status": str(getattr(e, "status", "") or "failed"),
            "retryable": bool(getattr(e, "retryable", False)),
            "url": "",
            "ba_token": "",
            "target_country": target_country,
            "checkout_country": checkout_country,
            "workflow_attempt": int(getattr(extractor, "workflow_attempt", 0) or 0),
            "last_retry_error": getattr(extractor, "last_retry_error", {}),
            **diagnostic,
        }



def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


def _canonical_checkout_long_url(cs_id: str) -> str:
    cs_id = str(cs_id or "").strip()
    return f"https://pay.openai.com/c/pay/{cs_id}" if cs_id else ""


def _normalized_generation_type(paypal_cfg: dict[str, Any], override: str | None = None) -> str:
    raw = str(
        override
        or paypal_cfg.get("link_generation_type")
        or paypal_cfg.get("generation_type")
        or paypal_cfg.get("paypal_generation_type")
        or ""
    ).strip().lower().replace("-", "_")
    return raw


def _is_paypal_direct_generation_type(value: str) -> bool:
    return value in {
        "pp_direct", "paypal_direct", "direct_pp", "paypal_approve", "ba_direct", "ba_approve",
        "pp_direct_zero_due", "paypal_direct_zero_due", "direct_pp_zero_due", "paypal_approve_zero_due",
        "ba_direct_zero_due", "ba_approve_zero_due", "pp_direct_0_due", "paypal_direct_0_due",
        "pp_direct_force_zero", "paypal_direct_force_zero", "paypal_direct_require_zero_due",
    }


def _is_zero_due_generation_type(value: str) -> bool:
    return value in {
        "pp_direct_zero_due", "paypal_direct_zero_due", "direct_pp_zero_due", "paypal_approve_zero_due",
        "ba_direct_zero_due", "ba_approve_zero_due", "pp_direct_0_due", "paypal_direct_0_due",
        "pp_direct_force_zero", "paypal_direct_force_zero", "paypal_direct_require_zero_due",
    }


def _is_hosted_generation_type(value: str) -> bool:
    return value in {"long", "long_link", "hosted", "hosted_long", "hosted_long_url", "stripe_hosted", "chatgpt_checkout"}


def _is_chatgpt_checkout_link_generation_type(value: str) -> bool:
    return value in {"chatgpt_checkout_link", "checkout_link", "short_checkout", "chatgpt_short_link"}


def _chatgpt_checkout_url(processor_entity: str, cs_id: str) -> str:
    processor_entity = str(processor_entity or "").strip()
    cs_id = str(cs_id or "").strip()
    return f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}" if processor_entity and cs_id else ""


def _checkout_country_from_cfg(paypal_cfg: dict[str, Any], explicit_country: str | None = None, default: str = "JP") -> str:
    if explicit_country:
        return str(explicit_country).strip().upper()
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    candidates = [
        regions[0] if regions else "",
        paypal_cfg.get("checkout_country"),
        paypal_cfg.get("billing_country"),
        paypal_cfg.get("target_country"),
        default,
    ]
    for candidate in candidates:
        value = str(candidate or "").strip().upper()
        if value:
            return value
    return default


def _prepare_configured_stage_proxy(
    paypal_cfg: dict,
    state: PayPalProxyState,
    stage: str,
    proxy: str,
    expected_country: str,
    emit: Any,
) -> tuple[str, dict[str, Any]]:
    prepared = normalize_proxy_url(proxy)
    expected = str(expected_country or "").upper()
    if prepared and bool(paypal_cfg.get("rotate_proxy_sessions", False)):
        prepared = rotate_stage_proxy_session(prepared, expected)
    if not prepared:
        return "", {"ok": True, "stage": stage, "proxy": "DIRECT"}
    label = redact_proxy_url(prepared)
    if not bool(paypal_cfg.get("preflight_proxy_check", False)):
        emit("proxy", f"{stage} proxy={label} (preflight disabled)")
        return prepared, {"ok": True, "stage": stage, "proxy": label}
    result = probe_proxy(
        prepared,
        expected_country=expected,
        stage=stage,
        timeout=float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12),
    )
    state.record_result(stage, prepared, result.ok, result.error, result.country_code)
    detail = {**result.to_dict(), "proxy": label}
    if not result.ok:
        raise RuntimeError(
            f"proxy_preflight_failed:{stage}:expected={expected or 'ANY'}:"
            f"actual={result.country_code or 'unknown'}:{result.error}"
        )
    emit("proxy", f"{stage} exit={result.ip}/{result.country_code} {result.country}")
    return prepared, detail


def generate_chatgpt_checkout_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    stage_proxy_countries: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a ChatGPT checkout session and return chatgpt.com/checkout/{entity}/{cs_id}."""
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = _paypal_config(cfg)
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    target_country = str(
        target_country
        or paypal_cfg.get("target_country")
        or checkout_country
        or (regions[0] if regions else None)
        or "US"
    ).strip().upper()
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
        or "US"
    ).strip().upper()
    currency = CURRENCY_MAP.get(checkout_country, "USD")
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)
    checkout_proxy = str(checkout_proxy or proxy or stage_proxies["checkout"] or "").strip()
    state = _paypal_proxy_state(paypal_cfg)

    emit = _emit

    try:
        country_overrides = stage_proxy_countries if isinstance(stage_proxy_countries, dict) else {}
        expected_country = str(
            country_overrides.get("checkout")
            or ((paypal_cfg.get("stage_proxy_countries") or {}).get("checkout") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else "")
            or infer_proxy_country(checkout_proxy)
            or checkout_country
        ).upper()
        checkout_proxy, proxy_exit = _prepare_configured_stage_proxy(
            paypal_cfg, state, "checkout", checkout_proxy, expected_country, emit,
        )
        emit("checkout", f"Stage 1: proxy={redact_proxy_url(checkout_proxy)} for ChatGPT checkout link")
        _cookie = ""
        if isinstance(auth_context, dict):
            _cookie = str(auth_context.get("cookie_header") or "")
        contract = CheckoutRequestContract.for_payment_method(
            "paypal", billing_country=checkout_country, currency=currency,
            payment_locale="en", browser_locale="en-US", browser_timezone="Asia/Shanghai",
        )
        checkout_body = contract.checkout_payload()
        r = _checkout_post(
            "https://chatgpt.com/backend-api/payments/checkout",
            checkout_body, access_token, _cookie, checkout_proxy, CHATGPT_TIMEOUT,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "link_type": "chatgpt_checkout_link"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "link_type": "chatgpt_checkout_link"}
        checkout_data = r.json() or {}
        checkout = CheckoutSessionContract.from_payload(
            checkout_data, billing_country=checkout_country, fallback_publishable_key=DEFAULT_STRIPE_PK,
        )
        cs_id = checkout.checkout_session_id
        processor_entity = checkout.processor_entity
        url = _chatgpt_checkout_url(processor_entity, cs_id)
        emit("checkout", f"checkout success: cs_id={cs_id} entity={processor_entity} country={checkout_country} currency={currency}")
        return {
            "ok": True,
            "url": url,
            "checkout_url": url,
            "short_url": url,
            "ba_token": "",
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "link_type": "chatgpt_checkout_link",
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "currency": currency,
            "checkout_proxy": redact_proxy_url(checkout_proxy),
            "provider_proxy": "",
            "approve_proxy": "",
            "proxy_exits": {"checkout": proxy_exit},
            "promo_campaign_id": "plus-1-month-free",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "chatgpt_checkout_link_failed", "link_type": "chatgpt_checkout_link", "url": ""}


def generate_hosted_long_url(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    stripe_init_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    require_zero: bool | None = None,
    stage_proxy_countries: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate a ChatGPT/Stripe hosted checkout URL without entering BA/approve flow."""
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = _paypal_config(cfg)
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    target_country = str(
        target_country
        or paypal_cfg.get("target_country")
        or checkout_country
        or (regions[0] if regions else None)
        or "US"
    ).strip().upper()
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
        or "US"
    ).strip().upper()
    currency = CURRENCY_MAP.get(checkout_country, "USD")
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)
    checkout_proxy = str(checkout_proxy or proxy or stage_proxies["checkout"] or "").strip()
    provider_proxy = str(provider_proxy or proxy or stage_proxies["provider"] or "").strip()
    stripe_init_proxy = str(stripe_init_proxy or stage_proxies.get("stripe_init") or provider_proxy).strip()
    state = _paypal_proxy_state(paypal_cfg)
    if require_zero is None:
        require_zero = bool(paypal_cfg.get("require_zero_due", True))

    emit = _emit

    try:
        countries = paypal_cfg.get("stage_proxy_countries") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else {}
        country_overrides = stage_proxy_countries if isinstance(stage_proxy_countries, dict) else {}
        checkout_proxy, checkout_exit = _prepare_configured_stage_proxy(
            paypal_cfg,
            state,
            "checkout",
            checkout_proxy,
            str(country_overrides.get("checkout") or countries.get("checkout") or infer_proxy_country(checkout_proxy) or checkout_country),
            emit,
        )
        emit("checkout", f"Stage 1: proxy={redact_proxy_url(checkout_proxy)} for hosted checkout")
        _cookie = ""
        if isinstance(auth_context, dict):
            _cookie = str(auth_context.get("cookie_header") or "")
        contract = CheckoutRequestContract.for_payment_method(
            "paypal", billing_country=checkout_country, currency=currency,
            payment_locale="en", browser_locale="en-US", browser_timezone="Asia/Shanghai",
        )
        checkout_body = contract.checkout_payload()
        r = _checkout_post(
            "https://chatgpt.com/backend-api/payments/checkout",
            checkout_body, access_token, _cookie, checkout_proxy, CHATGPT_TIMEOUT,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "link_type": "chatgpt_checkout_hosted_long_url"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "link_type": "chatgpt_checkout_hosted_long_url"}
        checkout_data = r.json() or {}
        checkout = CheckoutSessionContract.from_payload(
            checkout_data, billing_country=checkout_country, fallback_publishable_key=DEFAULT_STRIPE_PK,
        )
        cs_id = checkout.checkout_session_id
        stripe_pk = checkout.publishable_key
        processor_entity = checkout.processor_entity
        emit("checkout", f"checkout success: cs_id={cs_id} country={checkout_country} currency={currency}")

        stripe_init_proxy, stripe_exit = _prepare_configured_stage_proxy(
            paypal_cfg,
            state,
            "stripe_init",
            stripe_init_proxy,
            str(country_overrides.get("stripe_init") or country_overrides.get("provider") or countries.get("stripe_init") or infer_proxy_country(stripe_init_proxy) or target_country),
            emit,
        )
        emit("stripe_init", f"Stage 2: proxy={redact_proxy_url(stripe_init_proxy)} for Stripe init")
        stripe = _new_session(stripe_init_proxy)
        init_body = contract.stripe_init_payload(stripe_pk, stripe_version=STRIPE_VERSION)
        init_resp = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", data=init_body, timeout=DEFAULT_TIMEOUT)
        if init_resp.status_code >= 400:
            return {"ok": False, "error": f"stripe init failed: {init_resp.status_code} {init_resp.text[:300]}", "error_code": "stripe_init_failed", "link_type": "chatgpt_checkout_hosted_long_url", "cs_id": cs_id, "target_country": target_country, "checkout_country": checkout_country, "billing_country": checkout_country}
        init = init_resp.json() or {}
        amount_info = stripe_amount_details(init)
        amount = amount_info.get("amount")
        state.record_zero_result(checkout_proxy, checkout_country, amount)
        emit("stripe_init", f"amount={amount} currency={amount_info.get('currency')} source={amount_info.get('source')}")
        if require_zero and amount is not None and amount != 0:
            return {
                "ok": False,
                "error": f"checkout_not_zero_due: amount={amount} {amount_info.get('currency')}",
                "error_code": "checkout_not_zero_due",
                "link_type": "chatgpt_checkout_hosted_long_url",
                "url": "",
                "cs_id": cs_id,
                "amount": amount,
                "currency": str(amount_info.get("currency") or currency).upper(),
                "target_country": target_country,
                "checkout_country": checkout_country,
                "billing_country": checkout_country,
                "payment_method_types": init.get("payment_method_types") or [],
            }
        if require_zero and amount is None:
            # Stripe 响应里取不到金额证据 —— 协议模糊，既不能当 0 元放行也不能当非零误杀。
            # 归为 unknown，交上层对账；retryable=False 防止自动重试复制 checkout。
            return {
                "ok": False,
                "status": "unknown",
                "error": f"checkout_amount_unknown: amount not present in stripe init response {amount_info.get('currency')}",
                "error_code": "checkout_amount_unknown",
                "requires_reconciliation": True,
                "retryable": False,
                "link_type": "chatgpt_checkout_hosted_long_url",
                "url": "",
                "cs_id": cs_id,
                "amount": None,
                "currency": str(amount_info.get("currency") or currency).upper(),
                "target_country": target_country,
                "checkout_country": checkout_country,
                "billing_country": checkout_country,
                "payment_method_types": init.get("payment_method_types") or [],
            }
        short_url = _canonical_checkout_long_url(cs_id)
        hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or short_url
        return {
            "ok": True,
            "url": hosted_url,
            "checkout_url": hosted_url,
            "short_url": short_url,
            "stripe_hosted_url": hosted_url,
            "ba_token": "",
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "link_type": "chatgpt_checkout_hosted_long_url",
            "amount": amount,
            "currency": str(amount_info.get("currency") or currency).upper(),
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "payment_method_types": init.get("payment_method_types") or [],
            "checkout_proxy": redact_proxy_url(checkout_proxy),
            "provider_proxy": redact_proxy_url(provider_proxy),
            "stripe_init_proxy": redact_proxy_url(stripe_init_proxy),
            "approve_proxy": "",
            "proxy_exits": {"checkout": checkout_exit, "stripe_init": stripe_exit},
            "promo_campaign_id": "plus-1-month-free",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "hosted_long_url_failed", "link_type": "chatgpt_checkout_hosted_long_url", "url": ""}


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility entrypoint backed by the unified payment-link manager."""
    from .payment_link_manager import generate_payment_link as managed_generate

    return managed_generate(
        access_token=access_token,
        proxy=proxy,
        payment_method=payment_method,
        auth_context=auth_context,
        paypal_generation_type=paypal_generation_type,
        **kwargs,
    )


if __name__ == "__main__":
    main()

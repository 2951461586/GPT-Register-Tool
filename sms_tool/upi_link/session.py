from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..checkout_contract import PLUS_TRIAL_CAMPAIGN_ID, browser_profile_for_country
except ImportError:
    from checkout_contract import PLUS_TRIAL_CAMPAIGN_ID, browser_profile_for_country  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..paypal_extract import _new_session
except ImportError:
    from paypal_extract import _new_session  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:
    from pp_link_helpers import DEFAULT_TIMEOUT, STRIPE_VERSION  # type: ignore
try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - curl_cffi is a project dependency
    _curl_requests = None
from typing import Any
from collections.abc import Mapping
from pathlib import Path
import base64
import json
import os
from urllib.parse import quote
import re
import secrets
import time
from urllib.parse import urlencode
import uuid
from ._extract import (
    _upi_int_value,
    _upi_is_provider_decline_text,
)
from .constants import (
    PROJECT_ROOT,
    UPI_ATTESTATION_ENV_KEYS,
    UPI_BILLING_ADDRESSES,
    UPI_BILLING_IN,
    UPI_BILLING_NAMES,
    UPI_CHATGPT_CLIENT_BUILD_NUMBER,
    UPI_CHATGPT_CLIENT_VERSION,
    UPI_EMAIL_DOMAINS,
    UPI_FINGERPRINT_TEMPLATES,
    UPI_SENTINEL_CHECKOUT_FLOW,
    UPI_WARMUP_TIMEOUT,
    _UPI_ATTESTATION_ANY_RE,
    _UPI_ATTESTATION_RE,
    _UPI_PAGE_BUILD_ANY_RE,
    _UPI_PAGE_BUILD_RE,
    _UPI_PAGE_SEQ_ANY_RE,
    _UPI_PAGE_SEQ_JSON_RE,
    _UPI_PAGE_SEQ_RE,
)
from .env import _emit, _env_bool, _env_str


def _upi_billing_profile(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """构造印度账单资料。

    优先级（后者覆盖前者）:
      随机池 → ``upi.fixed_billing`` / ``UPI_USE_FIXED_BILLING`` → 逐字段环境变量。

    参考实现 ``upi_billing_profile()`` 只有「随机池 → 固定 → 环境变量」三级,
    这里多一级「配置段」, 因为本项目配置是 JSON 而非环境变量驱动。
    """
    section = cfg if isinstance(cfg, Mapping) else {}
    first_name, last_name = UPI_BILLING_NAMES[secrets.randbelow(len(UPI_BILLING_NAMES))]
    line1, city, postal_code, state = UPI_BILLING_ADDRESSES[secrets.randbelow(len(UPI_BILLING_ADDRESSES))]
    domain = UPI_EMAIL_DOMAINS[secrets.randbelow(len(UPI_EMAIL_DOMAINS))]
    profile = {
        "email": f"{first_name.lower()}.{last_name.lower()}{secrets.randbelow(9000) + 1000}@{domain}",
        "name": f"{first_name} {last_name}",
        "country": "IN",
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal_code,
        "state": state,
    }

    fixed = section.get("fixed_billing")
    use_fixed = _env_bool("UPI_USE_FIXED_BILLING", False) or bool(fixed)
    if use_fixed:
        baseline = dict(UPI_BILLING_IN)
        if isinstance(fixed, Mapping):
            baseline.update({str(k): str(v) for k, v in fixed.items() if v is not None})
        # 固定资料的键名是 ``postal``, 内部统一用 ``postal_code``。
        if baseline.get("postal") and not baseline.get("postal_code"):
            baseline["postal_code"] = baseline["postal"]
        profile.update({k: str(v) for k, v in baseline.items() if k in profile})

    env_map = {
        "email": "UPI_EMAIL",
        "name": "UPI_NAME",
        "country": "UPI_BILLING_COUNTRY",
        "line1": "UPI_LINE1",
        "line2": "UPI_LINE2",
        "city": "UPI_CITY",
        "postal_code": "UPI_POSTAL_CODE",
        "state": "UPI_STATE",
    }
    for key, env_name in env_map.items():
        value = _env_str(env_name, "")
        if value:
            profile[key] = value

    profile["country"] = str(profile.get("country") or "IN").strip().upper()
    return profile


def _upi_fingerprint(index: int | None = None, country: str | None = None) -> dict[str, str]:
    """按索引（缺省随机）取一套自洽的浏览器身份模板。

    ``locale`` / ``timezone`` / ``accept_language`` 从契约层
    （``checkout_contract.browser_profile_for_country``）取，模板里只保留
    UA 与 ``sec-ch-ua*`` 这类**硬件/浏览器层面**的身份特征。

    为什么不把语言时区硬编码进模板：那是一份与 ``COUNTRY_BROWSER_PROFILES``
    平行的事实来源，改一处漏一处就会出现「UA 说印度、时区说加尔各答、
    但契约层要求越南」这种自相矛盾。契约层是唯一真源。
    """
    if index is None:
        index = secrets.randbelow(len(UPI_FINGERPRINT_TEMPLATES))
    template = UPI_FINGERPRINT_TEMPLATES[index % len(UPI_FINGERPRINT_TEMPLATES)]
    result = dict(template)

    profile = browser_profile_for_country(country) if country else None
    if profile is not None:
        # 🔴 契约层字段名是 ``browser_locale`` / ``browser_timezone``，不是
        # ``locale`` / ``timezone``。这里**故意显式取值并在缺失时抛错**，不走
        # ``getattr(..., "")`` 兜底——兜底会让字段改名变成「静默沿用模板里的
        # en-IN」，运行期看不出任何异常，只有抓包才知道语言错了。
        locale = str(getattr(profile, "browser_locale", "") or "")
        timezone_name = str(getattr(profile, "browser_timezone", "") or "")
        if not locale or not timezone_name:
            raise RuntimeError("browser_profile_for_country(%r) 返回的 profile 缺字段: %r" % (country, profile))
        result["locale"] = locale
        result["accept_language"] = _upi_accept_language_for(locale)
        result["timezone"] = timezone_name
    return result


def _upi_record_zero_result(proxy_state: Any, proxy: str, country: str, amount: Any) -> None:
    """把本轮 checkout 的实付金额记进代理状态（0 元缓存）。

    **这块能力项目里本来就有** —— ``sms_tool/paypal_proxy.PayPalProxyState``
    已经实现了 ``record_zero_result`` / ``zero_status``，三要素齐全：

    * 禁用开关：构造参数 ``enabled``（``proxy_state_from_config`` 已从配置读）
    * 存储位置：``runtime/paypal_proxy_state.json``（``runtime/`` 整目录 gitignore）
    * 过期策略：``zero_cache_ttl_seconds``，默认 1800 秒

    缺的只是**UPI 这条流水线没接上去**。参考实现用它做代理调度（把出过 0 元的
    代理排前面、跳过已知出非零的），本项目 PayPal 链路已经这么用了。

    🔴 这里必须**吞掉所有异常**：0 元缓存是纯调度优化，记不进去不影响本轮
    提链成败，绝不能因为它把主流程带崩。
    """
    if proxy_state is None or not proxy:
        return
    try:
        proxy_state.record_zero_result(proxy, country, amount)
    except Exception:
        # 调度优化失败不应该有任何可观测后果，也不该刷日志噪声
        pass


def _upi_accept_language_for(locale: str) -> str:
    """从 ``en-IN`` / ``vi-VN`` 这类 locale 生成 ``Accept-Language``。

    契约层的 ``BrowserProfile`` 只给 ``locale``，而 ``Accept-Language`` 需要
    「主标签 + 带地区 + 降级链」。规则与浏览器一致：``vi-VN`` →
    ``vi-VN,vi;q=0.9``。
    """
    value = str(locale or "").strip()
    if not value:
        return "en;q=0.9"
    primary = value.split("-", 1)[0].lower()
    if primary == value.lower():
        return "%s;q=0.9" % primary
    return "%s,%s;q=0.9" % (value, primary)


def _upi_apply_fingerprint(session: Any, fingerprint: Mapping[str, str]) -> None:
    """把指纹模板落到 session 的默认 header 上。

    只设置能自洽共存的一组 header；缺任何一个都会让 UA 与
    ``sec-ch-ua-platform`` 互相矛盾。
    """
    if session is None or not fingerprint:
        return
    headers = {
        "User-Agent": fingerprint.get("user_agent", ""),
        "Accept-Language": fingerprint.get("accept_language", "en;q=0.9"),
        "sec-ch-ua": fingerprint.get("sec_ch_ua", ""),
        "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile", "?0"),
        "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform", '""'),
    }
    cleaned = {k: v for k, v in headers.items() if v}
    try:
        session.headers.update(cleaned)
    except Exception:
        pass


def _upi_apply_chatgpt_identity(
    session: Any,
    fingerprint: Mapping[str, str],
    device_id: str,
) -> None:
    """给 ChatGPT 端点的 session 补齐客户端身份头（对齐参考实现）。

    参考实现 ``build_chatgpt_session`` 除 UA 三件套外还带：
    ``oai-device-id`` / ``oai-language`` / ``oai-session-id`` /
    ``oai-client-version`` / ``oai-client-build-number`` /
    ``sec-fetch-*`` / ``Cookie: oai-did=...`` / ``Origin``。
    2026-09-18 实测：缺这组头时 IN 区 checkout 全部 400 unusual activity。
    """
    if session is None:
        return
    locale = str(fingerprint.get("locale", "") or "en-US") if fingerprint else "en-US"
    headers = {
        "Origin": "https://chatgpt.com",
        "oai-device-id": device_id,
        "oai-language": locale,
        "oai-session-id": device_id,
        "oai-client-version": UPI_CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": UPI_CHATGPT_CLIENT_BUILD_NUMBER,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": "oai-did=%s" % device_id,
    }
    try:
        session.headers.update(headers)
    except Exception:
        pass


def _upi_new_chatgpt_session(
    proxy: str,
    fingerprint: Mapping[str, str],
    device_id: str,
    session_token: str = "",
) -> Any:
    """创建面向 ChatGPT 端点的 session（checkout / approve）。

    与参考实现 ``build_chatgpt_session`` 对齐的三个关键差异：

    1. **curl_cffi + impersonate**：标准 ``requests`` 的 TLS 指纹会被
       Cloudflare 标记为自动化流量，实测 checkout 全部 400 unusual activity。
       必须用 ``curl_cffi.requests.Session(impersonate=...)`` 模拟真实浏览器。
    2. **完整指纹头**：``_upi_apply_fingerprint`` 只设了 UA + sec-ch-ua +
       Accept-Language，但参考实现还带 ``Origin`` / ``oai-*`` / ``sec-fetch-*``
       / ``Cookie``。这些由 ``_upi_apply_chatgpt_identity`` 补齐。
    3. **session_token cookie**：``__Secure-next-auth.session-token`` 是
       ChatGPT 的会话凭据，缺失时 checkout 会被判为未登录态。

    ``trust_env=False`` 是刻意的——不继承系统环境变量里的代理配置，
    避免与显式传入的 proxy 冲突（参考实现同样这么做）。

    🔴 测试兼容：``_new_session`` 被 patch 时（FakeSession），本函数**不**
    替换为 curl session，而是直接在 patch 后的 session 上补头。这样既有测试
    无需改动就能继续拦截 checkout 请求。
    """
    # 先走 _new_session（可能被测试 patch 成 FakeSession）
    base = _new_session(proxy)
    # 如果 _new_session 返回的是标准 requests.Session 且 curl_cffi 可用，
    # 升级为 curl session（生产路径）。
    if _curl_requests is not None and type(base).__module__.startswith("requests"):
        impersonate = str(fingerprint.get("impersonate") or "chrome136")
        session = _curl_requests.Session(impersonate=impersonate)
        if hasattr(session, "trust_env"):
            session.trust_env = False
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
    else:
        session = base
        if hasattr(session, "trust_env"):
            session.trust_env = False
    _upi_apply_fingerprint(session, fingerprint)
    _upi_apply_chatgpt_identity(session, fingerprint, device_id)
    if session_token:
        existing_cookie = str(session.headers.get("Cookie") or "")
        if existing_cookie:
            session.headers["Cookie"] = f"{existing_cookie}; __Secure-next-auth.session-token={session_token}"
        else:
            session.headers["Cookie"] = f"__Secure-next-auth.session-token={session_token}"
    return session


def _upi_session_is_live(session: Any) -> bool:
    """True only for a real transport session, never a test double.

    The risk rail (page warmup, Sentinel issuance, sentinel ping) performs real
    network calls. Tests patch ``_new_session`` with a fake, so gating on the
    concrete curl_cffi type keeps the offline suite free of network side
    effects while production always takes the live path.
    """
    if session is None:
        return False
    if _curl_requests is not None:
        try:
            return isinstance(session, _curl_requests.Session)
        except Exception:
            return False
    return type(session).__module__.split(".", 1)[0] in {"requests", "curl_cffi"}


def _upi_account_id_from_token(token: str) -> str:
    """Read the ChatGPT account id from an access token without verifying it."""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return ""
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        auth = payload.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or payload.get("chatgpt_account_id") or "")
    except Exception:
        return ""


def _upi_promo_page_url(campaign: str = PLUS_TRIAL_CAMPAIGN_ID) -> str:
    """The campaign entry page the browser checkout navigates through first."""
    return "https://chatgpt.com/?promo_campaign=" + quote(str(campaign or PLUS_TRIAL_CAMPAIGN_ID), safe="")


def _upi_attestation_deploy_id(attestation: str) -> str:
    """Extract the deployment hash from a ``webDeploymentAttestation`` value."""
    for part in str(attestation or "").strip().split("."):
        if len(part) >= 16 and re.fullmatch(r"[A-Za-z0-9_-]+", part):
            return part
    return ""


def _upi_scrape_page_identity(html: str) -> tuple[str, str, str]:
    """Return ``(client_version, client_build, attestation)`` scraped from HTML.

    Aligned with the reference ``_apply_upi_page_identity`` /
    ``_fetch_web_attestation``: the live page carries ``data-build`` /
    ``data-seq`` and the server-signed ``webDeploymentAttestation``. Pinning
    these to constants makes the client look stale and self-contradictory.
    """
    text = str(html or "")
    version = ""
    build_match = _UPI_PAGE_BUILD_RE.search(text) or _UPI_PAGE_BUILD_ANY_RE.search(text)
    if build_match:
        raw = build_match.group(1).strip()
        version = raw if raw.startswith("prod-") else f"prod-{raw}"
    seq_match = _UPI_PAGE_SEQ_RE.search(text) or _UPI_PAGE_SEQ_ANY_RE.search(text) or _UPI_PAGE_SEQ_JSON_RE.search(text)
    build = str(seq_match.group(1)).strip() if seq_match else ""
    attestation = ""
    match = _UPI_ATTESTATION_RE.search(text) or _UPI_ATTESTATION_ANY_RE.search(text)
    if match and "." in match.group(1):
        attestation = match.group(1).strip()
    return version, build, attestation


def _upi_env_attestation() -> str:
    for key in UPI_ATTESTATION_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value and "." in value:
            return value
    return ""


def _upi_find_attestation(text: Any) -> str:
    """Find a two-part ``webDeploymentAttestation`` in a serialized page."""
    value = str(text or "")
    match = _UPI_ATTESTATION_RE.search(value) or _UPI_ATTESTATION_ANY_RE.search(value)
    if match and "." in match.group(1):
        return match.group(1).strip()
    return ""


def _upi_warmup_session(
    session: Any,
    *,
    device_id: Any,
    page_url: Any,
    fingerprint: Any,
) -> str:
    """GET the campaign page on the same session so its cookies are absorbed."""
    if not hasattr(session, "get"):
        return ""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": str(fingerprint.get("accept_language") or "en-IN,en;q=0.9"),
        "Referer": page_url,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
    }
    try:
        response = session.get(page_url, headers=headers, timeout=UPI_WARMUP_TIMEOUT)
    except Exception as exc:
        _emit("risk", f"warmup failed (non-fatal): {type(exc).__name__}")
        return ""
    return str(getattr(response, "text", "") or "")


def _upi_ensure_checkout_flow() -> str:
    """Register and return the create-time Sentinel flow.

    The shared ``sentinel`` module ships the registration vocabulary; the UPI
    checkout flow is registered lazily here so a payment-only concept does not
    become part of that public map. ``issue_sentinel_token`` resolves the flow
    through ``FLOW_PAGE_URLS`` at call time, so this is sufficient.
    """
    try:
        from ..sentinel import client as sentinel_client

        flows = getattr(sentinel_client, "FLOW_PAGE_URLS", None)
        if isinstance(flows, dict):
            flows.setdefault(UPI_SENTINEL_CHECKOUT_FLOW, "https://chatgpt.com/")
    except Exception:
        pass
    return UPI_SENTINEL_CHECKOUT_FLOW


def _upi_elements_session(
    stripe: Any,
    *,
    stripe_pk: Any,
    amount: Any,
    stripe_js_id: Any,
    customer_session_secret: Any,
    fingerprint: Any,
) -> dict[str, Any]:
    """Negotiate the deferred-intent Elements session for an ``oaics_`` Checkout.

    Aligned with the reference ``_elements_session`` (non-custom branch): the
    deferred-intent contract carries the CustomerSession secret and the
    authoritative payment method list, and reuses the same `_stripe_version`
    revision as the later confirm.
    """
    if not hasattr(stripe, "get"):
        return {}
    params: dict[str, str] = {
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(_upi_int_value(amount)[0] or 0),
        "deferred_intent[currency]": "inr",
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": "inr",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": str(fingerprint.get("elements_locale") or "en"),
        "browser_timezone": str(fingerprint.get("timezone") or "Asia/Kolkata"),
        "type": "deferred_intent",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "upi",
        "customer_session_client_secret": str(customer_session_secret or ""),
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
    }
    try:
        response = stripe.get(
            "https://api.stripe.com/v1/elements/sessions?" + urlencode(params),
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        _emit("oaics", f"elements session failed: {type(exc).__name__}: {str(exc)[:120]}")
        return {}
    if _upi_int_value(getattr(response, "status_code", 0))[0] >= 400:
        return {}
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _upi_server_mandate(init: Any) -> dict[str, Any]:
    """Return the server-issued UPI mandate_options, if any."""
    options = init.get("payment_method_options") if isinstance(init, Mapping) else None
    upi = options.get("upi") if isinstance(options, Mapping) else None
    mandate = upi.get("mandate_options") if isinstance(upi, Mapping) else None
    return dict(mandate) if isinstance(mandate, Mapping) else {}


def _upi_runtime_version() -> str:
    """Stripe.js 运行时版本（``version`` 字段)。

    参考实现用固定常量 ``DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"``;
    这里允许 ``UPI_STRIPE_RUNTIME_VERSION`` 覆盖以便灰度。
    """
    return _env_str("UPI_STRIPE_RUNTIME_VERSION", "6f8494a281")


def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


def _default_qr_path(prefix: str = "upi") -> str:
    directory = Path(PROJECT_ROOT) / "runtime" / "upi_qr"
    directory.mkdir(parents=True, exist_ok=True)
    stamp, _ = _upi_int_value(time.time())
    return str(directory / f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}.png")


def _write_qr_png(data: str, qr_path: str = "") -> str:
    url = str(data or "").strip()
    if not url:
        return ""
    path = Path(qr_path or _default_qr_path("upi"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode
    except Exception as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("qrcode package is required for UPI QR generation; run pip install qrcode[pil]") from exc
    img = qrcode.make(url)
    try:
        with open(path, "wb") as qr_file:
            img.save(qr_file)
    except OSError as exc:
        raise RuntimeError(f"failed to write UPI QR image to {path}") from exc
    return str(path)


def _upi_classify_failure(error: Any) -> str:
    """把 UPI 流水线的异常压成稳定的 ``error_code``。

    只做「可判读」这一件事：分类边界必须能从异常文本唯一确定，
    不引入新的外部依赖，也不改变任何控制流（异常仍照原样向上抛/被捕获）。

    注意与 ``sms_tool.error_classification`` 的关系：那边是注册制判据，
    但它的注册表里没有 ``generic_decline`` / ``approve blocked`` /
    ``checkout_not_active_session`` 这些 UPI 专有说法（实测都归到
    ``unknown``）。此处先给出比 "upi_qr_failed" 更细的代码，
    后续若要统一，应把这几个词条补进 ``failure_registry.py`` 再回头收敛。
    """
    text = str(error or "").lower()
    # 🔴 下面的规则必须能识别**本函数自己产出的 code**（幂等）。
    #
    # 实测背景：``error_code`` 会存进 session / progress，上层复盘时常拿它
    # **再喂一次**本函数。若识别不了，``upi_redirect_timeout`` 会被判成
    # ``upi_qr_failed``——同一个串自己分类自己得到不同结果。
    #
    # 曾经为此加过一个「先精确匹配已知 code 再走文本规则」的短路分支，
    # 但变异验证显示**关掉它行为完全不变**（文本规则已覆盖全部 5 个 code）⇒
    # 是冗余的防御代码，已移除。现在由下面每条规则的**子串本身**保证幂等：
    #   ``upi_checkout_not_active``  含 "checkout_not_active"
    #   ``upi_provider_declined``    被 ``_upi_is_provider_decline_text`` 命中
    #   ``upi_redirect_timeout``     含 "upi_redirect_timeout"（显式登记过）
    #   ``upi_checkout_unauthorized``含 "unauthorized"
    #   ``upi_qr_failed``           走兜底
    # 改动任一规则前，先跑 ``test_classification_is_idempotent``。
    if "checkout_not_active" in text:
        return "upi_checkout_not_active"
    if _upi_is_provider_decline_text(text) or "approve blocked" in text:
        return "upi_provider_declined"
    if "redirect url resolution timeout" in text or "upi_redirect_timeout" in text:
        return "upi_redirect_timeout"
    if ("poll" in text or "polling" in text) and "timeout" in text:
        return "upi_redirect_timeout"
    if "401" in text or "unauthorized" in text:
        return "upi_checkout_unauthorized"
    return "upi_qr_failed"

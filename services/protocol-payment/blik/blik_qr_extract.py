"""
        BLIK 接口提交 / iDEAL 支付页 URL 提取脚本。

说明：
- BLIK 模式通过 Stripe 接口提交 6 位 BLIK Code 和波兰账单资料，不打开支付页面。
- iDEAL 在 Stripe 里通常是银行授权跳转，不一定有二维码指令页。
- 本脚本会提取 iDEAL redirect URL；成功时该 URL 就是最终扫码/授权界面。
- 默认只读取当前项目的 proxy_seeds.txt；每条 Seed 会派生为 PL checkout、Stripe 和 approve 出口。

用法：
  1) 环境变量方式：
     PP_TOKEN="..." python blik_qr_extract.py

  2) 文件方式：
     把 token 放到当前目录 token.txt
     python blik_qr_extract.py

常用环境变量：
  IDEAL_PAYMENT_METHOD=blik   # blik 或 ideal；默认 blik
  IDEAL_BLIK_CODE=            # BLIK 必填：6 位银行码；不填会直接结束任务
  IDEAL_BANK=ing              # iDEAL 可选：指定银行；不填则走银行选择页
  IDEAL_CONFIRM_INLINE_PM=0   # 默认按 gpthel 流程：先创建 PM，再 confirm 引用 PM
  IDEAL_UPDATE_TAX_REGION=0   # 默认不额外提交 tax_region
  IDEAL_CHECKOUT_SNAPSHOT=0   # 默认不额外提交 checkout snapshot
  IDEAL_CHECKOUT_COUNTRY=PL   # BLIK 默认 PL；iDEAL 可设 NL
  IDEAL_MAX_RETRY=900         # 可选；不设置时默认每批 30 轮、最多 30 批
  IDEAL_PROVIDER_PER_CHECKOUT=30
  IDEAL_MAX_ACCOUNT_ATTEMPTS=0   # 显式设为 0 时按代理池总容量跑
  IDEAL_MAX_APPROVE_BLOCKED=900
  IDEAL_WORKERS=30            # 正式流程并发数；默认每批 30 个 provider 同时跑
  IDEAL_WORKERS_MAX=30
  IDEAL_APPROVE_RETRY_MAX=10 # approve 阶段最大重试次数
  IDEAL_APPROVE_STICKY=0     # approve 默认走代理池换出口
  IDEAL_FOLLOW_REDIRECT=1
  IDEAL_REQUIRE_ZERO=1        # 默认强制 0 元，不生成非 0 元链
  IDEAL_DUMP_LIMIT=6000       # 抓包响应保存长度
  IDEAL_PROXY_SKIP_FAILED=1   # 普通流程失败下次软跳过
  IDEAL_PROXY_REMOVE_FAILED=1 # 明确的代理失败会从原代理文件移除
  IDEAL_PROXY_GEO_CHECK=1     # 默认检测代理出口国家
  IDEAL_PROXY_PRECHECK=1      # 默认启动时先预筛代理出口国家
  IDEAL_PROXY_PRECHECK_WORKERS=50
  IDEAL_PROXY_PRECHECK_WORKERS_MAX=50
  IDEAL_PROXY_PRECHECK_TIMEOUT=20
  IDEAL_PROXY_DEFAULT_SCHEME=http # 裸代理默认协议；Mars SOCKS5 可设 socks5h
  IDEAL_PROXY_GEO_USE_PRE_PROXY=0 # 国家检测默认不走本机前置，避免检测到 Clash 出口国家
  IDEAL_PROXY_TARGET_CHECK=1  # 预筛/运行中确认代理能访问 chatgpt.com/api.stripe.com
  IDEAL_PROXY_TARGET_USE_PRE_PROXY=1 # 目标站检测默认走本机前置，匹配正式访问链路
  IDEAL_PROXY_FAIL_COOLDOWN=180 # 失败代理冷却秒数，0 表示按旧逻辑一直跳过
  IDEAL_PROXY_REMOVE_AFTER_FAILS=3 # 已复用代理健康类失败累计 3 次移除；普通代理失败 1 次移除
  IDEAL_ZERO_CACHE=1          # 缓存 checkout 0 元资格结果，用于后续优先调度
  IDEAL_PROXY_SEED_FILE=proxy_seeds.txt
  PP_PROMO_MODE=campaign      # 默认直接走 promo_campaign，避免 coupon 再 fallback 多耗时
  PP_TRIAL_DAYS=30            # 仅 PP_PROMO_MODE=trial/free_trial 时使用
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, RLock, local
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

try:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:
    CurlOpt = None
    CurlCffiSession = None


SCRIPT_DIR = Path(__file__).resolve().parent
PROTOCOL_ROOT = SCRIPT_DIR.parent
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))
from common.extractor_runtime import (
    PROXY_COUNTRY_SELECTOR_RE as _PROXY_COUNTRY_SELECTOR_RE,
    normalize_pre_proxy_url,
    proxy_chain_key,
)
from common.payload_helpers import (
    format_payment_summary,
    infer_processor_entity,
    normalize_access_token as normalize_token,
    payment_page_summary,
    stripe_browser_id,
)
from common.extractor_helpers import (
    is_direct_remove_proxy_error as shared_is_direct_remove_proxy_error,
    is_proxy_health_failure as shared_is_proxy_health_failure,
    is_user_already_paid_error,
)
from common.payment_predicates import (
    checkout_response_has_promo as shared_checkout_response_has_promo,
    checkout_response_has_trial as shared_checkout_response_has_trial,
    extract_qr_candidates as shared_extract_qr_candidates,
    is_approve_failure_error as shared_is_approve_failure_error,
    is_checkout_not_active_error as shared_is_checkout_not_active_error,
    is_qr_candidate as shared_is_qr_candidate,
    is_resource_url as shared_is_resource_url,
)
from common.proxy_state import (
    ProxyStateStore,
    checkout_zero_cache_status as shared_checkout_zero_cache_status,
    checkout_zero_cache_ttl as shared_checkout_zero_cache_ttl,
    is_reused_proxy_record as shared_is_reused_proxy_record,
    load_proxy_state as shared_load_proxy_state,
    order_proxy_group as shared_order_proxy_group,
    prune_proxy_seed_state as shared_prune_proxy_seed_state,
    proxy_pair_key as shared_proxy_pair_key,
    proxy_record as shared_proxy_record,
    proxy_remove_after_fails as shared_proxy_remove_after_fails,
    proxy_state_key as shared_proxy_state_key,
    proxy_state_path as shared_proxy_state_path,
    record_checkout_zero_result as shared_record_checkout_zero_result,
    record_proxy_health_failure as shared_record_proxy_health_failure,
    record_proxy_pair_approve_success as shared_record_proxy_pair_approve_success,
    record_proxy_pair_result as shared_record_proxy_pair_result,
    record_proxy_result as shared_record_proxy_result,
    save_proxy_state as shared_save_proxy_state,
    successful_approve_preferences as shared_successful_approve_preferences,
    successful_pair_preferences as shared_successful_pair_preferences,
)
from common.proxy_seed_file import (
    ProxySeedFile,
    proxy_seed_file as shared_proxy_seed_file,
    unique_proxy_seeds as shared_unique_proxy_seeds,
)
from common.protocol_core import (
    ProtocolResultReporter,
    run_extractor_entrypoint,
    amount_from_payload as common_amount_from_payload,
    collect_strings as common_collect_strings,
    collect_urls as common_collect_urls,
    env_bool as common_env_bool,
    env_int as common_env_int,
    extract_redirect_url as common_extract_redirect_url,
    find_submission_attempt as common_find_submission_attempt,
    first_value_by_key as common_first_value_by_key,
)
from common.file_loading import (
    load_proxy_file as shared_load_proxy_file,
    load_token as shared_load_token,
)
from common.redaction import (
    RedactionRegistry,
    redact_log_text as shared_redact_log_text,
    register_proxy_for_redaction as shared_register_proxy_for_redaction,
)
from common.proxy_bookkeeping import (
    is_known_static_host as shared_is_known_static_host,
    proxy_key as shared_proxy_key,
    proxy_label as shared_proxy_label,
    proxy_short as shared_proxy_short,
    token_key_name as shared_token_key_name,
)
from common.proxy_selection import (
    build_attempt_batches as shared_build_attempt_batches,
    pick_random_proxies as shared_pick_random_proxies,
    proxy_for_country as shared_proxy_for_country,
)
from common.proxy_url import (
    USER_FIRST,
    default_scheme_from_env as shared_default_scheme_from_env,
    normalize_proxy_url as shared_normalize_proxy_url,
)
from common.logging_setup import append_log_line, make_file_logger
from common import geo as shared_geo
from common.timeouts import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT
from common import stripe_flow as shared_stripe_flow
from common import endpoints

LOG_DIR = Path(os.environ.get("IDEAL_LOG_DIR", "").strip() or (SCRIPT_DIR / "logs"))
DUMP_DIR = Path(os.environ.get("IDEAL_DUMP_DIR", "").strip() or (SCRIPT_DIR / "dumps"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
# ``PP_STRIPE_PUBLISHABLE_KEY`` is the documented override (README,
# docs/TROUBLESHOOTING.md, sms_tool/pp_link_helpers.py).  The legacy
# ``STRIPE_PUBLISHABLE_KEY`` stays as a fallback: nothing sets it and nothing
# documents it, but this module read it historically, so dropping it would be a
# silent behaviour change for anyone who did.
DEFAULT_STRIPE_PK = (
    os.environ.get("PP_STRIPE_PUBLISHABLE_KEY", "") or os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
).strip()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

COUNTRY_CURRENCY = {
    "NL": "EUR",
    "BE": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "PL": "PLN",
    "US": "USD",
    "IN": "INR",
    "JP": "JPY",
}

BLIK_PROXY_COUNTRY = "PL"

DEFAULT_IDEAL_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Daan de Vries",
    "country": "NL",
    "line1": "Damrak 1",
    "line2": "",
    "city": "Amsterdam",
    "postal_code": "1012 LG",
    "state": "",
}

DEFAULT_BLIK_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Jan Kowalski",
    "country": "PL",
    "line1": "Marszalkowska 1",
    "line2": "",
    "city": "Warszawa",
    "postal_code": "00-001",
    "state": "",
}

NL_BILLING_NAMES = [
    ("Daan", "de Vries"),
    ("Sem", "Jansen"),
    ("Milan", "Bakker"),
    ("Lars", "Visser"),
    ("Sophie", "Smit"),
    ("Emma", "Meijer"),
    ("Tess", "Mulder"),
    ("Nina", "de Boer"),
]

NL_BILLING_ADDRESSES = [
    ("Damrak 1", "Amsterdam", "1012 LG"),
    ("Kalverstraat 92", "Amsterdam", "1012 PH"),
    ("Coolsingel 40", "Rotterdam", "3011 AD"),
    ("Lijnbaan 50", "Rotterdam", "3012 EP"),
    ("Grote Marktstraat 43", "Den Haag", "2511 BH"),
    ("Oudegracht 120", "Utrecht", "3511 AW"),
    ("Stationsplein 1", "Eindhoven", "5611 AB"),
    ("Vismarkt 10", "Groningen", "9711 KV"),
]

PL_BILLING_NAMES = [
    ("Jan", "Kowalski"),
    ("Piotr", "Nowak"),
    ("Anna", "Wisniewska"),
    ("Katarzyna", "Wojcik"),
    ("Tomasz", "Kaminski"),
    ("Marta", "Lewandowska"),
]

PL_BILLING_ADDRESSES = [
    ("Marszalkowska 1", "Warszawa", "00-001"),
    ("Nowy Swiat 15", "Warszawa", "00-029"),
    ("Florianska 10", "Krakow", "31-021"),
    ("Dluga 5", "Gdansk", "80-827"),
    ("Piotrkowska 20", "Lodz", "90-269"),
    ("Rynek 1", "Wroclaw", "50-101"),
]

EMAIL_DOMAINS = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")

_log_file = LOG_DIR / f"blik_{time.strftime('%Y%m%d-%H%M%S')}.log"
# Durable rotated copy alongside the legacy append-only file above. The legacy
# file keeps its exact naming/content; this channel only adds a size-capped,
# rotation-resilient mirror. It writes to files only, never stdout.
_durable_logger = make_file_logger("blik", LOG_DIR, redact=lambda text: redact_log_text(text))
_dump_counter = 0
#: Owned by ``_proxy_state_store`` below; the lock is still shared with the
#: ``common.geo`` cache writers so country/target updates serialize cleanly.
_proxy_state_lock = RLock()
_log_lock = RLock()
_dump_lock = RLock()
_proxy_file_lock = RLock()
_proxy_redaction_registry = RedactionRegistry()
_log_context = local()


def redact_log_text(text: str) -> str:
    """Delegate to the shared redactor (batch 2 of the consolidation).

    ``registry`` is this module's own: it holds the proxies THIS extractor
    registered.  A single shared registry would make one extractor's log output
    depend on another extractor's registrations, and would leak one
    extractor's proxy strings into another's dump files.
    """
    return shared_redact_log_text(
        text,
        registry=_proxy_redaction_registry,
        proxy_label=proxy_label,
    )


def log(message: str, prefix: str = "") -> None:
    context = getattr(_log_context, "prefix", "")
    line = redact_log_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{context}{message}")
    with _log_lock:
        print(line, flush=True)
        # File logging mirrors stdout; an unwritable file must not abort the run.
        append_log_line(_log_file, line)
        _durable_logger.info("%s%s%s", prefix, context, message)


def env_bool(name: str, default: bool = False) -> bool:
    return common_env_bool(name, default)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    return common_env_int(name, default, minimum)


def is_checkout_not_active_error(value: Any) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_is_checkout_not_active_error(value)


def random_user_agent() -> str:
    major = random.randint(122, 149)
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def random_runtime_version() -> str:
    return os.environ.get("PP_RUNTIME_VERSION", "").strip() or DEFAULT_STRIPE_RUNTIME_VERSION


def build_email(first_name: str, last_name: str) -> str:
    first = re.sub(r"[^a-z]", "", first_name.lower())
    last = re.sub(r"[^a-z]", "", last_name.lower())
    suffix = random.randint(10000, 999999)
    domain = random.choice(EMAIL_DOMAINS)
    if random.random() < 0.5:
        local = f"{first}.{last}{suffix}"
    else:
        local = f"{first}{last}{suffix}"
    return f"{local}@{domain}"


def normalize_country(country: str) -> str:
    value = str(country or "").strip().upper()
    return value if value in COUNTRY_CURRENCY else default_payment_country()


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(normalize_country(country), "EUR")


def payment_browser_locale() -> str:
    default_locale = "pl-PL" if payment_method_type() == "blik" else "en-US"
    return os.environ.get("IDEAL_BROWSER_LOCALE", default_locale).strip() or default_locale


def payment_elements_locale() -> str:
    return os.environ.get("IDEAL_ELEMENTS_LOCALE", payment_browser_locale()).strip() or payment_browser_locale()


def payment_browser_timezone() -> str:
    default_timezone = "Europe/Warsaw" if payment_method_type() == "blik" else "Europe/Amsterdam"
    return os.environ.get("IDEAL_BROWSER_TIMEZONE", default_timezone).strip() or default_timezone


def saved_payment_value() -> str:
    return os.environ.get("IDEAL_SAVED_PAYMENT_VALUE", "never").strip() or "never"


def payment_method_type() -> str:
    value = os.environ.get("IDEAL_PAYMENT_METHOD", "blik").strip().lower()
    return value if value in {"ideal", "blik"} else "ideal"


def current_payment_method_label() -> str:
    return "BLIK" if payment_method_type() == "blik" else "iDEAL"


def default_payment_country() -> str:
    return "PL" if payment_method_type() == "blik" else "NL"


def default_checkout_proxy_country() -> str:
    return BLIK_PROXY_COUNTRY if payment_method_type() == "blik" else "JP"


def default_provider_proxy_countries() -> str:
    return "PL" if payment_method_type() == "blik" else "NL,BE,DE,FR,IE,ES,IT,AT,LU"


def provider_country_label() -> str:
    label = os.environ.get("IDEAL_PROVIDER_COUNTRY_LABEL", "").strip()
    if label:
        return label
    raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRIES", "").strip()
    if raw:
        return raw
    raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRY", "").strip()
    if raw:
        return raw
    return default_provider_proxy_countries()


_result_reporter = ProtocolResultReporter(payment_method_type)


def print_result_url(url: str) -> None:
    if _result_reporter.emitted:
        return
    if payment_method_type() == "blik" and os.environ.get("IDEAL_BLIK_CODE", "").strip():
        print("BLIK 自动提交完成")
        # 结构化完成哨兵：BLIK 自动提交模式直接向 Stripe 提交 6 位码完成支付，产物是
        # “支付已完成”状态而非可分享 URL。payment_link_manager 依赖此行判定成功，
        # 避免从截断日志里误抓/误判 URL。
        _result_reporter.success(
            "",
            operation="execute_payment",
            link_type="blik_protocol_completed",
            message="BLIK 自动提交完成",
            side_effect_started=True,
            prefix="BLIK_RESULT:",
        )
        return
    _result_reporter.success(url)


def print_failure_result(
    error: Any,
    *,
    status: str = "failed",
    error_code: str = "extractor_failed",
    retryable: bool = False,
) -> None:
    """Print the terminal failure contract so the manager never scrapes logs."""
    _result_reporter.failure(
        error,
        status=status,
        error_code=error_code,
        retryable=retryable,
    )


def print_already_paid_result() -> None:
    _result_reporter.already_paid()


def validate_blik_code_before_start() -> bool:
    if payment_method_type() != "blik":
        return True
    code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if not code:
        log("BLIK Code 为空，任务已结束；请先填写 6 位 BLIK Code", "[ERROR] ")
        return False
    if not re.fullmatch(r"\d{6}", code):
        log("BLIK Code 必须是6位数字，任务已结束", "[ERROR] ")
        return False
    return True


def payment_accept_language() -> str:
    locale = payment_browser_locale()
    if locale.lower().startswith("en"):
        return "en-US,en;q=0.9"
    return f"{locale},{locale.split('-', 1)[0]};q=0.9,en;q=0.8"


def normalize_proxy_url(proxy: str) -> str:
    """Delegate to the shared skeleton in ``common/proxy_url.py``.

    ``four_part=USER_FIRST`` records blik's inverse four-field convention
    (``user:pass:host:port``), which disagrees with the ``host:port:user:pass``
    order used by kakao / direct_card / ac_paylink_core and by the host-side
    authority ``sms_tool/proxy_entry.parse_proxy``.  The disagreement is
    preserved here rather than silently unified -- see the module docstring of
    ``common/proxy_url.py``.  ``runtime/tmp/p0_proxy_diff.py`` pins it.
    """
    return shared_normalize_proxy_url(proxy, default_scheme=default_proxy_scheme(), four_part=USER_FIRST)


def register_proxy_for_redaction(proxy: str) -> None:
    """Delegate to the shared registrar (batch 2 of the consolidation).

    ``normalize`` is this module's own normaliser and ``registry`` is this
    module's own value set -- see ``redact_log_text`` for why neither is
    shared.
    """
    shared_register_proxy_for_redaction(
        proxy,
        registry=_proxy_redaction_registry,
        normalize=normalize_proxy_url,
    )


def default_proxy_scheme() -> str:
    """Delegate to the shared resolver in ``common/proxy_url.py``.

    ``allow_https`` defaults to true here, so ``IDEAL_PROXY_DEFAULT_SCHEME=https``
    survives -- unlike kakao, which collapses it to ``http``.
    """
    return shared_default_scheme_from_env("IDEAL_PROXY_DEFAULT_SCHEME")


def proxy_short(proxy: str) -> str:
    """Delegate to ``common/proxy_bookkeeping.py``.

    The host-side body is shared; the normaliser is injected so blik keeps its
    ``user:pass:host:port`` reading while the others keep ``host:port:user:pass``
    (see ``common/proxy_url.py``).
    """
    return shared_proxy_short(proxy, normalize_proxy_url)


def proxy_label(proxy: str) -> str:
    """Delegate to ``common/proxy_bookkeeping.py`` (historical alias of proxy_short)."""
    return shared_proxy_label(proxy, normalize_proxy_url)


def proxy_key(proxy: str) -> str:
    """Delegate to ``common/proxy_bookkeeping.py``."""
    return shared_proxy_key(proxy, normalize_proxy_url)


#: Rewritable country selector inside a proxy's username/password.
#:
#: ``geo`` is included because the configured payment egress provider (9http)
#: spells it ``<user>-geo-US-sid-...``. Without it the rewriter matched nothing,
#: ``proxy_for_country`` raised "代理未包含可改写的 country/region 选择器", and
#: every method that derives a country exit failed before its first HTTP call
#: (measured 2026-10-06). This constant is byte-identical in blik / ideal / kakao
#: / twint -- keep it that way, or ``sunk_copy_parity`` and the next reader will
#: both be right to complain.
#:
#: ⚠️ A provider that does not serve the requested country can answer with a
#: *different* one rather than failing: ``geo-PL`` resolved to ``MX``, not ``PL``.
#: Rewriting therefore does not prove the exit came out where you asked; the
#: egress gate or ``resolve_proxy_geo`` has to confirm it.
#: ``_PROXY_COUNTRY_SELECTOR_RE`` and ``proxy_chain_key`` now live in
#: ``common/extractor_runtime.py`` (identical in ideal/twint/blik).


def proxy_for_country(proxy: str, country: str) -> str:
    """Delegate to the shared rewriter (batch 5 of the consolidation).

    Every knob is this module's own: the proxy normaliser, the country
    normaliser (whose fallback differs per provider), the selector pattern --
    which this module also uses for ``proxy_chain_key`` -- the redaction label
    used in the error text, and the registrar that keeps the *derived* proxy out
    of the logs.  The derived proxy is a new credential, not a restatement of
    the seed, so registering it is not optional.
    """
    return shared_proxy_for_country(
        proxy,
        country,
        normalize=normalize_proxy_url,
        country_normalizer=normalize_country,
        selector=_PROXY_COUNTRY_SELECTOR_RE,
        label=proxy_label,
        register=register_proxy_for_redaction,
    )


def blik_proxy_chain(proxy_seed: str) -> tuple[str, str]:
    """Use one sticky seed for all BLIK stages, with a PL country selector."""
    checkout_proxy = proxy_for_country(proxy_seed, BLIK_PROXY_COUNTRY)
    provider_proxy = proxy_for_country(proxy_seed, BLIK_PROXY_COUNTRY)
    chain_key = proxy_chain_key(proxy_seed)
    if not chain_key or any(proxy_chain_key(proxy) != chain_key for proxy in (checkout_proxy, provider_proxy)):
        raise RuntimeError("代理地区改写改变了 sticky seed，已拒绝混用代理链")
    return checkout_proxy, provider_proxy


def log_blik_proxy_chain(proxy_seed: str, checkout_proxy: str, provider_proxy: str) -> None:
    log(
        "派生代理链: "
        f"chain={proxy_chain_key(proxy_seed)}; seed={proxy_label(proxy_seed)}; "
        f"PL checkout={proxy_label(checkout_proxy)}; "
        f"PL Stripe/approve={proxy_label(provider_proxy)}"
    )


_proxy_state_store = ProxyStateStore(
    env_prefix="IDEAL",
    base_dir=SCRIPT_DIR,
    proxy_key=proxy_key,
    proxy_chain_key=proxy_chain_key,
    env_bool=env_bool,
    env_int=env_int,
    normalize_country=normalize_country,
    lock=_proxy_state_lock,
    log=log,
)

_proxy_seed_file = ProxySeedFile(
    env_prefix="IDEAL",
    base_dir=SCRIPT_DIR,
    proxy_chain_key=proxy_chain_key,
    label=proxy_label,
    redact=redact_log_text,
    register=register_proxy_for_redaction,
    env_bool=env_bool,
    log=log,
    file_lock=_proxy_file_lock,
)


def proxy_state_path() -> Path:
    """Delegate to ``common/proxy_state.py``."""
    return shared_proxy_state_path(_proxy_state_store)


def load_proxy_state() -> dict[str, Any]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_load_proxy_state(_proxy_state_store)


def save_proxy_state() -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_save_proxy_state(_proxy_state_store)


def proxy_state_key(group: str, proxy: str) -> str:
    """Delegate to ``common/proxy_state.py``."""
    return shared_proxy_state_key(_proxy_state_store, group, proxy)


def prune_proxy_state(checkout_proxies: list[str], provider_proxies: list[str]) -> None:
    removed_counts: dict[str, int] = {}
    with _proxy_state_lock:
        state = load_proxy_state()
        active_keys_by_group: dict[str, set[str]] = {}
        for group, proxies in (("checkout", checkout_proxies), ("provider", provider_proxies)):
            group_state = state.get(group)
            if not isinstance(group_state, dict):
                continue
            active_keys = {proxy_key(proxy) for proxy in proxies if proxy}
            active_keys_by_group[group] = active_keys
            stale_keys = [key for key in group_state if key not in active_keys]
            for key in stale_keys:
                del group_state[key]
            if stale_keys:
                removed_counts[group] = len(stale_keys)
        pair_state = state.get("pair")
        if isinstance(pair_state, dict):
            active_checkout = active_keys_by_group.get("checkout", set())
            active_provider = active_keys_by_group.get("provider", set())
            stale_pair_keys = [
                key
                for key, record in pair_state.items()
                if not isinstance(record, dict)
                or record.get("checkout") not in active_checkout
                or record.get("provider") not in active_provider
            ]
            for key in stale_pair_keys:
                del pair_state[key]
            if stale_pair_keys:
                removed_counts["pair"] = len(stale_pair_keys)
        if removed_counts:
            save_proxy_state()
    if removed_counts:
        summary = ", ".join(f"{group}={count}" for group, count in removed_counts.items())
        log(f"代理状态清理完成: {summary}")


def prune_proxy_seed_state(proxy_seeds: list[str]) -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_prune_proxy_seed_state(_proxy_state_store, proxy_seeds)


def proxy_record(group: str, proxy: str) -> dict[str, Any]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_proxy_record(_proxy_state_store, group, proxy)


def proxy_pair_key(checkout_proxy: str, provider_proxy: str) -> str:
    """Delegate to ``common/proxy_state.py``."""
    return shared_proxy_pair_key(_proxy_state_store, checkout_proxy, provider_proxy)


def clean_country_code(value: str) -> str:
    return shared_geo.clean_country_code(value)


def proxy_country_cache_ttl() -> int:
    return env_int("IDEAL_PROXY_GEO_CACHE_TTL", 3600)


def geo_lookup_urls() -> list[tuple[str, str]]:
    return shared_geo.geo_lookup_urls()


def parse_geo_country(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    return shared_geo.parse_geo_country(source, payload)


def lookup_proxy_country(group: str, proxy: str, timeout: int | None = None) -> tuple[str, str, str]:
    return shared_geo.lookup_proxy_country(
        group,
        proxy,
        timeout,
        record=proxy_record(group, proxy),
        save_state=save_proxy_state,
        new_session=new_session,
        env_bool=env_bool,
        env_int=env_int,
        redact=redact_log_text,
        env_prefix="IDEAL",
    )


def proxy_target_cache_ttl() -> int:
    return env_int("IDEAL_PROXY_TARGET_CACHE_TTL", 1800, minimum=0)


def target_probe_urls(group: str) -> list[tuple[str, str]]:
    return shared_geo.target_probe_urls(group)


def target_response_error(resp: Any) -> str:
    return shared_geo.target_response_error(resp)


def lookup_proxy_targets(group: str, proxy: str, timeout: int | None = None) -> tuple[bool, str]:
    return shared_geo.lookup_proxy_targets(
        group,
        proxy,
        timeout,
        record=proxy_record(group, proxy),
        save_state=save_proxy_state,
        new_session=new_session,
        env_bool=env_bool,
        env_int=env_int,
        redact=redact_log_text,
        env_prefix="IDEAL",
        user_agent=DEFAULT_USER_AGENT,
    )


def expected_proxy_country(group: str) -> str:
    if group == "checkout":
        raw = os.environ.get("IDEAL_CHECKOUT_PROXY_COUNTRY", default_checkout_proxy_country())
    else:
        raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRY", default_payment_country())
    return clean_country_code(raw)


def expected_proxy_countries(group: str) -> set[str]:
    return shared_geo.expected_proxy_countries(
        group,
        env_prefix="IDEAL",
        default_checkout_country=default_checkout_proxy_country,
        default_country=default_payment_country,
        default_provider_countries=default_provider_proxy_countries,
    )


def format_expected_countries(countries: set[str]) -> str:
    return shared_geo.format_expected_countries(countries)


def ensure_proxy_country(group: str, proxy: str) -> None:
    return shared_geo.ensure_proxy_country(
        group,
        proxy,
        lookup_country=lookup_proxy_country,
        expected_countries=expected_proxy_countries,
        env_bool=env_bool,
        log=log,
        label=proxy_label,
        remove_failed=remove_failed_proxy,
        env_prefix="IDEAL",
    )


def ensure_proxy_targets(group: str, proxy: str) -> None:
    return shared_geo.ensure_proxy_targets(
        group,
        proxy,
        lookup_targets=lookup_proxy_targets,
        env_bool=env_bool,
        log=log,
        label=proxy_label,
        env_prefix="IDEAL",
    )


def precheck_proxy_group(group: str, proxies: list[str]) -> list[str]:
    return shared_geo.precheck_proxy_group(
        group,
        proxies,
        lookup_country=lookup_proxy_country,
        lookup_targets=lookup_proxy_targets,
        expected_countries=expected_proxy_countries,
        env_bool=env_bool,
        env_int=env_int,
        log=log,
        record_health_failure=record_proxy_health_failure,
        remove_failed_proxies=remove_failed_proxies,
        env_prefix="IDEAL",
    )


def record_proxy_result(group: str, proxy: str, success: bool, reason: str = "") -> dict[str, Any]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_record_proxy_result(_proxy_state_store, group, proxy, success, reason)


def proxy_remove_after_fails() -> int:
    """Delegate to ``common/proxy_state.py``."""
    return shared_proxy_remove_after_fails(_proxy_state_store)


def is_reused_proxy_record(group: str, record: dict[str, Any]) -> bool:
    """Delegate to ``common/proxy_state.py``."""
    return shared_is_reused_proxy_record(_proxy_state_store, record)


def record_proxy_health_failure(group: str, proxy: str, reason: str) -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_record_proxy_health_failure(
        _proxy_state_store,
        group,
        proxy,
        reason,
        remove_failed_proxy,
    )


def checkout_zero_cache_ttl() -> int:
    """Delegate to ``common/proxy_state.py``."""
    return shared_checkout_zero_cache_ttl(_proxy_state_store)


def checkout_zero_cache_status(proxy: str, country: str) -> tuple[str, int, int]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_checkout_zero_cache_status(_proxy_state_store, proxy, country)


def record_checkout_zero_result(proxy: str, country: str, amount: int) -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_record_checkout_zero_result(_proxy_state_store, proxy, country, amount)


def record_proxy_pair_result(checkout_proxy: str, provider_proxy: str, success: bool, reason: str = "") -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_record_proxy_pair_result(
        _proxy_state_store,
        checkout_proxy,
        provider_proxy,
        success,
        reason,
    )


def record_proxy_pair_approve_success(checkout_proxy: str, provider_proxy: str, approve_proxy: str) -> None:
    """Delegate to ``common/proxy_state.py``."""
    return shared_record_proxy_pair_approve_success(
        _proxy_state_store,
        checkout_proxy,
        provider_proxy,
        approve_proxy,
    )


def successful_approve_preferences(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_successful_approve_preferences(_proxy_state_store, checkout_proxy, provider_proxy, approve_pool)


def record_failure_by_stage(reason: str, checkout_proxy: str, provider_proxy: str) -> None:
    if "checkout 代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("checkout", checkout_proxy, reason)
        else:
            record_proxy_result("checkout", checkout_proxy, False, reason)
        return
    if "checkout 代理目标站不可达" in reason:
        record_proxy_health_failure("checkout", checkout_proxy, reason)
        return
    if "provider 代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("provider", provider_proxy, reason)
        else:
            record_proxy_result("provider", provider_proxy, False, reason)
        return
    if "provider 代理目标站不可达" in reason:
        record_proxy_health_failure("provider", provider_proxy, reason)
        return
    if "checkout 阶段失败" in reason or "checkout 创建失败" in reason:
        if is_direct_remove_proxy_error(reason):
            remove_failed_proxy("checkout", checkout_proxy, reason)
            record_proxy_result("checkout", checkout_proxy, False, reason)
        elif is_proxy_health_failure(reason):
            record_proxy_health_failure("checkout", checkout_proxy, reason)
        return
    if "当前 checkout 不支持" in reason:
        return
    if "0 元优惠未生效" in reason:
        return
    if "approve blocked" in reason:
        return
    if is_direct_remove_proxy_error(reason):
        remove_failed_proxy("provider", provider_proxy, reason)
        record_proxy_result("provider", provider_proxy, False, reason)
    elif is_proxy_health_failure(reason):
        record_proxy_health_failure("provider", provider_proxy, reason)
    else:
        record_proxy_result("provider", provider_proxy, False, reason)


def record_seed_failure(proxy_seed: str, reason: str) -> None:
    """Persist one BLIK chain result against its original sticky seed."""
    if not proxy_seed:
        return
    if is_checkout_not_active_error(reason) or "当前 checkout 不支持" in reason:
        return
    if "0 元优惠未生效" in reason or "approve blocked" in reason:
        return
    if "代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("seed", proxy_seed, reason)
        record_proxy_result("seed", proxy_seed, False, reason)
        return
    if is_direct_remove_proxy_error(reason):
        remove_failed_proxy("seed", proxy_seed, reason)
        record_proxy_result("seed", proxy_seed, False, reason)
        return
    if is_proxy_health_failure(reason):
        record_proxy_health_failure("seed", proxy_seed, reason)
        return
    record_proxy_result("seed", proxy_seed, False, reason)


def order_proxy_group(group: str, proxies: list[str]) -> list[str]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_order_proxy_group(_proxy_state_store, group, proxies)


def set_proxy(session: Any, proxy: str) -> None:
    register_proxy_for_redaction(proxy)
    proxy = normalize_proxy_url(proxy)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}


def pre_proxy_url() -> str:
    """本机前置代理：本机代理 -> 文件代理 -> 目标站。"""
    for name in ("IDEAL_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"):
        if name in os.environ:
            raw = os.environ.get(name, "").strip()
            if raw.lower() in {"", "0", "off", "none", "direct", "disabled"}:
                return ""
            proxy = normalize_pre_proxy_url(raw)
            register_proxy_for_redaction(proxy)
            return proxy
    return ""


def load_proxy_file(path: Path) -> list[str]:
    """Delegate to the shared loader (batch 3 of the consolidation).

    The two injected knobs are this module's own: redaction registration writes
    into *this* extractor's registry, and the normaliser is the one that knows
    blik's four-field convention.
    """
    return shared_load_proxy_file(
        path,
        register_for_redaction=register_proxy_for_redaction,
        normalize=normalize_proxy_url,
    )


def proxy_seed_file() -> Path:
    """Delegate to ``common/proxy_seed_file.py``."""
    return shared_proxy_seed_file(_proxy_seed_file)


def unique_proxy_seeds(proxy_seeds: list[str]) -> list[str]:
    """Delegate to ``common/proxy_seed_file.py``."""
    return shared_unique_proxy_seeds(_proxy_seed_file, proxy_seeds)


def load_proxy_seeds() -> list[str]:
    if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False):
        local_proxy = pre_proxy_url()
        if not local_proxy:
            raise RuntimeError("本地代理直连模式需要设置 IDEAL_PRE_PROXY")
        log("本地代理直连模式: 单一出口用于 BLIK 全流程")
        return [local_proxy]

    path = proxy_seed_file()
    if not path.is_file():
        raise RuntimeError("代理 Seed 文件不存在")
    proxy_seeds = unique_proxy_seeds(load_proxy_file(path))
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 为空")
    prune_proxy_seed_state(proxy_seeds)
    proxy_seeds = order_proxy_group("seed", proxy_seeds)
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 已全部处于失败冷却")
    log(f"加载代理 Seed {len(proxy_seeds)} 条")
    log("严格代理策略: 每轮取一条 seed，派生 PL Checkout → PL Stripe/BLIK → PL approve")
    log(f"裸代理默认协议: {default_proxy_scheme()}://")
    log(f"本机前置代理: {proxy_label(pre_proxy_url())}")
    return proxy_seeds


def first_existing_file(paths: list[str | Path]) -> Path:
    for item in paths:
        if not item:
            continue
        path = Path(item)
        if path.exists():
            return path
    return Path("")


def proxy_file_for_group(group: str) -> Path:
    if (
        group == "seed"
        or os.environ.get("IDEAL_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    ):
        return proxy_seed_file()
    if group == "checkout":
        candidates = [
            os.environ.get("IDEAL_CHECKOUT_PROXY_FILE", "").strip(),
            os.environ.get("PP_CHECKOUT_PROXY_FILE", "").strip(),
            SCRIPT_DIR / "checkout.json",
        ]
    else:
        candidates = [
            os.environ.get("IDEAL_PROVIDER_PROXY_FILE", "").strip(),
            os.environ.get("PP_PROVIDER_PROXY_FILE", "").strip(),
            SCRIPT_DIR / "provider.json",
        ]
    return first_existing_file(candidates)


def is_direct_remove_proxy_error(reason: str) -> bool:
    """Delegate to ``common/extractor_helpers.py`` (batch 5)."""
    return shared_is_direct_remove_proxy_error(reason)


def is_proxy_health_failure(reason: str) -> bool:
    """Delegate to ``common/extractor_helpers.py`` (batch 5)."""
    return shared_is_proxy_health_failure(reason)


def remove_failed_proxies(group: str, failures: list[tuple[str, str]]) -> int:
    if not failures or not env_bool("IDEAL_PROXY_REMOVE_FAILED", True):
        return 0
    path = proxy_file_for_group(group)
    if not str(path) or not path.exists():
        return 0
    seed_mode = group == "seed" or bool(
        os.environ.get("IDEAL_PROXY_SEED_FILE", "").strip() or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    )
    key_for_proxy = proxy_chain_key if seed_mode else normalize_proxy_url
    reasons = {key_for_proxy(proxy): reason for proxy, reason in failures if key_for_proxy(proxy)}
    if not reasons:
        return 0
    with _proxy_file_lock:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        removed = [line for line in lines if key_for_proxy(line) in reasons]
        if not removed:
            return 0
        kept = [line for line in lines if key_for_proxy(line) not in reasons]
        quarantine = SCRIPT_DIR / "removed_proxies.jsonl"
        try:
            with open(quarantine, "a", encoding="utf-8") as f:
                for line in removed:
                    normalized = key_for_proxy(line)
                    f.write(
                        json.dumps(
                            {
                                "time": int(time.time()),
                                "group": group,
                                "proxy": proxy_label(line.strip()),
                                "reason": redact_log_text(str(reasons.get(normalized) or ""))[:300],
                                "source": path.name,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError as exc:
            # Abort before rewriting the proxy file: removing proxies without the
            # quarantine audit trail is worse than failing this rotation.
            raise RuntimeError(f"无法写入代理隔离记录 {quarantine.name}: {exc}") from exc
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text("".join(kept), encoding="utf-8")
        os.replace(temp_path, path)
        return len(removed)


def remove_failed_proxy(group: str, proxy: str, reason: str) -> bool:
    return remove_failed_proxies(group, [(proxy, reason)]) > 0


def load_proxy_groups() -> tuple[list[str], list[str]]:
    if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False):
        local_proxy = pre_proxy_url()
        if not local_proxy:
            raise RuntimeError("本地代理直连模式需要设置 IDEAL_PRE_PROXY")
        log(f"本地代理直连模式: checkout/provider 均使用 {proxy_label(local_proxy)}")
        return [local_proxy], [local_proxy]

    checkout_file = proxy_file_for_group("checkout")
    provider_file = proxy_file_for_group("provider")
    checkout_proxies = load_proxy_file(checkout_file) if str(checkout_file) else []
    provider_proxies = load_proxy_file(provider_file) if str(provider_file) else []
    if not checkout_proxies:
        raise RuntimeError("Checkout 代理为空：需要 checkout.json")
    if not provider_proxies:
        raise RuntimeError("Provider 代理为空：approve 阶段需要 provider.json")
    prune_proxy_state(checkout_proxies, provider_proxies)
    checkout_proxies = order_proxy_group("checkout", checkout_proxies)
    provider_proxies = order_proxy_group("provider", provider_proxies)
    if not checkout_proxies:
        raise RuntimeError("Checkout 代理已全部被失败状态过滤")
    if not provider_proxies:
        raise RuntimeError("Provider 代理已全部被失败状态过滤")
    log(f"加载 Checkout 代理 {len(checkout_proxies)} 条: {checkout_file}")
    log(f"加载 Provider 代理 {len(provider_proxies)} 条: {provider_file}")
    log(f"代理策略: checkout/0元资格/approve 用 Checkout；Stripe/{current_payment_method_label()} 用 Provider")
    log(f"裸代理默认协议: {default_proxy_scheme()}://")
    log(f"本机前置代理: {proxy_label(pre_proxy_url())}")
    log(
        "代理检测前置策略: "
        f"国家检测={'on' if env_bool('IDEAL_PROXY_GEO_USE_PRE_PROXY', False) else 'off'}，"
        f"目标站检测={'on' if env_bool('IDEAL_PROXY_TARGET_USE_PRE_PROXY', True) else 'off'}"
    )
    checkout_proxies = precheck_proxy_group("checkout", checkout_proxies)
    provider_proxies = precheck_proxy_group("provider", provider_proxies)
    log(f"预筛后 Checkout 代理 {len(checkout_proxies)} 条")
    log(f"预筛后 Provider 代理 {len(provider_proxies)} 条")
    return checkout_proxies, provider_proxies


def new_session(proxy: str = "", use_pre_proxy: bool = True) -> Any:
    pre_proxy = "" if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False) else (pre_proxy_url() if use_pre_proxy else "")
    register_proxy_for_redaction(pre_proxy)
    if CurlCffiSession is not None:
        kwargs: dict[str, Any] = {"impersonate": "chrome136"}
        if pre_proxy:
            if CurlOpt is None:
                raise RuntimeError("本机前置代理需要 curl_cffi 支持")
            kwargs["curl_options"] = {CurlOpt.PRE_PROXY: pre_proxy}
        session = CurlCffiSession(**kwargs)
    else:
        if pre_proxy:
            raise RuntimeError("本机前置代理需要 curl_cffi：python3 -m pip install curl_cffi")
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy:
        set_proxy(session, proxy)
    return session


def _redact_text(text: str, limit: int | None = None) -> str:
    text = text or ""
    blik_code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if blik_code:
        text = text.replace(blik_code, "***")
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._=-]+", r"\1***", text)
    text = re.sub(r"(__Secure-next-auth\.session-token=)[^;\\s]+", r"\1***", text)
    text = re.sub(r"(accessToken|access_token|sessionToken|token)(['\"]?\s*[:=]\s*['\"])[^'\"]+", r"\1\2***", text)
    text = re.sub(r"(?i)(blik_code['\"]?\s*[:=]\s*['\"]?)\d{6}", r"\1***", text)
    text = redact_log_text(text)
    if limit is None:
        limit = env_int("IDEAL_DUMP_LIMIT", 6000, minimum=500)
    return text[:limit]


def dump_http(
    response: requests.Response | None,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> None:
    if not env_bool("IDEAL_DUMP", False):
        return
    global _dump_counter
    with _dump_lock:
        _dump_counter += 1
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{_dump_counter:04d}_{stage}.txt"
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = DUMP_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    lines = [
        f"stage: {stage}",
        f"request: {request_method} {request_url}",
        "",
        "request_body:",
        _redact_text(json.dumps(request_body, ensure_ascii=False, indent=2) if request_body is not None else ""),
        "",
    ]
    if response is not None:
        lines.extend(
            [
                f"status: {response.status_code}",
                f"url: {response.url}",
                "",
                "response:",
                _redact_text(response.text),
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def token_key_name(value: Any) -> str:
    """Delegate to ``common/proxy_bookkeeping.py``."""
    return shared_token_key_name(value)


def collect_strings(payload: Any, result: list[str] | None = None) -> list[str]:
    return common_collect_strings(payload, result)


def load_token() -> tuple[str, str]:
    """Delegate to the shared loader (batch 3 of the consolidation).

    ``env_names`` is passed explicitly.  It reuses ideal's ``IDEAL_TOKEN`` name,
    which looks like a copy-paste slip -- but it is the shipped behaviour, and
    changing it here would change which env var blik honours.  If that is ever
    intended to change, change it as its own unit of work with its own
    verification; do not "tidy" it during a refactor.
    """
    return shared_load_token(
        env_names=("PP_TOKEN", "IDEAL_TOKEN"),
        token_file=SCRIPT_DIR / "token.txt",
        normalize_token=normalize_token,
        log=log,
    )


def build_chatgpt_session(access_token: str, device_id: str, proxy: str, session_token: str = "") -> requests.Session:
    session = new_session(proxy)
    cookie = f"oai-did={device_id}"
    if session_token:
        cookie += f"; __Secure-next-auth.session-token={session_token}"
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": payment_accept_language(),
            "Authorization": f"Bearer {access_token}",
            "Origin": endpoints.CHATGPT_BASE,
            "Referer": endpoints.CHATGPT_BASE + "/",
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            "oai-language": payment_browser_locale(),
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": cookie,
        }
    )
    return session


def checkout_response_has_promo(payload: Any) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_checkout_response_has_promo(payload)


def checkout_response_has_trial(payload: Any) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_checkout_response_has_trial(payload)


def create_checkout(chatgpt: requests.Session, country: str) -> dict[str, str]:
    country = normalize_country(country)
    promo_mode = os.environ.get("PP_PROMO_MODE", "campaign").strip().lower() or "campaign"
    promo_id = os.environ.get("PP_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "entry_point": os.environ.get("PP_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency_for_country(country)},
        "checkout_ui_mode": "custom",
    }
    if promo_mode in ("trial", "free_trial"):
        trial_days = env_int("PP_TRIAL_DAYS", 30)
        body["subscription_data"] = {"trial_period_days": trial_days}
    elif promo_mode in ("campaign", "query"):
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": promo_mode == "query",
        }
    elif promo_mode == "coupon":
        body["coupon"] = promo_id
    elif promo_mode == "code":
        body["promotion_code"] = promo_id
    elif promo_mode != "off":
        log(f"未知 PP_PROMO_MODE={promo_mode!r}，已忽略", "[WARN] ")
    log(f"Checkout promo: mode={promo_mode}, id={promo_id}")

    headers = {
        "Referer": endpoints.CHATGPT_BASE + "/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    resp = chatgpt.post(
        endpoints.CHATGPT_CHECKOUT_API,
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout", body, "POST", endpoints.CHATGPT_CHECKOUT_API, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        if is_user_already_paid_error(resp.text):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json() or {}
    if (
        promo_mode == "coupon"
        and promo_id == "plus-1-month-free"
        and not checkout_response_has_promo(data)
        and env_bool("IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN", True)
    ):
        log("coupon 响应未显示优惠，按 promo_campaign 字符串重试", "[PROMO] ")
        fallback_body = dict(body)
        fallback_body.pop("coupon", None)
        fallback_body["promo_campaign"] = promo_id
        resp = chatgpt.post(
            endpoints.CHATGPT_CHECKOUT_API,
            json=fallback_body,
            headers=headers,
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(
            resp,
            "checkout_promo_campaign",
            fallback_body,
            "POST",
            endpoints.CHATGPT_CHECKOUT_API,
            force=True,
        )
        if resp.status_code >= 400:
            if is_user_already_paid_error(resp.text):
                raise RuntimeError("用户已支付: User is already paid")
            raise RuntimeError(f"checkout promo_campaign 重试失败 HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json() or {}
        log(f"promo_campaign 重试后 promo={checkout_response_has_promo(data)}", "[PROMO] ")

    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout 响应缺少 cs_id: {str(data)[:500]}")

    raw_pk = (
        data.get("stripe_publishable_key")
        or data.get("publishable_key")
        or data.get("publishableKey")
        or data.get("stripePublishableKey")
        or data.get("key")
        or ""
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
    stripe_pk = match.group(0) if match else DEFAULT_STRIPE_PK
    processor_entity = str(data.get("processor_entity") or data.get("processorEntity") or "")
    log(
        f"Checkout 创建成功: {cs_id} / {country} / {currency_for_country(country)} / "
        f"mode={promo_mode} / promo={checkout_response_has_promo(data)} / "
        f"trial={checkout_response_has_trial(data)}"
    )
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_pk": stripe_pk,
        "billing_country": country,
        "currency": currency_for_country(country),
    }


def stripe_init(cs_id: str, stripe_pk: str, proxy: str) -> dict[str, Any]:
    stripe = new_session(proxy)
    stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    body = {
        "browser_locale": payment_browser_locale(),
        "browser_timezone": payment_browser_timezone(),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": payment_elements_locale(),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = endpoints.stripe_payment_page(cs_id, "init")
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "stripe_init", body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"Stripe init 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json() or {}
    payload["client_stripe_js_id"] = stripe_js_id
    payload["client_elements_session_id"] = elements_session_id
    return payload


def amount_from_payload(payload: Any) -> int:
    return common_amount_from_payload(payload)


def build_ctx(init_payload: dict[str, Any], checkout: dict[str, str]) -> dict[str, Any]:
    raw_context: object = init_payload.get("_client_context")
    client_context = raw_context if isinstance(raw_context, dict) else {}
    return {
        "stripe_js_id": str(
            client_context.get("stripe_js_id") or init_payload.get("client_stripe_js_id") or uuid.uuid4()
        ),
        "client_session_id": str(uuid.uuid4()),
        "guid": stripe_browser_id(),
        "muid": stripe_browser_id(),
        "sid": stripe_browser_id(),
        "elements_session_id": str(
            init_payload.get("client_elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"
        ),
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": amount_from_payload(init_payload),
        "locale": payment_elements_locale(),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "eur").lower(),
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
    }


def stripe_elements_session_params(ctx: dict[str, Any]) -> dict[str, str]:
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx["elements_session_id"]),
        "elements_session_client[stripe_js_id]": str(ctx["stripe_js_id"]),
        "elements_session_client[locale]": str(ctx["locale"]),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
    }


def ideal_billing_profile() -> dict[str, str]:
    is_blik = payment_method_type() == "blik"
    first_name, last_name = random.choice(PL_BILLING_NAMES if is_blik else NL_BILLING_NAMES)
    line1, city, postal_code = random.choice(PL_BILLING_ADDRESSES if is_blik else NL_BILLING_ADDRESSES)
    profile = {
        "email": build_email(first_name, last_name),
        "name": f"{first_name} {last_name}",
        "country": "PL" if is_blik else "NL",
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal_code,
        "state": "",
    }
    if env_bool("IDEAL_USE_FIXED_BILLING", False):
        profile = dict(DEFAULT_BLIK_BILLING if is_blik else DEFAULT_IDEAL_BILLING)
    env_map = {
        "email": "IDEAL_EMAIL",
        "name": "IDEAL_NAME",
        "country": "IDEAL_BILLING_COUNTRY",
        "line1": "IDEAL_LINE1",
        "line2": "IDEAL_LINE2",
        "city": "IDEAL_CITY",
        "postal_code": "IDEAL_POSTAL_CODE",
        "state": "IDEAL_STATE",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            profile[key] = value
    profile["country"] = normalize_country(profile.get("country", "NL"))
    return profile


def stripe_update_customer_data(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "expected_amount": str(ctx.get("checkout_amount") or 0),
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "customer_data[email]": billing["email"],
        "customer_data[name]": billing["name"],
        "customer_data[address][country]": billing["country"],
        "customer_data[address][line1]": billing["line1"],
        "customer_data[address][city]": billing["city"],
        "customer_data[address][postal_code]": billing["postal_code"],
    }
    if billing.get("line2"):
        body["customer_data[address][line2]"] = billing["line2"]
    if billing.get("state"):
        body["customer_data[address][state]"] = billing["state"]

    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "customer_data_update_nl", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(f"荷兰 customer_data 已提交: {billing['name']} / {billing['city']} / {billing['postal_code']}")
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"荷兰 customer_data 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"荷兰 customer_data 提交异常: {exc}", "[WARN] ")
    return False


def stripe_update_tax_region(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
    }
    if billing.get("state"):
        body["tax_region[state]"] = billing["state"]

    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "tax_region_update", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(f"tax_region 已提交: {billing['country']} / {billing['city']} {billing['postal_code']}")
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"tax_region 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"tax_region 提交异常: {exc}", "[WARN] ")
    return False


def checkout_snapshot(chatgpt: requests.Session, checkout: dict[str, str], billing: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(
        checkout.get("billing_country", "NL"), checkout.get("processor_entity") or ""
    )
    checkout_page_url = endpoints.chatgpt_checkout_page(processor, cs_id)
    body = {
        "snapshot": {
            "billing_address": {
                "name": billing["name"],
                "address": {
                    "line1": billing["line1"],
                    "city": billing["city"],
                    "country": billing["country"],
                    "postal_code": billing["postal_code"],
                    "state": billing.get("state", ""),
                },
            }
        }
    }
    try:
        resp = chatgpt.post(
            endpoints.CHATGPT_CHECKOUT_SNAPSHOT,
            json=body,
            headers={
                "Referer": checkout_page_url,
                "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
                "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(
            resp,
            "checkout_snapshot",
            body,
            "POST",
            endpoints.CHATGPT_CHECKOUT_SNAPSHOT,
            force=env_bool("IDEAL_DUMP_WARMUP", False) or resp.status_code >= 400,
        )
        if resp.status_code >= 400:
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            log(f"checkout snapshot 失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
        else:
            log("checkout snapshot 已提交")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"checkout snapshot 异常: {exc}", "[WARN] ")


def stripe_create_ideal_pm(
    stripe: requests.Session, cs_id: str, stripe_pk: str, billing: dict[str, str], ctx: dict[str, Any]
) -> str:
    body: dict[str, Any] = {
        "billing_details[name]": billing.get("name") or "Jan de Vries",
        "billing_details[email]": billing.get("email") or "redacted@example.invalid",
        "billing_details[address][country]": billing.get("country") or "NL",
        "billing_details[address][line1]": billing.get("line1") or "Prinsengracht 263",
        "billing_details[address][city]": billing.get("city") or "Amsterdam",
        "billing_details[address][postal_code]": billing.get("postal_code") or "1016 GV",
        "type": "ideal",
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "key": stripe_pk,
    }
    bank = os.environ.get("IDEAL_BANK", "").strip()
    if bank:
        body["ideal[bank]"] = bank
    if billing.get("state"):
        body["billing_details[address][state]"] = billing["state"]

    resp = stripe.post(endpoints.STRIPE_PAYMENT_METHODS, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "ideal_pm", body, "POST", endpoints.STRIPE_PAYMENT_METHODS, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"创建 iDEAL PM 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"创建 iDEAL PM 响应异常: {resp.text[:300]}")
    return pm_id


def add_inline_ideal_payment_method_data(
    body: dict[str, Any], cs_id: str, billing: dict[str, str], ctx: dict[str, Any]
) -> None:
    body.update(
        {
            "payment_method_data[type]": "ideal",
            "payment_method_data[allow_redisplay]": "limited",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[payment_user_agent]": f"stripe.js/{random_runtime_version()}; stripe-js-v3/{random_runtime_version()}; payment-element; deferred-intent",
            "payment_method_data[referrer]": endpoints.CHATGPT_BASE,
            "payment_method_data[time_on_page]": str(random.randint(18000, 55000)),
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["stripe_js_id"],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": ctx.get("config_id") or "",
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx["elements_session_id"],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx[
                "elements_session_config_id"
            ],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        }
    )
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    bank = os.environ.get("IDEAL_BANK", "").strip()
    if bank:
        body["payment_method_data[ideal][bank]"] = bank


def add_inline_blik_payment_method_data(
    body: dict[str, Any], cs_id: str, billing: dict[str, str], ctx: dict[str, Any]
) -> None:
    body.update(
        {
            "payment_method_data[type]": "blik",
            "payment_method_data[allow_redisplay]": "limited",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][line2]": billing.get("line2", ""),
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[payment_user_agent]": (
                f"stripe.js/{random_runtime_version()}; stripe-js-v3/{random_runtime_version()}; "
                "payment-element; deferred-intent"
            ),
            "payment_method_data[referrer]": endpoints.CHATGPT_BASE,
            "payment_method_data[time_on_page]": str(random.randint(18000, 55000)),
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["client_session_id"],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": ctx.get("config_id") or "",
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx["elements_session_id"],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx[
                "elements_session_config_id"
            ],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "address",
            "blik_code": os.environ.get("IDEAL_BLIK_CODE", "").strip(),
        }
    )


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    return shared_stripe_flow.processor_entity_for_country(
        country, processor_entity, normalize_country=normalize_country
    )


def stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str) -> str:
    return shared_stripe_flow.stripe_checkout_long_url(
        cs_id, country, processor_entity, normalize_country=normalize_country
    )


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    return shared_stripe_flow.to_openai_pay_url(stripe_hosted_url)


def stripe_confirm_return_url(cs_id: str, checkout: dict[str, str], stripe_hosted_url: str) -> str:
    return shared_stripe_flow.stripe_confirm_return_url(
        cs_id, checkout, stripe_hosted_url, normalize_country=normalize_country, default_country="NL"
    )


def stripe_confirm_ideal(
    stripe: requests.Session,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
) -> dict[str, Any]:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "eid": "NA",
        "expected_amount": os.environ.get("PP_EXPECTED_AMOUNT", "").strip()
        or str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "ideal",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": runtime_version,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(ctx.get("client_session_id") or ctx["stripe_js_id"]),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "link_brand": "link",
    }
    if env_bool("IDEAL_CONFIRM_INLINE_PM", False):
        add_inline_ideal_payment_method_data(body, cs_id, billing, ctx)
    else:
        body["payment_method"] = pm_id
    url = endpoints.stripe_payment_page(cs_id, "confirm")
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "ideal_confirm", body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"iDEAL confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def stripe_confirm_blik(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
) -> dict[str, Any]:
    code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError("IDEAL_BLIK_CODE 必须是6位数字")
    body: dict[str, Any] = {
        "eid": "NA",
        "expected_amount": os.environ.get("PP_EXPECTED_AMOUNT", "").strip()
        or str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "blik",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(ctx["client_session_id"]),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "link_brand": "link",
    }
    body.update(stripe_elements_session_params(ctx))
    add_inline_blik_payment_method_data(body, cs_id, billing, ctx)
    url = endpoints.stripe_payment_page(cs_id, "confirm")
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "blik_confirm", body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"BLIK confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    return common_collect_urls(payload, urls)


def is_resource_url(url: str) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_is_resource_url(url)


def is_known_static_host(url: str) -> bool:
    """Delegate to ``common/proxy_bookkeeping.py``."""
    return shared_is_known_static_host(url)


def is_redirect_like_url(url: str, from_action_field: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if is_resource_url(url):
        return False
    if from_action_field:
        return True

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    text = f"{host}{path}?{query}"
    if host in {"hooks.stripe.com", "payments.stripe.com"}:
        return True
    if host.endswith(".ideal.nl") or host == "ideal.nl":
        return True
    return any(part in text for part in ("ideal", "/redirect/", "redirect_to_url", "authenticate"))


def is_qr_candidate(url: str) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_is_qr_candidate(url)


def extract_qr_candidates(payload: Any) -> list[str]:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_extract_qr_candidates(payload)


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    return common_find_submission_attempt(payload)


def extract_redirect_url(payload: Any, path: tuple[str, ...] = ()) -> str:
    return common_extract_redirect_url(payload, is_redirect_like_url)


def first_value_by_key(payload: Any, key: str) -> Any:
    return common_first_value_by_key(payload, key)


def setup_intent_last_error(payload: Any) -> str:
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        is_setup_intent = payload.get("object") == "setup_intent" or payload_id.startswith("seti_")
        last_error = payload.get("last_setup_error") if is_setup_intent else None
        setup_intent = payload.get("setup_intent")
        if not last_error and isinstance(setup_intent, dict):
            last_error = setup_intent.get("last_setup_error")
        if last_error:
            try:
                return json.dumps(last_error, ensure_ascii=False)
            except Exception:
                return str(last_error)
        for value in payload.values():
            found = setup_intent_last_error(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = setup_intent_last_error(value)
            if found:
                return found
    return ""


def raise_if_setup_intent_blocked(payload: Any, context: str) -> None:
    last_error = setup_intent_last_error(payload)
    if not last_error:
        return
    if "generic_decline" in last_error.lower():
        raise RuntimeError(
            f"Stripe 风控拒绝（generic_decline）：{context} SetupIntent 创建失败；last_setup_error={last_error}"
        )
    raise RuntimeError(f"{context}: setup_intent.last_setup_error: {last_error}")


def log_payment_page_summary(stage: str, payload: Any) -> None:
    summary = payment_page_summary(payload)
    if not summary:
        return
    compact = format_payment_summary(summary)
    log(f"{stage} 返回摘要: {compact}")


def warmup_approve_context(chatgpt: requests.Session, checkout_page_url: str) -> None:
    try:
        resp = chatgpt.post(
            endpoints.CHATGPT_SENTINEL_PING,
            json={},
            headers={
                "Referer": endpoints.CHATGPT_BASE + "/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(
            resp,
            "sentinel_ping",
            {},
            "POST",
            endpoints.CHATGPT_SENTINEL_PING,
            force=env_bool("IDEAL_DUMP_WARMUP", False),
        )
    except Exception as exc:
        log(f"approve sentinel 请求异常: {exc}", "[WARN] ")


def chatgpt_approve(chatgpt: requests.Session, checkout: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(
        checkout.get("billing_country", "NL"), checkout.get("processor_entity", "")
    )
    checkout_page_url = endpoints.chatgpt_checkout_page(processor, cs_id)
    if env_bool("IDEAL_APPROVE_WARMUP", True):
        warmup_approve_context(chatgpt, checkout_page_url)
        time.sleep(random.uniform(0.8, 1.6))

    body = {"checkout_session_id": cs_id, "processor_entity": processor}
    headers = {
        "Referer": checkout_page_url,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
    }
    resp = chatgpt.post(
        endpoints.CHATGPT_CHECKOUT_APPROVE,
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "approve", body, "POST", endpoints.CHATGPT_CHECKOUT_APPROVE, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"ChatGPT approve 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    result = ""
    try:
        result = str((resp.json() or {}).get("result") or "")
    except (ValueError, TypeError, AttributeError):
        # Unparsable body: leave ``result`` empty so the check below fails with
        # the response text instead of pretending approval.
        result = ""
    if result != "approved":
        raise RuntimeError(f"ChatGPT approve 未通过: {result or resp.text[:200]}")


def approve_attempt(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    session_token: str,
    proxy: str,
    index: int,
    attempt_count: int,
) -> None:
    log(f"approve 第 {index}/{attempt_count} 次 / proxy={proxy_label(proxy)}")
    chatgpt = build_chatgpt_session(access_token, device_id, proxy, session_token)
    chatgpt_approve(chatgpt, checkout)


def log_approve_failure(error: str) -> bool:
    log(f"approve 失败: {error[:180]}", "[WARN] ")
    if "ChatGPT approve 未通过: blocked" in error:
        log("approve 返回 blocked，当前 checkout/approve 组失败后切下一次尝试", "[WARN] ")
        return True
    return False


def is_approve_failure_error(error: str) -> bool:
    """Delegate to ``common/payment_predicates.py``."""
    return shared_is_approve_failure_error(error)


def approve_with_retry(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    proxies: list[str],
    session_token: str,
    proxy_group: str = "provider",
) -> str:
    max_retry = env_int("IDEAL_APPROVE_RETRY_MAX", 10)
    parallel = env_int("IDEAL_APPROVE_PARALLEL", 1)
    last_error = ""
    if max_retry <= 0:
        raise RuntimeError("approve 重试次数必须大于 0")
    proxies = [proxy for proxy in dict.fromkeys(proxies) if proxy]
    if not proxies:
        raise RuntimeError("approve 代理为空")
    sticky = env_bool("IDEAL_APPROVE_STICKY", False)
    if sticky:
        selected_proxies = [proxies[0]] * max_retry
    else:
        attempt_count = min(max_retry, len(proxies))
        fixed_proxies = proxies[: min(2, attempt_count)]
        selected_proxies = fixed_proxies[:]
        remain_count = attempt_count - len(selected_proxies)
        if remain_count > 0:
            selected_proxies.extend(random.sample(proxies[2:], min(remain_count, len(proxies) - 2)))
    attempt_count = len(selected_proxies)
    log(f"approve 代理策略: {'sticky' if sticky else 'rotate'}")
    if parallel > 1:
        workers = min(parallel, attempt_count)
        log(f"approve 并发: workers={workers}, attempts={attempt_count}")
        blocked_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    approve_attempt,
                    access_token,
                    device_id,
                    checkout,
                    session_token,
                    proxy,
                    index,
                    attempt_count,
                ): proxy
                for index, proxy in enumerate(selected_proxies, start=1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                    log("approve 成功")
                    for pending in futures:
                        pending.cancel()
                    return futures[future]
                except Exception as exc:
                    last_error = str(exc)
                    if is_checkout_not_active_error(last_error):
                        raise RuntimeError("checkout_not_active_session")
                    if log_approve_failure(last_error):
                        blocked_count += 1
                        record_proxy_health_failure(proxy_group, futures[future], last_error)
        if blocked_count and blocked_count == attempt_count:
            raise RuntimeError("approve blocked")
        raise RuntimeError(f"approve 重试失败: {last_error}")

    blocked_count = 0
    for index, proxy in enumerate(selected_proxies, start=1):
        try:
            approve_attempt(access_token, device_id, checkout, session_token, proxy, index, attempt_count)
            log("approve 成功")
            return proxy
        except Exception as exc:
            last_error = str(exc)
            if is_checkout_not_active_error(last_error):
                raise RuntimeError("checkout_not_active_session")
            if log_approve_failure(last_error):
                blocked_count += 1
                record_proxy_health_failure(proxy_group, proxy, last_error)
            if index < attempt_count:
                time.sleep(random.uniform(1, 2))
    if blocked_count and blocked_count == attempt_count:
        raise RuntimeError("approve blocked")
    raise RuntimeError(f"approve 重试失败: {last_error}")


def poll_payment_page(
    stripe: requests.Session, checkout: dict[str, str], stripe_pk: str, ctx: dict[str, Any]
) -> tuple[str, list[str]]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("IDEAL_POLL_TIMEOUT", 45)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    last_error = ""
    last_payload: dict[str, Any] = {}
    last_summary = ""
    while time.time() < deadline:
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            dump_http(resp, "poll_error", params, "GET", url, force=True)
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            last_error = f"HTTP {resp.status_code}"
            time.sleep(1)
            continue
        payload = resp.json() or {}
        last_payload = payload
        summary = payment_page_summary(payload)
        summary_text = format_payment_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            log(f"poll 返回摘要: {summary_text}")
        redirect_url = extract_redirect_url(payload)
        qr_urls = extract_qr_candidates(payload)
        if redirect_url or qr_urls:
            dump_http(resp, "poll_success", params, "GET", url, force=True)
            return redirect_url, qr_urls
        submission = find_submission_attempt(payload)
        if submission.get("state") == "requires_approval":
            last_error = "payment_pages 仍然 requires_approval"
            time.sleep(1)
            continue
        if submission.get("state") == "failed":
            dump_http(resp, "poll_failed", params, "GET", url, force=True)
            raise RuntimeError(f"Stripe submission failed: {submission}")
        last_error = str(submission or "waiting")
        time.sleep(1)
    if last_payload:
        # A real Response (not a dynamic stub) so ``dump_http``'s annotation and
        # the ``.text`` decoding path are both exercised as they are in prod.
        dump_response = requests.Response()
        dump_response.status_code = 200
        dump_response.url = url
        dump_response.encoding = "utf-8"
        dump_response._content = json.dumps(last_payload, ensure_ascii=False, indent=2).encode("utf-8")
        dump_http(dump_response, "poll_no_redirect", params, "GET", url, force=True)
    log(f"poll 结束，未发现真实 iDEAL redirect/QR: {last_error}", "[WARN] ")
    return "", []


def poll_blik_status(
    stripe: requests.Session,
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("IDEAL_BLIK_POLL_TIMEOUT", 65)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    last_state = "waiting"
    last_summary = ""
    while time.time() < deadline:
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            last_state = f"HTTP {resp.status_code}"
            time.sleep(1)
            continue
        try:
            payload = resp.json() or {}
        except Exception:
            payload = {"_raw_text": resp.text}
        raise_if_setup_intent_blocked(payload, "BLIK payment_pages")

        summary = payment_page_summary(payload)
        summary_text = format_payment_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            log(f"BLIK poll 返回摘要: {summary_text}")

        submission = find_submission_attempt(payload)
        if submission.get("state") == "failed":
            raise RuntimeError(f"Stripe submission failed: {submission}")

        intent = first_value_by_key(payload, "payment_intent")
        if not isinstance(intent, dict):
            intent = first_value_by_key(payload, "setup_intent")
        intent_status = str(intent.get("status") or "") if isinstance(intent, dict) else ""
        next_action = intent.get("next_action") if isinstance(intent, dict) else None
        next_action_type = str(next_action.get("type") or "") if isinstance(next_action, dict) else ""
        if intent_status in {"succeeded", "processing"}:
            return payload
        if intent_status in {"requires_payment_method", "canceled"}:
            raise RuntimeError(f"BLIK 支付失败: intent_status={intent_status}")
        if next_action_type == "blik_authorize":
            last_state = "等待手机银行确认"
        elif submission.get("state") == "requires_approval":
            last_state = "payment_pages 仍然 requires_approval"
        else:
            checkout_status = str(payload.get("status") or "")
            payment_status = str(payload.get("payment_status") or "")
            if checkout_status == "complete" or payment_status in {"paid", "no_payment_required"}:
                return payload
            last_state = intent_status or str(submission or "waiting")
        time.sleep(1)
    raise RuntimeError(f"BLIK 状态确认超时: {last_state}")


def fetch_redirect_page(stripe: requests.Session, start_url: str) -> list[str]:
    if not start_url or not env_bool("IDEAL_FOLLOW_REDIRECT", True):
        return []
    current = start_url
    qr_urls: list[str] = []
    for hop in range(1, 6):
        resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        dump_http(resp, f"redirect_hop_{hop}", None, "GET", current, force=True)
        qr_urls.extend(extract_qr_candidates(resp.text))
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            break
        current = urljoin(current, location)
    return list(dict.fromkeys(qr_urls))


def resolve_external_redirect(stripe: requests.Session, start_url: str) -> str:
    if not start_url or not env_bool("IDEAL_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for hop in range(1, 6):
        host = (urlparse(current).netloc or "").lower()
        if host.endswith(".ideal.nl") or host == "ideal.nl":
            return current
        try:
            resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
            dump_http(resp, f"resolve_redirect_hop_{hop}", None, "GET", current, force=True)
        except Exception as exc:
            log(f"跟随 redirect 异常: {exc}", "[WARN] ")
            return current
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            return current
        current = urljoin(current, location)
    return current


def approve_proxy_candidates(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    approve_preferences = successful_approve_preferences(
        checkout_proxy, provider_proxy, [checkout_proxy] + approve_pool
    )
    if approve_preferences:
        log(f"命中成功 approve 代理优先: {proxy_label(approve_preferences[0])}")
    return list(dict.fromkeys(approve_preferences + [checkout_proxy]))


def run_provider_flow(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    device_id: str,
    checkout: dict[str, str],
    billing: dict[str, str],
    stop_event: Event | None = None,
) -> tuple[str, list[str]]:
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    expected_method = payment_method_type()
    expected_label = current_payment_method_label()
    stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK

    log(f"Stripe init (PM={billing['country']}, proxy={proxy_label(provider_proxy)})...")
    init_payload = stripe_init(checkout["cs_id"], stripe_pk, provider_proxy)
    hosted_url = str(init_payload.get("stripe_hosted_url") or "")
    if not checkout.get("processor_entity"):
        processor_entity = infer_processor_entity(init_payload)
        if processor_entity:
            checkout["processor_entity"] = processor_entity
            log(f"从 Stripe init 推断 processor_entity={processor_entity}")
    ctx = build_ctx(init_payload, checkout)
    amount = ctx.get("checkout_amount") or 0
    record_checkout_zero_result(checkout_proxy, checkout_country, int(amount or 0))
    amount_major = int(amount or 0) / 100
    log(f"Stripe init 成功, 金额={checkout['currency']} {amount_major:.2f}")
    payment_method_types = first_value_by_key(init_payload, "payment_method_types")
    if isinstance(payment_method_types, list):
        methods = [str(item).lower() for item in payment_method_types]
        log(f"Stripe 可用支付方式: {methods}")
        if expected_method not in methods:
            raise RuntimeError(f"当前 checkout 不支持 {expected_label}，payment_method_types={methods}")
    if env_bool("IDEAL_REQUIRE_ZERO", True) and int(amount or 0) != 0:
        raise RuntimeError(f"0 元优惠未生效，当前金额小单位={amount}，已停止生成非 0 元 {expected_label} 链")
    if int(amount or 0) == 0:
        action = "提交 BLIK 接口" if expected_method == "blik" else "提取 iDEAL 支付页 URL"
        log(f"当前金额为 0，继续按 0 元 promo 流程{action}")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    if expected_method == "blik":
        stripe = new_session(provider_proxy)
        stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})
        log(
            f"BLIK 接口 confirm: 账单={billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )
        confirm_payload = stripe_confirm_blik(
            stripe,
            checkout["cs_id"],
            stripe_pk,
            init_payload,
            ctx,
            checkout,
            hosted_url,
            billing,
        )
        log_payment_page_summary("BLIK confirm", confirm_payload)
        raise_if_setup_intent_blocked(confirm_payload, "BLIK confirm")
        submission = find_submission_attempt(confirm_payload)
        if submission.get("state") == "failed":
            raise RuntimeError(f"Stripe submission failed: {submission}")

        approve_proxy = ""
        if submission.get("state") == "requires_approval":
            log("BLIK confirm 需要 ChatGPT approve...")
            approve_proxies = approve_proxy_candidates(checkout_proxy, provider_proxy, approve_pool)
            approve_proxy = approve_with_retry(
                access_token,
                device_id,
                checkout,
                approve_proxies,
                session_token,
                "checkout",
            )
            log("BLIK approve 成功，轮询 Stripe 后续状态...")

        final_payload = poll_blik_status(stripe, checkout, stripe_pk, ctx)
        log_payment_page_summary("BLIK final", final_payload)
        if approve_proxy:
            record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
        log("BLIK 后续状态确认完成")
        return f"blik://{checkout['cs_id']}", []

    stripe = new_session(provider_proxy)
    stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})

    pm_id = ""
    if env_bool("IDEAL_CONFIRM_INLINE_PM", False):
        log(
            f"iDEAL confirm 内联资料: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']} / bank={os.environ.get('IDEAL_BANK', '') or 'auto'}"
        )
    else:
        log(f"创建 PM (IDEAL): {billing['country']} {billing['name']} / {billing['city']}")
        pm_id = stripe_create_ideal_pm(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
        log(f"PM 创建成功: {pm_id}")
        log(
            f"荷兰资料已填入 PM billing_details: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )

    if env_bool("IDEAL_UPDATE_CUSTOMER_DATA", False):
        log(
            f"提交荷兰资料填充: {billing['name']} / {billing['line1']} / "
            f"{billing['city']} {billing['postal_code']} / {billing['email']}"
        )
        stripe_update_customer_data(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    if env_bool("IDEAL_UPDATE_TAX_REGION", False):
        stripe_update_tax_region(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    if env_bool("IDEAL_CHECKOUT_SNAPSHOT", False):
        snapshot_chatgpt = build_chatgpt_session(access_token, device_id, provider_proxy, session_token)
        checkout_snapshot(snapshot_chatgpt, checkout, billing)

    log("Stripe confirm (expected=IDEAL)...")
    confirm_payload = stripe_confirm_ideal(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout, hosted_url, billing
    )
    log("Stripe confirm 成功, 解析跳转...")
    log_payment_page_summary("confirm", confirm_payload)
    redirect_url = extract_redirect_url(confirm_payload)
    qr_urls = extract_qr_candidates(confirm_payload)
    submission = find_submission_attempt(confirm_payload)

    if redirect_url:
        log(f"confirm 提取到最终扫码/授权 URL: {redirect_url[:180]}")
    if qr_urls:
        log(f"confirm 提取到 QR 候选 {len(qr_urls)} 个")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    if not redirect_url and submission.get("state") == "requires_approval":
        log("需要 ChatGPT approve...")
        approve_preferences = successful_approve_preferences(
            checkout_proxy, provider_proxy, [checkout_proxy] + approve_pool
        )
        if approve_preferences:
            log(f"命中成功 approve 代理优先: {proxy_label(approve_preferences[0])}")
        approve_proxies = list(dict.fromkeys(approve_preferences + [checkout_proxy]))
        log("需要 approve：iDEAL 0 元场景，approve 复用当前 checkout 代理，失败后切下一次尝试。")
        approve_proxy = approve_with_retry(
            access_token, device_id, checkout, approve_proxies, session_token, "checkout"
        )
        log("跟随跳转提取最终链...")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx)
        qr_urls.extend(poll_qr)
        if redirect_url:
            record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
            log("完成 - 已记忆此 checkout/provider/approve combo")
    elif not redirect_url and not qr_urls:
        log("confirm 未返回真实 iDEAL redirect/QR，继续 poll payment_pages 做最终确认", "[WARN] ")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx)
        qr_urls.extend(poll_qr)

    if redirect_url:
        final_url = resolve_external_redirect(stripe, redirect_url)
        if final_url and final_url != redirect_url:
            log(f"跟随 redirect 得到最终链: {final_url[:180]}")
            redirect_url = final_url

    return redirect_url, list(dict.fromkeys(qr_urls))


def run_once(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
) -> tuple[str, list[str]]:
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")
    device_id = str(uuid.uuid4())
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    billing = ideal_billing_profile()
    expected_label = current_payment_method_label()
    log(f"开始 {expected_label} 提取，第 {attempt}/{max_retry} 次")
    log(
        "组合测试: "
        f"{checkout_country} / {billing['country']} / {currency_for_country(checkout_country)} / "
        f"{payment_browser_locale()} / {os.environ.get('IDEAL_BANK', '').strip() or 'ANY'} / "
        f"{provider_country_label()}"
    )
    log(f"本轮代理: checkout/资格={proxy_label(checkout_proxy)}；Stripe/{expected_label}={proxy_label(provider_proxy)}")
    ensure_proxy_country("checkout", checkout_proxy)
    ensure_proxy_country("provider", provider_proxy)
    ensure_proxy_targets("checkout", checkout_proxy)
    ensure_proxy_targets("provider", provider_proxy)
    zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
    if zero_status == "ok":
        log(f"checkout 0元资格缓存命中: amount={zero_amount}")
    elif zero_status == "bad":
        log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    try:
        chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
        checkout = create_checkout(chatgpt, checkout_country)
    except Exception as exc:
        if is_user_already_paid_error(exc):
            raise RuntimeError("用户已支付: User is already paid") from exc
        if is_checkout_not_active_error(exc):
            raise
        raise RuntimeError(f"checkout 阶段失败: {exc}") from exc
    return run_provider_flow(
        access_token,
        session_token,
        checkout_proxy,
        provider_proxy,
        approve_pool,
        device_id,
        checkout,
        billing,
        stop_event,
    )


def run_attempt(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
    batch_no: int = 0,
    batch_total: int = 0,
) -> tuple[int, str, list[str], str, str, str]:
    previous_log_context = getattr(_log_context, "prefix", "")
    if batch_no > 0:
        _log_context.prefix = f"[批次 {batch_no}/{batch_total or '?'}][轮次 {attempt}/{max_retry}] "
    else:
        _log_context.prefix = f"[轮次 {attempt}/{max_retry}] "
    try:
        redirect_url, qr_urls = run_once(
            access_token,
            session_token,
            checkout_proxy,
            provider_proxy,
            approve_pool,
            attempt,
            max_retry,
            stop_event,
        )
        has_result = bool(redirect_url)
        if has_result and stop_event:
            stop_event.set()
        if has_result:
            record_proxy_pair_result(checkout_proxy, provider_proxy, True, "success")
        else:
            record_proxy_result("provider", provider_proxy, False, "no_redirect_url")
        _log_context.prefix = previous_log_context
        return attempt, redirect_url, qr_urls, checkout_proxy, provider_proxy, ""
    except Exception as exc:
        error = str(exc)
        if error.startswith("任务已停止"):
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, ""
        if is_user_already_paid_error(error):
            log("检测到 User is already paid：用户已支付，停止任务")
            if stop_event:
                stop_event.set()
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if "当前 checkout 不支持" in error:
            log(f"第 {attempt}/{max_retry} 轮失败: {error[:300]}，继续尝试后续代理/checkout", "[WARN] ")
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if is_checkout_not_active_error(error):
            log(
                f"第 {attempt}/{max_retry} 轮 Session 已失效；跳过本轮，不记录代理失败，代理保留供后续使用",
                "[WARN] ",
            )
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        record_failure_by_stage(error, checkout_proxy, provider_proxy)
        log(f"第 {attempt}/{max_retry} 轮失败: {error[:300]}", "[WARN] ")
        _log_context.prefix = previous_log_context
        return attempt, "", [], checkout_proxy, provider_proxy, error


def successful_pair_preferences(checkout_proxies: list[str], provider_proxies: list[str]) -> dict[str, list[str]]:
    """Delegate to ``common/proxy_state.py``."""
    return shared_successful_pair_preferences(_proxy_state_store, checkout_proxies, provider_proxies)


def build_attempt_batches(
    checkout_proxies: list[str], provider_proxies: list[str], max_attempts: int
) -> list[tuple[str, list[str]]]:
    return shared_build_attempt_batches(
        checkout_proxies,
        provider_proxies,
        max_attempts,
        per_checkout=env_int("IDEAL_PROVIDER_PER_CHECKOUT", 30),
        successful_pair_preferences=successful_pair_preferences,
        log=log,
    )


def is_preferred_proxy(group: str, proxy: str) -> bool:
    if not group or not env_bool("IDEAL_PROXY_SCORE", True):
        return False
    state = load_proxy_state().get(group, {})
    if not isinstance(state, dict):
        return False
    record = state.get(proxy_state_key(group, proxy), {})
    if not isinstance(record, dict):
        return False
    if int(record.get("success") or 0) > 0:
        return True
    return group in {"checkout", "seed"} and record.get("zero_ok") is True


def pick_random_proxies(proxies: list[str], limit: int, group: str = "") -> list[str]:
    """Delegate to the shared picker (batch 5 of the consolidation).

    ``is_preferred`` is this module's own rule.  blik's is deliberately not the
    shared one (it keys records by group and honours ``zero_ok``), so passing
    the callable keeps that difference explicit instead of hiding it behind a
    flag.
    """
    return shared_pick_random_proxies(
        proxies,
        limit,
        group,
        order_group=order_proxy_group,
        is_preferred=is_preferred_proxy,
    )


def run_single_link_attempt(
    access_token: str,
    session_token: str,
    checkout_proxies: list[str],
    provider_proxies: list[str],
    attempt: int,
    ideal_retry: int,
    checkout_retry: int,
    provider_retry: int,
    checkout_country: str,
    checkout_currency: str,
    stop_event: Event,
) -> tuple[int, str, str, bool]:
    previous_log_context = getattr(_log_context, "prefix", "")
    expected_label = current_payment_method_label()
    _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
    last_error = ""
    approve_blocked = False
    checkout_proxy_used = ""
    try:
        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        billing = ideal_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        provider_candidates = pick_random_proxies(provider_proxies, provider_retry, "provider")
        if not provider_candidates:
            log("Provider 代理已全部被失败状态过滤，停止本次提链", "[WARN] ")
            return attempt, "", "provider_all_filtered", False
        checkout: dict[str, str] | None = None

        log(f"开始第 {attempt}/{ideal_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{ideal_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"首次 PM 国家: {pm_country}")

        for checkout_index, checkout_proxy in enumerate(checkout_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}][PM={pm_country}] "
            try:
                log(
                    f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}"
                )
                ensure_proxy_country("checkout", checkout_proxy)
                ensure_proxy_targets("checkout", checkout_proxy)
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(
                    checkout_proxy, checkout_country
                )
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，停止任务")
                    stop_event.set()
                    return attempt, "", error, False
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(f"checkout 阶段失败: {error}", checkout_proxy, "")
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")

        _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{ideal_retry} 次提链 checkout 阶段失败", "[WARN] ")
            return attempt, "", last_error or "checkout_failed", False

        preferred_providers = successful_pair_preferences([checkout_proxy_used], provider_proxies).get(
            checkout_proxy_used, []
        )
        if preferred_providers:
            provider_candidates = list(dict.fromkeys(preferred_providers + provider_candidates))[:provider_retry]
            log(f"命中成功 checkout/provider 组合优先: provider={len(preferred_providers)}")

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log("Stripe publishable key loaded")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        for provider_index, provider_proxy in enumerate(provider_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}][PM={pm_country}] "
            try:
                log(f"Provider(随机 {provider_index}/{len(provider_candidates)}): {proxy_label(provider_proxy)}")
                ensure_proxy_country("provider", provider_proxy)
                ensure_proxy_targets("provider", provider_proxy)
                redirect_url, _qr_urls = run_provider_flow(
                    access_token,
                    session_token,
                    checkout_proxy_used,
                    provider_proxy,
                    checkout_proxies,
                    device_id,
                    checkout,
                    billing,
                    stop_event,
                )
                if redirect_url:
                    record_proxy_pair_result(checkout_proxy_used, provider_proxy, True, "success")
                    stop_event.set()
                    return attempt, redirect_url, "", False
                last_error = "no_redirect_url"
                record_proxy_result("provider", provider_proxy, False, last_error)
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
                    break
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy)
                log(f"Provider {provider_index}/{len(provider_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error):
                    if "approve blocked" in error:
                        approve_blocked = True
                    log("approve 失败，当前 checkout/approve 组失败，切下一次尝试", "[WARN] ")
                    break
                if "当前 checkout 不支持" in error:
                    log(f"当前 provider 未拿到 {expected_label}，继续尝试下一个 provider", "[WARN] ")
                    continue
                if "0 元优惠未生效" in error:
                    break

        _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
        log(f"第 {attempt}/{ideal_retry} 次提链结束，未拿到最终 URL", "[WARN] ")
        return attempt, "", last_error, approve_blocked
    finally:
        _log_context.prefix = previous_log_context


def run_single_link_parallel_mode(
    access_token: str, session_token: str, checkout_proxies: list[str], provider_proxies: list[str]
) -> int:
    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("IDEAL_PROVIDER_RETRY_MAX", 3)
    ideal_retry = env_int("IDEAL_MAX_RETRY", 5)
    requested_workers = env_int("IDEAL_WORKERS", 1)
    worker_limit = env_int("IDEAL_WORKERS_MAX", requested_workers)
    workers = min(max(1, requested_workers), max(1, worker_limit), ideal_retry)
    expected_label = current_payment_method_label()
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    checkout_currency = currency_for_country(checkout_country)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", ideal_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    if requested_workers > workers:
        log(f"{expected_label}并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        f"开始执行 {expected_label} 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={provider_country_label()}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，{expected_label}总重试={ideal_retry}，{expected_label}并发={workers}。"
    )

    executor = ThreadPoolExecutor(max_workers=workers)
    futures: dict[Any, int] = {}
    try:
        for attempt in range(1, ideal_retry + 1):
            futures[
                executor.submit(
                    run_single_link_attempt,
                    access_token,
                    session_token,
                    checkout_proxies,
                    provider_proxies,
                    attempt,
                    ideal_retry,
                    checkout_retry,
                    provider_retry,
                    checkout_country,
                    checkout_currency,
                    stop_event,
                )
            ] = attempt

        for future in as_completed(futures):
            try:
                attempt, redirect_url, error, approve_blocked = future.result()
            except Exception as exc:
                attempt = futures.get(future, 0)
                redirect_url = ""
                error = str(exc)
                approve_blocked = False
                log(f"第 {attempt}/{ideal_retry} 次提链异常: {error[:300]}", "[WARN] ")
            if redirect_url:
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                print("\n===== 结果 =====")
                print_result_url(redirect_url)
                return 0
            last_error = error or last_error
            if is_user_already_paid_error(error):
                log("检测到 User is already paid：用户已支付，任务正常结束")
                print_already_paid_result()
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 0
            if approve_blocked:
                approve_blocked_count += 1
                log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
            if approve_blocked_count >= max_blocked:
                log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 1
    finally:
        executor.shutdown(wait=True, cancel_futures=stop_event.is_set())

    log(f"全部失败: {last_error}", "[ERROR] ")
    print_failure_result(last_error or "all attempts failed")
    return 1


def run_single_link_mode(
    access_token: str, session_token: str, checkout_proxies: list[str], provider_proxies: list[str]
) -> int:
    ideal_workers = env_int("IDEAL_WORKERS", 1)
    if ideal_workers > 1:
        return run_single_link_parallel_mode(access_token, session_token, checkout_proxies, provider_proxies)

    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("IDEAL_PROVIDER_RETRY_MAX", 3)
    ideal_retry = env_int("IDEAL_MAX_RETRY", 5)
    expected_label = current_payment_method_label()
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    checkout_currency = currency_for_country(checkout_country)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", ideal_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    log(
        f"开始执行 {expected_label} 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={provider_country_label()}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，{expected_label}总重试={ideal_retry}。"
    )

    for attempt in range(1, ideal_retry + 1):
        billing = ideal_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        provider_candidates = pick_random_proxies(provider_proxies, provider_retry, "provider")
        if not provider_candidates:
            last_error = "Provider 代理已全部被失败状态过滤"
            log(last_error, "[WARN] ")
            break
        checkout: dict[str, str] | None = None
        checkout_proxy_used = ""

        log(f"开始第 {attempt}/{ideal_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{ideal_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"  首次 PM 国家: {pm_country}")

        for checkout_index, checkout_proxy in enumerate(checkout_candidates, start=1):
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"  [PM={pm_country}] "
            try:
                log(
                    f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}"
                )
                ensure_proxy_country("checkout", checkout_proxy)
                ensure_proxy_targets("checkout", checkout_proxy)
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(
                    checkout_proxy, checkout_country
                )
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    print_already_paid_result()
                    return 0
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(f"checkout 阶段失败: {error}", checkout_proxy, "")
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")
            finally:
                _log_context.prefix = previous_log_context

        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{ideal_retry} 次提链 checkout 阶段失败，换下一次提链", "[WARN] ")
            continue

        preferred_providers = successful_pair_preferences([checkout_proxy_used], provider_proxies).get(
            checkout_proxy_used, []
        )
        if preferred_providers:
            provider_candidates = list(dict.fromkeys(preferred_providers + provider_candidates))[:provider_retry]
            log(f"命中成功 checkout/provider 组合优先: provider={len(preferred_providers)}")

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log("Stripe publishable key loaded")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        for provider_index, provider_proxy in enumerate(provider_candidates, start=1):
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"  [PM={pm_country}] "
            try:
                log(f"Provider(随机 {provider_index}/{len(provider_candidates)}): {proxy_label(provider_proxy)}")
                ensure_proxy_country("provider", provider_proxy)
                ensure_proxy_targets("provider", provider_proxy)
                redirect_url, _qr_urls = run_provider_flow(
                    access_token,
                    session_token,
                    checkout_proxy_used,
                    provider_proxy,
                    checkout_proxies,
                    device_id,
                    checkout,
                    billing,
                    stop_event,
                )
                if redirect_url:
                    record_proxy_pair_result(checkout_proxy_used, provider_proxy, True, "success")
                    print("\n===== 结果 =====")
                    print_result_url(redirect_url)
                    return 0
                last_error = "no_redirect_url"
                record_proxy_result("provider", provider_proxy, False, last_error)
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
                    break
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy)
                log(f"Provider {provider_index}/{len(provider_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error):
                    if "approve blocked" in error:
                        approve_blocked_count += 1
                        log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
                    log("approve 失败，当前 checkout/approve 组失败，切下一次尝试", "[WARN] ")
                    break
                if "当前 checkout 不支持" in error:
                    log(f"当前 provider 未拿到 {expected_label}，继续尝试下一个 provider", "[WARN] ")
                    continue
                if "0 元优惠未生效" in error:
                    break
            finally:
                _log_context.prefix = previous_log_context

        if approve_blocked_count >= max_blocked:
            log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
            return 1
        log(f"第 {attempt}/{ideal_retry} 次提链结束，未拿到最终 URL", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    print_failure_result(last_error or "all attempts failed")
    return 1


def run_single_seed_mode(access_token: str, session_token: str, proxy_seeds: list[str]) -> int:
    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    max_retry = env_int("IDEAL_MAX_RETRY", 5)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", max_retry)
    attempted_seed_keys: set[str] = set()
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    log(
        "开始执行 BLIK 单 Seed 链路："
        f"PL checkout -> PL Stripe/BLIK -> PL approve；"
        f"每轮候选={checkout_retry}，总重试={max_retry}。"
    )
    for attempt in range(1, max_retry + 1):
        available_seeds = [
            proxy_seed for proxy_seed in proxy_seeds if proxy_chain_key(proxy_seed) not in attempted_seed_keys
        ]
        seed_candidates = pick_random_proxies(available_seeds, checkout_retry, "seed")
        if not seed_candidates:
            last_error = last_error or "本次任务的代理 Seed 已全部尝试"
            log("本次任务的代理 Seed 已全部尝试，不再重复失败节点", "[WARN] ")
            break

        log(f"开始第 {attempt}/{max_retry} 次 BLIK 提交")
        for seed_index, proxy_seed in enumerate(seed_candidates, start=1):
            chain_key = proxy_chain_key(proxy_seed)
            attempted_seed_keys.add(chain_key)
            checkout_proxy = ""
            provider_proxy = ""
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"[BLIK {attempt}/{max_retry}] "
            try:
                checkout_proxy, provider_proxy = blik_proxy_chain(proxy_seed)
                log_blik_proxy_chain(proxy_seed, checkout_proxy, provider_proxy)
                log(f"Seed {seed_index}/{len(seed_candidates)}: 本次已尝试 Seed={len(attempted_seed_keys)}")
                completion, _qr_urls = run_once(
                    access_token,
                    session_token,
                    checkout_proxy,
                    provider_proxy,
                    [checkout_proxy],
                    attempt,
                    max_retry,
                    stop_event,
                )
                if completion:
                    record_proxy_result("seed", proxy_seed, True, "success")
                    print("\n===== 结果 =====")
                    print_result_url(completion)
                    return 0
                last_error = "no_blik_completion"
                record_seed_failure(proxy_seed, last_error)
                log("BLIK 未返回完成状态，换下一个未尝试 Seed", "[WARN] ")
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    print_already_paid_result()
                    return 0
                record_seed_failure(proxy_seed, error)
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 Seed 不记录冷却，但本次任务不会重复抽取", "[WARN] ")
                elif "当前 checkout 不支持" in error:
                    log("当前 checkout 未提供 BLIK，换下一个未尝试 Seed", "[WARN] ")
                else:
                    log(f"Seed {seed_index}/{len(seed_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error) and "approve blocked" in error:
                    approve_blocked_count += 1
                    log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
            finally:
                _log_context.prefix = previous_log_context

            if approve_blocked_count >= max_blocked:
                log("达到当前账号 approve blocked 上限，停止继续提交", "[WARN] ")
                return 1

        log(f"第 {attempt}/{max_retry} 次 BLIK 提交结束，未完成", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    print_failure_result(last_error or "all attempts failed")
    return 1


def main() -> int:
    if not validate_blik_code_before_start():
        return 1
    access_token, session_token = load_token()
    if not access_token:
        log("access_token 为空", "[ERROR] ")
        print_failure_result("access_token 为空", error_code="missing_token", retryable=False)
        return 1

    proxy_seeds = load_proxy_seeds()
    os.environ.setdefault("IDEAL_PROXY_SEED_FILE", str(proxy_seed_file()))
    flow_mode = os.environ.get("IDEAL_FLOW_MODE", "single").strip().lower() or "single"
    if flow_mode != "single":
        log(f"IDEAL_FLOW_MODE={flow_mode} 已收敛为 BLIK 单 Seed 链路", "[WARN] ")
    return run_single_seed_mode(access_token, session_token, proxy_seeds)


if __name__ == "__main__":
    sys.exit(run_extractor_entrypoint(_result_reporter, main, error_code="blik_runner_exception"))

#!/usr/bin/env python3
r"""UPI 支付链接 / QR 生成子系统。

从 ``gen_pp_link.py`` 纯搬迁拆分而来；2026-09-17 按外部参考实现
``upi_extract.py`` 做了协议级改写（hosted → **custom checkout**）。

本模块拥有:

* ``generate_upi_qr_link`` -- 完整 UPI 提取流水线 (custom checkout 协议)
  (checkout → stripe init → 免费试用检测 → 税区/客户资料同步 → confirm →
   approve → 轮询 payment_pages/intent → 提取 upi:// URI → hydrate → 渲染 QR)
* ``_upi_*`` 系列 -- Stripe init / confirm / payment_pages 响应里的 UPI 数据提取助手
* ``_upi_extract_redirect_url`` / ``_upi_extract_qr_candidates`` /
  ``_upi_setup_intent_last_error`` -- 参考实现三层提取的等价实现
* ``_upi_confirm_amounts`` / ``_upi_display_amounts`` -- custom 模式 confirm 的
  金额与「屏幕上显示的行项目」回填
* ``_default_qr_path`` / ``_write_qr_png`` -- QR PNG / SVG 写出助手
* ``_method_cfg`` / ``_payment_stage_proxies_from_config`` -- 按支付方式的
  配置与阶段代理解析

依赖方向: ``gen_pp_link`` → 本模块; 本模块不得 import ``gen_pp_link``。

2026-09-17 改写的四条依据
------------------------
1. **base64url 解码缺 ``=`` 补位** (旧 L300): 旧代码是
   ``b64decode(raw.replace("-","+").replace("_","/"))``。
   字母表替换方向其实是对的（``-``/``_`` → ``+``/``/`` 确实还原成标准表），
   真正缺的是**补位**：Stripe 放在 URL fragment 里的 base64url 通常不带 ``=``，
   而 ``b64decode`` 对长度非 4 的倍数直接抛 ``Incorrect padding``。
   实测 ``'eyJhIjoxfQ'`` → 旧代码 ``FAIL Error: Incorrect padding``，
   而它一模一样是 ``{"a":1}`` 的合法无补位编码。
   异常被外层 ``except Exception: pass`` 吞掉 ⇒ 整包 hosted instructions
   payload 静默丢失。改为 ``base64.urlsafe_b64decode`` + ``"=" * (-len % 4)``。
2. **QR 类型用裸子串判定** (旧 L315):
   ``"png" if "png" in src.lower() else "svg"``。实测
   ``.../image.png?format=svg&w=300`` → 判成 ``svg`` (query 里出现了 svg, URL 里
   根本没有 png)。改为取 ``urlsplit(src).path`` 的扩展名。
3. **只取首个 ``<img>``** (旧 L316 的 ``break``): 改成收集全部候选并去重,
   参考实现 ``extract_qr_candidates`` 同样是「全收 + 去重」。
4. **协议方向**: 旧实现 ``checkout_ui_mode`` 默认 ``hosted``, 因此 confirm 缺
   ``expected_amount`` / ``last_displayed_line_item_group_details`` /
   ``consent[terms_of_service]`` / ``link_brand`` / ``guid|muid|sid`` —— 这些
   全是 custom 模式专属字段。改写后默认 ``custom``, 并补齐这些语义。
   注意**改代码默认值不够**: ``config.json`` / ``payment.json`` 的 ``upi`` 段
   显式写了 ``checkout_ui_mode: "hosted"``, 配置段优先级更高 ⇒ 不改配置
   运行期仍是 hosted。三个随附配置已同步改成 ``custom``。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

try:
    from .paypal_extract import CURRENCY_MAP, _new_session
    from .checkout_contract import PLUS_TRIAL_CAMPAIGN_ID, browser_profile_for_country
    from .phone_proxy import redact_proxy_text
    from .pp_link_helpers import (
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
    )
    from .paypal_proxy import _stage_proxy_value
except ImportError:  # pragma: no cover - direct script execution
    from paypal_extract import CURRENCY_MAP, _new_session  # type: ignore
    from checkout_contract import (  # type: ignore
        PLUS_TRIAL_CAMPAIGN_ID,
        browser_profile_for_country,
    )
    from phone_proxy import redact_proxy_text  # type: ignore
    from pp_link_helpers import (  # type: ignore
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
    )
    from paypal_proxy import _stage_proxy_value  # type: ignore

# curl_cffi for ChatGPT-facing sessions (TLS fingerprint + sec-ch-ua headers).
# Stripe-facing sessions keep using the standard ``_new_session`` (requests).
try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - curl_cffi is a project dependency
    _curl_requests = None


# ─── 输出 ────────────────────────────────────────────────────────────────────


def _emit(step: str, msg: str, **kw: Any) -> None:
    """Top-level progress/error sink (sunk copy; see ``gen_pp_link._emit``)."""
    print(f"[{step}] {msg}", file=sys.stderr)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """解析浮点环境变量。非法值 / 空值回落到默认值，并夹到下界。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, float(raw))
    except ValueError:
        return max(minimum, default)


# ─── 诊断抓包通道 ────────────────────────────────────────────────────────────
#
# 旧实现**没有任何 dump 通道**，线上出问题时只能靠 stderr 的几行 emit 猜，
# 拿不到「发出去的是什么、回来的又是什么」。参考实现有 ``dump_http``。
#
# 与同族模块（services/protocol-payment/*）保持同一套环境变量命名与语义：
#   UPI_DUMP=1          总开关（默认关）
#   UPI_DUMP_WARMUP=1   把预热类请求也记下来（默认只记 >=400 和显式 force）
#   UPI_DUMP_LIMIT=6000 单次响应截断长度
#   UPI_DUMP_DIR        落盘目录（默认 <repo>/runtime/upi_dumps）
#
# 🔴 落盘前**必须脱敏**：Bearer token / session-token / access_token /
#    代理 URL 里的账号密码。dump 文件是给人看的，不能因为开了调试就把凭据
#    写进磁盘。

#: 落盘目录相对仓库根的默认位置。放 ``runtime/`` 下是刻意的——该目录
#: 已被 ``.gitignore`` 第 16 行 ``/runtime/`` 整目录忽略，dump 不会被误提交。
UPI_DUMP_DEFAULT_DIR = "runtime/upi_dumps"

_dump_lock = threading.RLock()
_dump_counter = 0


def _upi_redact_for_dump(text: Any) -> str:
    """把一段文本里的凭据抹掉，供落盘/日志使用。

    四道过滤，缺一不可（参考实现只做了前三道；代理凭据是我们这边多出来的，
    因为本项目的代理由调用方传入，很容易带在请求体或错误文本里）：
      1. ``Authorization: Bearer <token>``
      2. ``__Secure-next-auth.session-token=<token>`` 及其它常见 cookie 名
      3. JSON / form 里的 ``access_token`` / ``sessionToken`` / ``token`` 字段
      4. 内联代理凭据 ``scheme://user:pass@host``（走项目 canonical 脱敏器）
    """
    value = str(text if text is not None else "")
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", value)
    value = re.sub(
        r"(?i)(__Secure-next-auth\.session-token=)[^;,\s\"']+", r"\1***", value
    )
    value = re.sub(
        r"(?i)((?:access[_-]?token|session[_-]?token|api[_-]?key|token)"
        r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{6,}",
        r"\1***",
        value,
    )
    try:
        value = redact_proxy_text(value)
    except Exception:  # pragma: no cover - redactor must never break dumping
        pass
    return value


def _upi_dump_dir() -> Path:
    """解析 dump 落盘目录。

    优先 ``UPI_DUMP_DIR``；否则相对**仓库根**取 ``runtime/upi_dumps``。
    仓库根用本文件位置推断（``sms_tool/upi_link.py`` 的上一级），
    不依赖 cwd——CLI 与 GUI 的 cwd 不一样。
    """
    configured = _env_str("UPI_DUMP_DIR", "")
    if configured:
        return Path(configured).expanduser()
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / UPI_DUMP_DEFAULT_DIR


def _upi_dump_http(
    response: Any,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> str:
    """把一次请求/响应落盘，返回写入的文件路径（未写则返回空串）。

    **绝不抛异常**：dump 是诊断辅助，不能因为它自己的问题（磁盘满、
    权限不足、响应对象没有 .text）把主流程带崩。所有失败都吞掉并返回 ""。

    触发条件：``force=True`` 或 ``UPI_DUMP=1``。
    调用方对 >=400 的响应传 ``force=True``——**失败现场永远要留证据**，
    哪怕总开关没开。
    """
    if not force and not _env_bool("UPI_DUMP", False):
        return ""
    global _dump_counter
    try:
        limit = _env_int("UPI_DUMP_LIMIT", 6000, minimum=500)
        with _dump_lock:
            _dump_counter += 1
            index = _dump_counter
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage or "stage"))
        directory = _upi_dump_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # 🔴 必须先 % 成 str 再 path join。``Path / "a_%04d_b.txt" % (1,)`` 会
        # 让 ``%`` 作用在 **Path 对象**上（``Path.__mod__`` 不存在）⇒ TypeError，
        # 而它被本函数的 ``except Exception`` 吞成 ""，表现为「开关开了也不落盘」。
        filename = "%s_%04d_%s.txt" % (stamp, index, safe_stage)
        path = directory / filename

        status = getattr(response, "status_code", "")
        url = getattr(response, "url", "") or request_url
        try:
            body_text = response.text if response is not None else ""
        except Exception:
            body_text = "<unreadable response body>"
        if request_body is None:
            rendered_request = ""
        else:
            try:
                rendered_request = json.dumps(request_body, ensure_ascii=False, indent=2)
            except Exception:
                rendered_request = repr(request_body)

        lines = [
            "stage: %s" % safe_stage,
            "request: %s %s" % (request_method, request_url),
            "",
            "request_body:",
            _upi_redact_for_dump(rendered_request)[:limit],
            "",
        ]
        if response is not None:
            lines.extend([
                "status: %s" % status,
                "url: %s" % url,
                "",
                "response:",
                _upi_redact_for_dump(body_text)[:limit],
                "",
            ])
        else:
            lines.extend(["response: <none>", ""])

        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return str(path)
    except Exception:
        return ""


def _approve_backoff(attempt: int, cap: float) -> float:
    """approve 重试退避：随尝试次数线性增长，被 cap 截断。

    cap=0 时完全不睡（测试用）。参考实现是 random.uniform(1, 2)，
    这里改成确定性递增，便于把「重试确实退避了」写进断言。

    返回值恒 >= 0：``time.sleep`` 收到负数会抛 ValueError，
    而 attempt 从 1 起算，正常路径不会为负——但 helper 是纯函数，
    不能依赖调用方保证，所以自己夹住。
    """
    if cap <= 0:
        return 0.0
    return max(0.0, min(0.5 * attempt, cap))


# ─── 路径 / 配置装载 (下沉副本, 与 gen_pp_link 同语义) ──────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM."""
    if os.path.abspath(path) == os.path.abspath(DEFAULT_CONFIG_PATH):
        from .config import load_merged_config
        return load_merged_config()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ─── UPI 常量 ──────────────────────────────────────────────────────────────────

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

UPI_APPROVAL_MAX_ATTEMPTS = 60
UPI_QR_POLL_MAX_ATTEMPTS = 30

# ChatGPT 客户端身份头（参考实现 build_chatgpt_session 同款值）。
# 缺这组头时 checkout 请求不像浏览器发起 ⇒ 400 unusual activity。
UPI_CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
UPI_CHATGPT_CLIENT_BUILD_NUMBER = "7904904"
UPI_QR_POLL_INTERVAL = 1.0

#: 参考实现里 ``UpiFingerprintProfile`` 的等价物：一整套互相自洽的浏览器
#: 身份。旧实现只随机 ``User-Agent`` 一处, 其余 header 用库默认值 ⇒ 自相矛盾
#: 的指纹 (UA 说 Windows 而 ``sec-ch-ua-platform`` 缺失) 是 Stripe 侧可见面。
#:
#: 🔴 ``locale`` / ``timezone`` / ``accept_language`` 是**兜底值**，
#: 调用 ``_upi_fingerprint(index, country)`` 时会被契约层
#: （``checkout_contract.COUNTRY_BROWSER_PROFILES``）覆盖。保留字段是为了让
#: 不传 country 的老调用点仍有一套完整自洽的身份，不是第二份真源。
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
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.114 Safari/537.36"
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

#: 印度账单资料池。参考实现 ``IN_BILLING_NAMES`` / ``IN_BILLING_ADDRESSES``。
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

#: 固定资料（配置 ``upi.fixed_billing`` 或环境变量 ``UPI_USE_FIXED_BILLING=1``）。
#: 键名与旧实现的 ``UPI_BILLING_IN`` 保持一致, 供兼容壳重导出。
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


def _upi_billing_profile(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """构造印度账单资料。

    优先级（后者覆盖前者）:
      随机池 → ``upi.fixed_billing`` / ``UPI_USE_FIXED_BILLING`` → 逐字段环境变量。

    参考实现 ``upi_billing_profile()`` 只有「随机池 → 固定 → 环境变量」三级,
    这里多一级「配置段」, 因为本项目配置是 JSON 而非环境变量驱动。
    """
    section = cfg if isinstance(cfg, Mapping) else {}
    first_name, last_name = UPI_BILLING_NAMES[secrets.randbelow(len(UPI_BILLING_NAMES))]
    line1, city, postal_code, state = UPI_BILLING_ADDRESSES[
        secrets.randbelow(len(UPI_BILLING_ADDRESSES))
    ]
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
            raise RuntimeError(
                "browser_profile_for_country(%r) 返回的 profile 缺字段: %r" % (country, profile)
            )
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
            session.headers["Cookie"] = (
                f"{existing_cookie}; __Secure-next-auth.session-token={session_token}"
            )
        else:
            session.headers["Cookie"] = f"__Secure-next-auth.session-token={session_token}"
    return session



def _upi_runtime_version() -> str:
    """Stripe.js 运行时版本（``version`` 字段)。

    参考实现用固定常量 ``DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"``;
    这里允许 ``UPI_STRIPE_RUNTIME_VERSION`` 覆盖以便灰度。
    """
    return _env_str("UPI_STRIPE_RUNTIME_VERSION", "6f8494a281")


def _upi_browser_id() -> str:
    """Stripe 的 guid / muid / sid 形态: uuid4 十六进制截 16 位。"""
    return uuid.uuid4().hex[:16]


def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


def _default_qr_path(prefix: str = "upi") -> str:
    directory = Path(PROJECT_ROOT) / "runtime" / "upi_qr"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png")


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
    img.save(str(path))
    return str(path)


def _write_qr_svg(data: str, qr_path: str = "") -> str:
    """把 payload 渲染成 SVG（远程 ``qr_image_url_svg`` 不可达时的本地兜底）。

    参考实现把远程 SVG/PNG URL 直接当结果返回, 没有本地渲染兜底; 客户端的
    ``artifact_kind`` 是 ``url_or_qr``, 因此这里补一个同构产物。
    """
    url = str(data or "").strip()
    if not url:
        return ""
    path = Path(qr_path or _default_qr_path("upi_svg")).with_suffix(".svg")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode
        import qrcode.image.svg
    except Exception as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("qrcode package is required for UPI QR generation; run pip install qrcode[pil]") from exc
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    img.save(str(path))
    return str(path)


# ─── UPI 辅助函数 ──────────────────────────────────────────────────────────────


def _upi_nested_get(data: Any, path: list[str]) -> Any:
    """安全地按路径取嵌套值."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _upi_amount_minor(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(value) if value == value else None  # reject NaN
    if isinstance(value, dict):
        for key in ("amount", "amount_due", "minor", "value"):
            nested = _upi_amount_minor(value.get(key))
            if nested is not None:
                return nested
    return None


def _upi_int_value(value: Any) -> tuple[int, bool]:
    """参考实现 ``_int_value``: 返回 (值, 是否真的取到)。"""
    if value is None or value == "" or value == [] or value == {}:
        return 0, False
    try:
        return int(value), True
    except (TypeError, ValueError):
        return 0, False


def _upi_bool_value(value: Any) -> bool:
    """参考实现 ``_bool_value``: 兼容 bool / 字符串 / 数字三种表述。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _upi_first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _upi_extract_payment_amount(init_data: Any) -> int:
    """从 Stripe init 响应取应收金额（最小单位）。

    参考实现 ``amount_from_payload`` 的顺序是 total_summary.due →
    invoice.amount_due → **line_items 求和** → 正则兜底。旧实现少了后两级,
    当 Stripe 把金额只放在 line_items 里时会误判成 0（→ 谎报「有免费试用」）。
    """
    if isinstance(init_data, dict):
        due = _upi_nested_get(init_data, ["total_summary", "due"])
        parsed = _upi_amount_minor(due)
        if parsed is not None:
            return parsed
        amount_due = _upi_nested_get(init_data, ["invoice", "amount_due"])
        parsed = _upi_amount_minor(amount_due)
        if parsed is not None:
            return parsed
        line_items = init_data.get("line_items")
        if isinstance(line_items, list):
            total = 0
            found = False
            for item in line_items:
                if not isinstance(item, dict):
                    continue
                amount = _upi_amount_minor(item.get("amount"))
                if amount is not None:
                    total += amount
                    found = True
            if found:
                return total
        parsed = _upi_amount_minor(_upi_nested_get(init_data, ["elements_options", "amount"]))
        if parsed is not None:
            return parsed
    # 正则兜底: 参考实现同样在结构解析失败后扫 JSON 文本。
    try:
        text = json.dumps(init_data, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(init_data or "")
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount_due"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return 0


def _upi_display_amounts(init_data: Any) -> dict[str, str]:
    """custom 模式 confirm 的 ``last_displayed_line_item_group_details``。

    参考实现 ``display_amounts_from_init``。这些字段是 Stripe 用来校验
    「服务器算出的金额与客户端屏幕上显示的一致」的, 缺失时 confirm 会被拒
    或直接被要求重新 init。全部以**字符串**提交。
    """
    init = init_data if isinstance(init_data, dict) else {}
    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    total_summary = init.get("total_summary") if isinstance(init.get("total_summary"), dict) else {}

    due, _ = _upi_int_value(
        _upi_first_non_empty(total_summary.get("due"), invoice.get("amount_due"), 0)
    )
    total, _ = _upi_int_value(
        _upi_first_non_empty(total_summary.get("total"), invoice.get("amount_due"), due)
    )
    subtotal, _ = _upi_int_value(_upi_first_non_empty(total_summary.get("subtotal"), total))

    exclusive_tax = 0
    inclusive_tax = 0
    tax_amounts = invoice.get("total_tax_amounts")
    if isinstance(tax_amounts, list):
        for item in tax_amounts:
            if not isinstance(item, dict):
                continue
            reason = str(
                item.get("taxability_reason")
                or item.get("taxability")
                or item.get("tax_behavior")
                or ""
            ).lower()
            amount, ok = _upi_int_value(
                _upi_first_non_empty(item.get("amount"), item.get("tax_amount"), 0)
            )
            if not ok:
                continue
            if "inclusive" in reason:
                inclusive_tax += amount
            else:
                exclusive_tax += amount

    discount = max(subtotal - total, 0)
    return {
        "subtotal": str(subtotal),
        "total_exclusive_tax": str(exclusive_tax),
        "total_inclusive_tax": str(inclusive_tax),
        "total_discount_amount": str(discount),
        "shipping_rate_amount": "0",
        "due": str(due),
    }


def _upi_confirm_amounts(init_data: Any, fallback_amount: Any = None) -> tuple[str, str]:
    """custom 模式 confirm 的 ``expected_amount``（及 ``expected_amount_on_bca``）。

    参考实现 ``confirm_expected_amounts_from_init``。判读顺序:
    ``line_item_group.total`` → 自动附加费开启时回到 ``total_summary.due`` →
    ``invoice.amount_due``（有 ``billing_cycle_anchor`` 且无 proration 时
    额外返回 bca 金额）。

    返回 ``(expected_amount, expected_amount_on_bca)``, 两者均为字符串,
    第二个为空字符串表示不提交该字段。
    """
    init = init_data if isinstance(init_data, dict) else {}
    fallback, ok = _upi_int_value(fallback_amount)
    expected = fallback if ok else 0

    total_summary = init.get("total_summary") if isinstance(init.get("total_summary"), dict) else {}
    total_due, has_total_due = _upi_int_value(total_summary.get("due"))
    if has_total_due:
        expected = total_due

    line_item = init.get("line_item_group") if isinstance(init.get("line_item_group"), dict) else {}
    line_total, has_line_total = _upi_int_value(line_item.get("total"))
    if has_line_total:
        expected = line_total

    auto_settings = (
        line_item.get("automatic_surcharge_settings")
        if isinstance(line_item.get("automatic_surcharge_settings"), dict)
        else {}
    )
    if _upi_bool_value(auto_settings.get("enabled")):
        due, has_due = _upi_int_value(total_summary.get("due"))
        if has_due:
            expected = due

    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    amount_due, has_amount_due = _upi_int_value(invoice.get("amount_due"))
    if has_amount_due:
        has_bca = bool(str(invoice.get("billing_cycle_anchor") or "").strip())
        if has_bca and not _upi_bool_value(invoice.get("has_prorations")):
            if has_total_due:
                return str(total_due), str(amount_due)
            return "0", str(amount_due)
        if has_total_due:
            return str(expected), ""
        expected = amount_due

    return str(expected), ""


def _upi_get_payment_method_types(init_data: Any) -> list[str]:
    candidates = [
        _upi_nested_get(init_data, ["elements_options", "payment_method_types"]),
        init_data.get("payment_method_types") if isinstance(init_data, dict) else None,
        _upi_nested_get(init_data, ["payment_method_preference", "payment_method_types"]),
        _upi_nested_get(init_data, ["session", "payment_method_types"]),
        init_data.get("ordered_payment_method_types") if isinstance(init_data, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [str(item).lower() for item in candidate]
    return []


def _upi_scan_free_trial(value: Any, depth: int = 0, signals: dict | None = None) -> dict:
    """递归搜索 Stripe init 响应中的免费试用信号."""
    if signals is None:
        signals = {"coupon_name": "", "percent_off": None, "duration_months": None}
    if depth > 8 or not value or not isinstance(value, (dict, list)):
        return signals
    if isinstance(value, list):
        for item in value:
            _upi_scan_free_trial(item, depth + 1, signals)
        return signals
    for key, next_val in value.items():
        lower_key = key.lower()
        if isinstance(next_val, str):
            lower_val = next_val.lower()
            if not signals["coupon_name"] and (
                lower_val.startswith("upi://")
                or "free trial" in lower_val
                or "1 month free" in lower_val
                or "one month free" in lower_val
                or PLUS_TRIAL_CAMPAIGN_ID in lower_val
                or "coupon" in lower_key
                or "promotion" in lower_key
            ):
                signals["coupon_name"] = next_val
        elif isinstance(next_val, (int, float)) and not isinstance(next_val, bool):
            if lower_key in ("percent_off", "percentoff"):
                signals["percent_off"] = max(signals["percent_off"] or 0, next_val)
            if lower_key in ("duration_in_months", "durationmonths"):
                signals["duration_months"] = max(signals["duration_months"] or 0, next_val)
        if next_val and isinstance(next_val, (dict, list)):
            _upi_scan_free_trial(next_val, depth + 1, signals)
    return signals


def _upi_get_free_trial_status(init_data: Any) -> dict:
    """分析 Stripe init 响应判断是否有免费试用."""
    due = _upi_extract_payment_amount(init_data)
    signals = _upi_scan_free_trial(init_data)
    pm_types = _upi_get_payment_method_types(init_data)
    coupon = signals["coupon_name"].strip()
    coupon_lower = coupon.lower()
    looks_like_trial = any(s in coupon_lower for s in ("free trial", "1 month free", "one month free", PLUS_TRIAL_CAMPAIGN_ID))
    looks_like_full_discount = (signals["percent_off"] is not None and signals["percent_off"] >= 100) or looks_like_trial
    return {
        "has_free_trial": due == 0 or (looks_like_full_discount and signals["percent_off"] is not None and signals["percent_off"] >= 100),
        "has_upi": "upi" in pm_types,
        "due": due,
        "coupon_name": coupon,
        "percent_off": signals["percent_off"],
        "duration_months": signals["duration_months"],
        "payment_method_types": pm_types,
    }


# ─── QR / 重定向数据提取 ────────────────────────────────────────────────────────

#: 参考实现 ``is_known_static_host`` 的静态资源主机黑名单。这些主机的 URL 是
#: 页面素材而不是支付指令, 混进 QR 候选会产生「看起来成功但扫不出钱」的链接。
UPI_STATIC_HOSTS = frozenset({
    "stripe-camo.global.ssl.fastly.net",
    "files.stripe.com",
    "js.stripe.com",
    "m.stripe.network",
    "q.stripe.com",
})

#: 🔴 代码类静态资源后缀。**只用于**判断「这是不是一段可执行的页面脚本/样式」,
#: 绝不能拿去过滤 QR 候选 —— QR 图本身就是 ``.png`` / ``.svg``,
#: 一旦把图片后缀并进 QR 过滤，整个 QR 通道会被清空（曾经踩过）。
UPI_CODE_RESOURCE_SUFFIXES = (
    ".js", ".css", ".map", ".woff", ".woff2", ".ttf", ".otf", ".ico",
)

_UPI_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_UPI_DATA_IMAGE_RE = re.compile(r"data:image/(?:png|svg\+xml|jpeg);base64,[A-Za-z0-9+/=]+")
_UPI_QR_HINT_RE = re.compile(r"(^|[/?&_.=-])(?:qr|qrcode|qr-code)(?:[/?&_.=-]|$)")


def _upi_url_path_extension(url: str) -> str:
    """取 URL **路径** 的扩展名（小写, 含点）。

    🔴 这里是旧实现的核心 bug 之一: 旧代码用 ``"png" in src.lower()`` 这种裸
    子串判定, 于是 ``.../image.png?format=svg&w=300`` 会被判成 svg —— query
    里的 ``svg`` 命中了, 而路径里根本没有 ``png`` 字样。
    只看 ``urlsplit(url).path`` 就不会被 query / fragment 干扰。
    """
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return ""
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return ""
    return "." + tail.rsplit(".", 1)[-1].lower()


def _upi_is_static_resource_url(url: str) -> bool:
    """参考实现 ``is_known_static_host``: **只看主机名**。

    🔴 不要在这里加「图片后缀也算静态资源」的判据。QR 图就是 ``qr.stripe.com``
    上的 ``.png`` / ``.svg``；`_upi_extract_qr_candidates` 用的正是
    ``is_qr_candidate(url) and not is_static(url)`` 这个与参考实现一致的组合，
    后缀一加进去，QR 候选会被全部过滤掉（实测：候选从 2 个变 0 个）。
    脚本/样式类资源改由 `_upi_is_code_resource_url` 单独判断。
    """
    value = str(url or "").strip()
    if not value:
        return False
    if value.lower().startswith("data:image/"):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (parsed.netloc or "").lower() in UPI_STATIC_HOSTS


def _upi_is_code_resource_url(url: str) -> bool:
    """是否 js/css/字体 这类**代码或样式**资源（不是支付内容, 也不是 QR 图）。"""
    try:
        path = urlsplit(str(url or "").strip()).path
    except ValueError:
        return False
    return (path or "").lower().endswith(UPI_CODE_RESOURCE_SUFFIXES)


def _upi_is_instructions_url(url: str) -> bool:
    """是否 ``https://payments.stripe.com/upi/instructions/...``。"""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.netloc or "").lower() == "payments.stripe.com"
        and (parsed.path or "").lower().startswith("/upi/instructions/")
    )


def _upi_is_qr_candidate(url: str) -> bool:
    """参考实现 ``is_qr_candidate``。

    注意 ``qr.stripe.com`` 这种 **主机名里带 qr** 的 URL 也会命中 —— 用
    ``urlsplit`` 把 netloc+path+query 拼起来再匹配, 而不是只看 path。
    """
    value = str(url or "").strip()
    if not value:
        return False
    if value.lower().startswith("data:image/"):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    text = f"{parsed.netloc}{parsed.path}?{parsed.query}".lower()
    return bool(_UPI_QR_HINT_RE.search(text))


def _upi_qr_image_kind(url: str) -> str:
    """按 **路径扩展名** 判定 QR 图类型, 返回 ``"png"`` / ``"svg"`` / ``"jpg"`` / ``""``。"""
    extension = _upi_url_path_extension(url)
    if extension == ".png":
        return "png"
    if extension == ".svg":
        return "svg"
    if extension in (".jpeg", ".jpg"):
        return "jpg"
    return ""


def _upi_merge_qr_key(result: dict, key: str, value: Any) -> None:
    """将 UPI QR 数据字段合并到 result dict."""
    if value is None:
        return
    normalized_key = key.lower()
    if isinstance(value, str):
        if value.startswith("upi://") and not result.get("upi_uri"):
            result["upi_uri"] = value
            result["mobile_auth_url"] = value
        elif value.startswith("https://payments.stripe.com/upi/instructions/") and not result.get("hosted_instructions_url"):
            result["hosted_instructions_url"] = value
        elif value.startswith("https://qr.stripe.com/"):
            # 按路径扩展名分流。扩展名无法判定时**不默认成 svg**（旧实现的错误
            # 行为），而是两个通道都登记，让调用方用真实可达性择优。
            kind = _upi_qr_image_kind(value)
            if kind == "png":
                result.setdefault("qr_image_url_png", value)
            elif kind == "svg":
                result.setdefault("qr_image_url_svg", value)
            elif kind == "jpg":
                result.setdefault("qr_image_url_png", value)
            else:
                result.setdefault("qr_image_url_png", value)
                result.setdefault("qr_image_url_svg", value)
    known_keys = {
        "hosted_instructions_url": "hosted_instructions_url",
        "mobile_auth_url": "mobile_auth_url",
        "upi_uri": "upi_uri",
        "image_url_svg": "qr_image_url_svg",
        "qr_image_url_svg": "qr_image_url_svg",
        "image_url_png": "qr_image_url_png",
        "qr_image_url_png": "qr_image_url_png",
    }
    if normalized_key in known_keys and isinstance(value, str) and value:
        out_key = known_keys[normalized_key]
        result.setdefault(out_key, value)
    if normalized_key in (
        "expires_at",
        "expires_after_timestamp",
        "qr_expires_at",
        "expires_at_timestamp",
    ):
        try:
            expires = int(value)
            if expires > 0 and not result.get("expires_at"):
                result["expires_at"] = expires
        except (ValueError, TypeError):
            pass


def _upi_extract_next_action(data: Any) -> dict:
    """递归遍历 Stripe 响应提取 UPI QR 数据."""
    result: dict[str, Any] = {}

    def walk(value: Any, key: str = "") -> None:
        _upi_merge_qr_key(result, key, value)
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for child_key, child_value in value.items():
            if child_key == "qr_code" and isinstance(child_value, dict):
                _upi_merge_qr_key(result, "qr_expires_at", child_value.get("expires_at"))
                _upi_merge_qr_key(result, "image_url_svg", child_value.get("image_url_svg"))
                _upi_merge_qr_key(result, "image_url_png", child_value.get("image_url_png"))
            walk(child_value, child_key)

    walk(data)
    return result


def _upi_collect_urls(payload: Any, found: list[str] | None = None) -> list[str]:
    """参考实现 ``collect_urls``: 递归收集 payload 里所有 http(s) URL 与 data:image。"""
    if found is None:
        found = []
    if isinstance(payload, str):
        for match in _UPI_URL_RE.findall(payload):
            found.append(match.rstrip("),.;]"))
        for match in _UPI_DATA_IMAGE_RE.findall(payload):
            found.append(match)
    elif isinstance(payload, dict):
        for value in payload.values():
            _upi_collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            _upi_collect_urls(item, found)
    return found


def _upi_extract_qr_candidates(payload: Any) -> list[str]:
    """参考实现 ``extract_qr_candidates``: 全量收集 QR 候选并去重。

    旧实现只在 HTML 分支取**首个** ``<img>`` 就 ``break``, 一旦首个是占位图或
    素材图就直接丢结果。这里与参考实现对齐: 收集全部候选, 去重, 剔除静态资源。
    """
    seen: set[str] = set()
    result: list[str] = []
    for url in _upi_collect_urls(payload):
        if url in seen:
            continue
        seen.add(url)
        if _upi_is_qr_candidate(url) and not _upi_is_static_resource_url(url):
            result.append(url)
    return result


def _upi_extract_redirect_url(payload: Any) -> str:
    """参考实现 ``extract_redirect_url``: 提取真实的支付跳转 URL。

    判读顺序（与参考实现一致）:
      1. ``next_action.hosted_instructions_url``（必须是 payments.stripe.com/upi/instructions/）
      2. ``next_action.redirect_to_url.url``
      3. ``next_action`` 下的 url / redirect_url / redirect_to_url / hosted_url
      4. 顶层 ``hosted_instructions_url`` / ``redirect_url`` / ``redirect_to_url``
         / ``authorization_url`` / ``authentication_url``
      5. 递归下钻

    旧实现**完全没有**这层: ``_upi_extract_next_action`` 只认 6 个标量 key, 拿不到
    ``redirect_to_url`` ⇒ 直接落到 hosted fallback。这是「明明有 upi:// 却返回
    hosted_url」的机制性原因。
    """

    def redirect_like(url: Any, from_action_field: bool = False) -> str:
        value = str(url or "").strip()
        if not value.startswith(("http://", "https://")):
            return ""
        if _upi_is_static_resource_url(value):
            return ""
        if _upi_is_instructions_url(value):
            return value
        if from_action_field:
            return value
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        host = (parsed.netloc or "").lower()
        text = f"{host}{(parsed.path or '').lower()}?{(parsed.query or '').lower()}"
        if host in {"hooks.stripe.com", "payments.stripe.com"}:
            return value
        if any(part in text for part in ("upi", "/redirect/", "redirect_to_url", "authenticate")):
            return value
        return ""

    def walk(payload: Any) -> str:
        if isinstance(payload, dict):
            next_action = payload.get("next_action")
            if isinstance(next_action, dict):
                hosted = redirect_like(next_action.get("hosted_instructions_url"))
                if hosted:
                    return hosted
                redirect = next_action.get("redirect_to_url")
                if isinstance(redirect, dict):
                    url = redirect_like(redirect.get("url"), True)
                    if url:
                        return url
                for key in (
                    "url",
                    "redirect_url",
                    "redirect_to_url",
                    "hosted_url",
                    "hosted_instructions_url",
                ):
                    url = redirect_like(next_action.get(key), True)
                    if url:
                        return url

            for key in (
                "hosted_instructions_url",
                "redirect_url",
                "redirect_to_url",
                "authorization_url",
                "authentication_url",
            ):
                url = redirect_like(payload.get(key), True)
                if url:
                    return url

            for value in payload.values():
                nested = walk(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = walk(item)
                if nested:
                    return nested
        return ""

    return walk(payload)


def _upi_first_value_by_key(payload: Any, key: str) -> Any:
    """参考实现 ``first_value_by_key``: 深度优先找第一个非空同名 key。"""
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _upi_first_value_by_key(value, key)
            if found is not None and found != "" and found != [] and found != {}:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _upi_first_value_by_key(item, key)
            if found is not None and found != "" and found != [] and found != {}:
                return found
    return None


def _upi_find_submission_attempt(payload: Any) -> dict[str, Any]:
    """参考实现 ``find_submission_attempt``。"""
    if isinstance(payload, dict):
        value = payload.get("submission_attempt")
        if isinstance(value, dict):
            return value
        for item in payload.values():
            nested = _upi_find_submission_attempt(item)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _upi_find_submission_attempt(item)
            if nested:
                return nested
    return {}


def _upi_setup_intent_last_error(payload: Any, current_pm_id: str = "") -> str:
    """参考实现 ``setup_intent_last_error``: 找 SetupIntent 的失败原因。

    只在 ``payment_method`` 与当前提交的 ``pm_id`` 一致时才算数 —— 否则会把
    **上一次**尝试的残留错误当成本次失败, 造成假阴性。
    """
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        is_setup_intent = payload.get("object") == "setup_intent" or payload_id.startswith("seti_")
        last_error = payload.get("last_setup_error") if is_setup_intent else None
        setup_intent = payload.get("setup_intent")
        if not last_error and isinstance(setup_intent, dict):
            last_error = setup_intent.get("last_setup_error")
        if last_error:
            if current_pm_id and isinstance(last_error, dict):
                error_pm = last_error.get("payment_method")
                error_pm_id = ""
                if isinstance(error_pm, dict):
                    error_pm_id = str(error_pm.get("id") or "").strip()
                elif isinstance(error_pm, str):
                    error_pm_id = error_pm.strip()
                if error_pm_id and error_pm_id != current_pm_id:
                    last_error = None
            if last_error:
                try:
                    return json.dumps(last_error, ensure_ascii=False)[:700]
                except (TypeError, ValueError):
                    return str(last_error)[:700]
        for value in payload.values():
            found = _upi_setup_intent_last_error(value, current_pm_id=current_pm_id)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _upi_setup_intent_last_error(value, current_pm_id=current_pm_id)
            if found:
                return found
    return ""


#: 参考实现 ``provider_decline_message`` 的中文前导。命中即视为 Stripe 风控拒绝,
#: 该 checkout 已不可用（换代理重试同一 cs_id 也没意义）。
UPI_PROVIDER_DECLINE_MARKER = "Stripe 风控拒绝"


def _upi_is_provider_decline_text(text: Any) -> bool:
    value = str(text or "").lower()
    return "generic_decline" in value or "provider_decline" in value


def _upi_provider_decline_message(context: str) -> str:
    return f"{UPI_PROVIDER_DECLINE_MARKER}: {context} setup_intent.last_setup_error 命中 generic_decline"


def _upi_raise_if_setup_intent_blocked(payload: Any, context: str, current_pm_id: str = "") -> None:
    """参考实现 ``raise_if_setup_intent_blocked``。

    旧实现完全不判读 SetupIntent 的 ``last_setup_error`` ⇒ ``generic_decline``
    会被当成「还在等待」继续轮询, 白烧 30 次额度后才超时。
    """
    last_error = _upi_setup_intent_last_error(payload, current_pm_id=current_pm_id)
    if not last_error:
        return
    if "generic_decline" in last_error.lower():
        raise RuntimeError(_upi_provider_decline_message(context))
    raise RuntimeError(f"{context}: setup_intent.last_setup_error: {last_error}")


def _upi_decode_base64url_json(raw: str) -> Any:
    """解码 Stripe hosted instructions 里 ``data-message`` 的 base64url JSON。

    🔴 旧实现是 ``raw.replace("-", "+").replace("_", "/")`` 之后 ``b64decode``。
    这个改写是**有损**的: ``-``/``_`` 属于 url-safe 字母表, 但替换后的 ``+``/``/``
    在 url-safe 串里是**非法字符**（url-safe 表里本来没有 ``+``）, 解码必抛异常;
    而异常被外层 ``except Exception: pass`` 吞掉, 于是整条 payload 静默丢失。
    实测: ``'eyJhIjoxfQ--'.replace('-', '+')`` → ``'eyJhIjoxfQ++'``。

    正确做法是用 ``urlsafe_b64decode`` 并补足 padding。这里按
    [url-safe, 标准] × [两套字母表] 依次试探, 保证两种编码都能解出。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    mangled = text.replace("-", "+").replace("_", "/")
    if mangled != text:
        candidates.append(mangled)
    for candidate in candidates:
        padded = candidate + "=" * (-len(candidate) % 4)
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                return json.loads(decoder(padded).decode("utf-8"))
            except Exception:
                continue
    return None


def _upi_extract_qr_from_html(html: str) -> dict:
    """从 Stripe hosted instructions HTML 页面解析 UPI QR 数据."""
    result: dict[str, Any] = {}
    # 解析 <meta id="payload" data-message="..." />
    meta_match = re.search(r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)["\']', html, re.I)
    if not meta_match:
        meta_match = re.search(r'<meta\b[^>]*\bdata-message=["\']([^"\']+)["\'][^>]*\bid=["\']payload["\']', html, re.I)
    if meta_match:
        raw = meta_match.group(1).replace("&quot;", '"')
        payload = _upi_decode_base64url_json(raw)
        if isinstance(payload, dict):
            _upi_merge_qr_key(result, "mobile_auth_url", payload.get("mobile_auth_url"))
            _upi_merge_qr_key(result, "upi_uri", payload.get("upi_uri"))
            _upi_merge_qr_key(
                result,
                "expires_at",
                _upi_first_non_empty(
                    payload.get("expires_at"),
                    payload.get("expires_after_timestamp"),
                ),
            )
    # 解析全部 <img src="https://qr.stripe.com/..." />（旧实现只取首个就 break）
    for img_match in re.finditer(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
        src = img_match.group(1).replace("&amp;", "&")
        tag = img_match.group(0)
        if "qr.stripe.com" in src or "QRCode-image" in tag:
            kind = _upi_qr_image_kind(src)
            if kind == "png":
                _upi_merge_qr_key(result, "qr_image_url_png", src)
            elif kind == "svg":
                _upi_merge_qr_key(result, "qr_image_url_svg", src)
            elif kind == "jpg":
                _upi_merge_qr_key(result, "qr_image_url_png", src)
            else:
                # 扩展名无法判定时两个通道都尝试登记, 由调用方择优;
                # 绝不按旧逻辑默认成 svg。
                _upi_merge_qr_key(result, "qr_image_url_png", src)
                _upi_merge_qr_key(result, "qr_image_url_svg", src)
    return result


def _upi_hydrate_qr_data(
    qr_data: dict,
    proxy_url: str,
    fingerprint: Mapping[str, str] | None = None,
) -> dict:
    """如果 JSON 中没有 upi://，访问 hosted_instructions_url 从 HTML 中解析.

    旧实现的守卫是 ``if hosted_url and not result.get("upi_uri")`` —— 只要 JSON
    任何角落出现过 ``upi://`` 字符串（包括**描述文案**里的示意串）就不再 hydrate,
    导致真正的深链取不到。这里改为: 只要还没有以 ``upi://`` 开头的 ``upi_uri``
    **且** 有 ``hosted_instructions_url`` 就去抓页面；抓到后按「深链优先」合并。
    """
    result = dict(qr_data)
    hosted_url = result.get("hosted_instructions_url")
    if not hosted_url:
        return result
    if str(result.get("upi_uri") or "").startswith("upi://"):
        return result
    try:
        session = _new_session(proxy_url)
        _upi_apply_fingerprint(session, fingerprint or {})
        resp = session.get(hosted_url, timeout=DEFAULT_TIMEOUT, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://js.stripe.com/",
        })
        if resp.status_code < 400:
            _upi_dump_http(resp, "hydrate_html", None, "GET", str(hosted_url),
                           force=resp.status_code >= 400)
            extracted = _upi_extract_qr_from_html(resp.text)
            for k, v in extracted.items():
                if v and (k == "upi_uri" or not result.get(k)):
                    result[k] = v
        else:
            _upi_dump_http(resp, "hydrate_html", None, "GET", str(hosted_url), force=True)
    except Exception as exc:
        _emit("hydrate", f"hydrate failed (non-fatal): {type(exc).__name__}: {exc}")
    return result


def _upi_resolve_external_redirect(session: Any, start_url: str, max_hops: int = 5) -> str:
    """参考实现 ``resolve_external_redirect``: 跟随跳转直到 instructions 页。

    旧实现拿到的 ``redirect_url`` 直接返回, 不会再跟一跳 ⇒ 交付给用户的是
    ``hooks.stripe.com`` 之类的中间跳转, 而不是真能扫的指令页。
    """
    if not start_url or not _env_bool("UPI_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for _hop in range(1, max_hops + 1):
        if _upi_is_instructions_url(current):
            return current
        try:
            resp = session.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        except Exception as exc:
            _emit("redirect", f"follow redirect failed (non-fatal): {type(exc).__name__}: {exc}")
            return current
        location = str(resp.headers.get("location") or resp.headers.get("Location") or "")
        if not location:
            return current
        current = urljoin(current, location)
    return current


# ─── 配置 / 代理解析 ───────────────────────────────────────────────────────────


def _method_cfg(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    section = cfg.get(method) if isinstance(cfg.get(method), dict) else {}
    return section if isinstance(section, dict) else {}


def _payment_stage_proxies_from_config(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    method_cfg = _method_cfg(cfg, method)
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    def pick(key: str, fallback: str = "") -> str:
        value = _stage_proxy_value(method_stage, key)
        if value:
            return value
        return _stage_proxy_value(paypal_stage, key, fallback)

    checkout = pick("checkout", proxy_default)
    provider = pick("provider") or pick("stripe_init") or proxy_default
    approve = pick("approve") or pick("confirm") or provider or proxy_default
    return {"checkout": checkout, "provider": provider, "approve": approve}


# ─── custom 模式请求载荷构造 ────────────────────────────────────────────────────


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
    expected_amount, expected_amount_on_bca = _upi_confirm_amounts(
        init_payload, ctx.get("checkout_amount")
    )
    displayed = _upi_display_amounts(init_payload)
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
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(
            ctx.get("elements_session_config_id") or ""
        ),
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
        body.update({
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
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
        })
        if billing.get("state"):
            body["payment_method_data[billing_details][address][state]"] = str(billing["state"])
        if billing.get("line2"):
            body["payment_method_data[billing_details][address][line2]"] = str(billing["line2"])
    else:
        body["payment_method"] = pm_id

    body.update({
        "browser_locale": str(fingerprint.get("locale") or "en-IN"),
        "browser_timezone": str(fingerprint.get("timezone") or "Asia/Kolkata"),
    })
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
    _upi_dump_http(resp, stage, data if data is not None else json_body, "POST", url,
                   force=resp.status_code >= 400)
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
    _upi_dump_http(resp_retry, f"{stage}_degraded", data if data is not None else json_body,
                   "POST", url, force=resp_retry.status_code >= 400)
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
        stripe, init_url, data=body, fingerprint=fingerprint, stage="stripe_init",
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"stripe init failed: {resp.status_code} {str(getattr(resp, 'text', ''))[:300]}"
        )
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
        resp, "stripe_create_pm", body, "POST", STRIPE_PAYMENT_METHODS_URL,
        force=resp.status_code >= 400,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"create UPI payment method failed: {resp.status_code} {str(getattr(resp, 'text', ''))[:400]}"
        )
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(
            f"create UPI payment method returned bad payload: {str(getattr(resp, 'text', ''))[:300]}"
        )
    return pm_id


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
        "setup_intents"
        if intent_object == "setup_intent" or intent_id.startswith("seti_")
        else "payment_intents"
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
            redirect_url = _upi_intent_redirect_url(
                stripe, intent_payload, stripe_pk, current_pm_id=current_pm_id
            )
            if redirect_url:
                return redirect_url
    return ""


def _upi_poll_payment_page(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    ctx: Mapping[str, Any],
    current_pm_id: str = "",
) -> tuple[str, list[str]]:
    """参考实现 ``poll_payment_page``: 轮询到「真跳转 / QR」或终态。

    返回 ``(redirect_url, qr_candidates)``。与旧实现的关键差异:

    * 识别终态 ``requires_approval``（继续等）与 ``failed``（含 generic_decline →
      直接判风控拒绝, 不再空转）。
    * 每轮都判读 SetupIntent 的 ``last_setup_error``。
    * 拿到 ``next_action`` 但没有直接可用的 URL 时, 还会追问 intent 一次。
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
        intent_redirect = _upi_payload_intent_redirect_url(
            stripe, payload, stripe_pk, current_pm_id=current_pm_id
        )
        if intent_redirect:
            return intent_redirect, qr_urls
        last_error = str(summary_text or "waiting")
        time.sleep(1)

    raise RuntimeError(f"redirect url resolution timeout: {last_error}")


#: 参考实现 ``should_retry_second_confirm_after_approve``: 这几类错误说明
#: confirm 之后的状态漂移了, 刷新 init 再 confirm 一次才有救。
UPI_SECOND_CONFIRM_MARKERS = (
    "checkout_upcoming_invoice_mismatch",
    "redirect url resolution timeout",
    "missing_redirect",
)


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
        "ok": True, "payment_method": "upi", "method": "upi",
        "link_type": "upi_hosted_fallback", "url": hosted_url, "qr_data": hosted_url,
        "qr_path": written_qr_path, "cs_id": cs_id, "processor_entity": processor_entity,
        "amount": amount, "currency": payment_currency.upper(),
        "target_country": target_country, "checkout_country": checkout_country,
        "billing_country": checkout_country, "payment_country": payment_country,
        "payment_method_types": pm_types, "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy, "approve_proxy": approve_proxy,
        "checkout_ui_mode": checkout_ui_mode,
    }
    if warning:
        result["warning"] = warning
    return result


# ─── 主流水线 ─────────────────────────────────────────────────────────────────


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
        payment_country
        or upi_cfg.get("payment_country")
        or upi_cfg.get("payment_method_country")
        or "IN"
    ).upper()
    target_country = checkout_country
    currency = CURRENCY_MAP.get(checkout_country, "INR")
    payment_currency = CURRENCY_MAP.get(payment_country, "INR")
    if require_zero is None:
        paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
        require_zero = bool(upi_cfg.get("require_zero_due", paypal_cfg.get("require_zero_due", True)))

    # 协议与策略开关（配置段优先, 环境变量兜底）
    checkout_ui_mode = str(
        upi_cfg.get("checkout_ui_mode")
        or _env_str("UPI_CHECKOUT_UI_MODE", "custom")
        or "custom"
    ).strip().lower()
    if checkout_ui_mode not in {"custom", "hosted"}:
        checkout_ui_mode = "custom"
    inline_pm = bool(
        upi_cfg.get("confirm_inline_pm")
        if "confirm_inline_pm" in upi_cfg
        else _env_bool("UPI_CONFIRM_INLINE_PM", True)
    )
    update_tax_region = bool(
        upi_cfg.get("update_tax_region")
        if "update_tax_region" in upi_cfg
        else _env_bool("UPI_UPDATE_TAX_REGION", True)
    )
    update_customer_data = bool(
        upi_cfg.get("update_customer_data")
        if "update_customer_data" in upi_cfg
        else _env_bool("UPI_UPDATE_CUSTOMER_DATA", False)
    )
    max_approve_attempts = _env_int("UPI_APPROVAL_MAX_ATTEMPTS", UPI_APPROVAL_MAX_ATTEMPTS)
    poll_max_attempts = _env_int("UPI_QR_POLL_MAX_ATTEMPTS", UPI_QR_POLL_MAX_ATTEMPTS)
    # approve 重试之间的退避上限（秒）。参考实现是 random.uniform(1, 2)，
    # 这里做成可调：默认 1.5s，测试里置 0 即可让 60 次重试瞬间跑完。
    approve_backoff_cap = _float_env("UPI_APPROVAL_BACKOFF", 1.5)
    approve_backoff_cap = max(0.0, approve_backoff_cap)

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
        update_tax_region=update_tax_region,
        update_customer_data=update_customer_data,
        max_approve_attempts=max_approve_attempts,
        poll_max_attempts=poll_max_attempts,
        approve_backoff_cap=approve_backoff_cap,
        fingerprint_index=fingerprint_index,
        fingerprint=fingerprint,
        billing=billing,
        device_id=device_id,
        session_token=session_token,
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
        access_token=access_token, proxy=proxy,
        checkout_proxy=checkout_proxy, provider_proxy=provider_proxy, approve_proxy=approve_proxy,
        target_country=target_country, checkout_country=checkout_country, payment_country=payment_country,
        require_zero=require_zero, runtime_config=runtime_config, device_id=device_id,
        session_token=session_token,
    )
    cfg = _rc.cfg
    upi_cfg = _rc.upi_cfg
    stage_proxies = _rc.stage_proxies
    checkout_proxy = _rc.checkout_proxy
    provider_proxy = _rc.provider_proxy
    approve_proxy = _rc.approve_proxy
    regions = _rc.regions
    checkout_country = _rc.checkout_country
    payment_country = _rc.payment_country
    target_country = _rc.target_country
    currency = _rc.currency
    payment_currency = _rc.payment_currency
    require_zero = _rc.require_zero
    checkout_ui_mode = _rc.checkout_ui_mode
    inline_pm = _rc.inline_pm
    update_tax_region = _rc.update_tax_region
    update_customer_data = _rc.update_customer_data
    max_approve_attempts = _rc.max_approve_attempts
    poll_max_attempts = _rc.poll_max_attempts
    approve_backoff_cap = _rc.approve_backoff_cap
    fingerprint_index = _rc.fingerprint_index
    fingerprint = _rc.fingerprint
    billing = _rc.billing
    device_id = _rc.device_id
    session_token = _rc.session_token

    emit = _emit

    try:
        # ── Stage 1: ChatGPT checkout ────────────────────────────────────
        emit("checkout", f"Stage 1: using {checkout_proxy or 'DIRECT'} for UPI checkout (ui_mode={checkout_ui_mode})")
        cs = _upi_new_chatgpt_session(checkout_proxy, fingerprint, device_id, session_token)
        cs.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
            # 网关路由头（参考实现在请求级传；本 session 只打这一个
            # 端点，放 session 头等价且不破坏 FakeSession.post 签名）
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        })
        checkout_body: dict[str, Any] = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": PLUS_TRIAL_CAMPAIGN_ID, "is_coupon_from_query_param": False},
            "checkout_ui_mode": checkout_ui_mode,
        }
        r = cs.post(UPI_CHECKOUT_URL, json=checkout_body, timeout=CHATGPT_TIMEOUT)
        _upi_dump_http(r, "chatgpt_checkout", checkout_body, "POST", UPI_CHECKOUT_URL,
                       force=r.status_code >= 400)
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "payment_method": "upi"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "payment_method": "upi"}
        checkout_data = r.json() or {}
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("id", "")
        if not str(cs_id).startswith("cs_"):
            return {"ok": False, "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}", "error_code": "checkout_bad_response", "payment_method": "upi"}
        stripe_pk = checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
        processor_entity = checkout_data.get("processor_entity") or ("openai_llc" if checkout_country == "US" else "openai_ie")
        emit("checkout", f"checkout success: cs_id={cs_id}")

        # ── Stage 2: Stripe init (custom mode) ───────────────────────────
        emit("stripe_init", f"Stage 2: using {provider_proxy or 'DIRECT'} for Stripe init")
        stripe = _new_session(provider_proxy)
        _upi_apply_fingerprint(stripe, fingerprint)
        stripe_js_id = uuid.uuid4().hex
        init = _upi_stripe_init(stripe, cs_id, stripe_pk, fingerprint, stripe_js_id)
        ctx = _upi_build_ctx(init, fingerprint, stripe_js_id)
        emit("stripe_init", "init success, analyzing free trial...")

        # ── Stage 3: Free trial detection ────────────────────────────────
        ft_status = _upi_get_free_trial_status(init)
        amount = ft_status["due"]
        pm_types = ft_status["payment_method_types"]
        emit("stripe_init", f"free_trial={ft_status['has_free_trial']} due={amount} coupon={ft_status['coupon_name']} upi={ft_status['has_upi']}")
        # 0 元缓存：本轮 checkout 代理有没有产出免费试用，记下来供后续批次调度。
        # 必须在 ``require_zero`` 的早退**之前**记——那次失败恰恰是最有价值的
        # 负样本（"这个代理出的是非零"），早退掉就永远学不到。
        _upi_record_zero_result(proxy_state, checkout_proxy, checkout_country, amount)
        if require_zero and not ft_status["has_free_trial"]:
            return {
                "ok": False, "error": f"no_free_trial: due={amount} coupon={ft_status['coupon_name']} percent_off={ft_status['percent_off']}",
                "error_code": "no_free_trial", "payment_method": "upi", "cs_id": cs_id,
                "amount": amount, "currency": payment_currency.upper(),
                "target_country": target_country, "checkout_country": checkout_country,
                "billing_country": checkout_country, "payment_country": payment_country,
                "coupon_name": ft_status["coupon_name"], "percent_off": ft_status["percent_off"],
            }
        if pm_types and not ft_status["has_upi"]:
            return {"ok": False, "error": f"UPI not available for checkout; payment_method_types={pm_types}", "error_code": "upi_not_available", "payment_method": "upi", "cs_id": cs_id, "payment_method_types": pm_types, "amount": amount, "currency": payment_currency.upper(), "target_country": target_country, "checkout_country": checkout_country, "billing_country": checkout_country, "payment_country": payment_country}

        # ── Stage 4: Tax region + customer data sync ─────────────────────
        if update_tax_region:
            emit("tax_region", "Stage 4: updating tax region to IN")
            tax_body: dict[str, str] = {
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
                    data=tax_body, timeout=DEFAULT_TIMEOUT,
                )
            except Exception as exc:
                emit("tax_region", f"tax region transport error (non-fatal): {type(exc).__name__}: {exc}")
                tax_resp = None
            if tax_resp is not None:
                _upi_dump_http(
                    tax_resp, "stripe_tax_region", tax_body, "POST",
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    force=tax_resp.status_code >= 400,
                )
                if tax_resp.status_code >= 400:
                    emit("tax_region", f"tax region update failed (non-fatal): {tax_resp.status_code} {tax_resp.text[:200]}")
                else:
                    emit("tax_region", "tax region updated")
                    refreshed = tax_resp.json() or {}
                    if isinstance(refreshed, dict) and refreshed:
                        init = refreshed
                        ctx = _upi_build_ctx(init, fingerprint, stripe_js_id)

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
                    data=customer_body, timeout=DEFAULT_TIMEOUT,
                )
                _upi_dump_http(
                    cd_resp, "stripe_customer_data", customer_body, "POST",
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    force=cd_resp.status_code >= 400,
                )
                if cd_resp.status_code >= 400:
                    emit("customer_data", f"customer_data failed (non-fatal): {cd_resp.status_code} {cd_resp.text[:200]}")
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
        )
        confirm_resp, confirm_fingerprint = _upi_post_with_degrade(
            stripe,
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
            data=confirm_body, fingerprint=fingerprint, stage="stripe_confirm",
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
                cs_id=cs_id, processor_entity=processor_entity, init=init, amount=amount,
                payment_currency=payment_currency, target_country=target_country,
                checkout_country=checkout_country, payment_country=payment_country,
                pm_types=pm_types, checkout_proxy=checkout_proxy,
                provider_proxy=provider_proxy, approve_proxy=approve_proxy,
                checkout_ui_mode=checkout_ui_mode, qr_path=qr_path,
                warning=f"stripe_confirm_failed: {confirm_resp.status_code}",
            )
        confirm_data = confirm_resp.json() or {}
        emit("stripe_confirm", "confirm success")
        # SetupIntent 失败判读（旧实现完全没有这一步）
        _upi_raise_if_setup_intent_blocked(confirm_data, "stripe confirm", current_pm_id=pm_id)

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
        needs_approval = (
            not confirm_redirect
            and submission_state == "requires_approval"
        )
        needs_final_poll = (
            not confirm_redirect
            and not confirm_qr_urls
            and submission_state != "requires_approval"
        )
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
            emit("approve", f"Stage 6: ChatGPT approve using {approve_proxy or 'DIRECT'}")
            approve_session = _upi_new_chatgpt_session(approve_proxy, fingerprint, device_id, session_token)
            approve_session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            })
            # Try confirm endpoint first
            try:
                confirm_chatgpt = approve_session.post(
                    UPI_CHECKOUT_CONFIRM_URL,
                    json={"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                    timeout=CHATGPT_TIMEOUT,
                )
                confirm_json = (
                    confirm_chatgpt.json() or {}
                    if confirm_chatgpt.status_code < 400
                    else {}
                )
                _upi_dump_http(
                    confirm_chatgpt, "chatgpt_approve_confirm",
                    {"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                    "POST", UPI_CHECKOUT_CONFIRM_URL,
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
                            approve_resp, f"chatgpt_approve_{attempt:02d}",
                            {"checkout_session_id": cs_id, "processor_entity": processor_entity},
                            "POST", UPI_CHECKOUT_APPROVE_URL,
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
                                # 参考实现: blocked 时保留当前线路刷新客户端标识重试,
                                # 不把它记成代理失败。旧实现只会一路撞到 60 次上限。
                                blocked_count += 1
                                emit("approve", f"attempt {attempt}: blocked, refreshing client ids")
                                # blocked 时换一套身份重试：同样要跟契约层的
                                # locale/timezone 对齐，否则新 UA 会带着旧语言。
                                _upi_apply_fingerprint(
                                    approve_session, _upi_fingerprint(country=payment_country)
                                )
                                if attempt < max_approve_attempts:
                                    time.sleep(_approve_backoff(attempt, approve_backoff_cap))
                                continue
                            approval_data = approve_json
                        elif attempt % 10 == 0:
                            emit("approve", f"attempt {attempt}/{max_approve_attempts}: status={approve_resp.status_code}")
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

        # Poll Stripe payment page until a real redirect / QR / upi:// appears
        if not redirect_url and not qr_data.get("upi_uri"):
            for _attempt in range(1, max(1, poll_max_attempts) + 1):
                if redirect_url or qr_data.get("upi_uri"):
                    break
                try:
                    poll_redirect, poll_qr = _upi_poll_payment_page(
                        stripe, cs_id, stripe_pk, ctx, current_pm_id=pm_id
                    )
                except Exception as exc:
                    if _upi_should_retry_second_confirm(exc):
                        emit("poll", f"extraction needs a second confirm: {str(exc)[:160]}")
                        break
                    raise
                if poll_redirect:
                    redirect_url = poll_redirect
                for url in poll_qr:
                    kind = _upi_qr_image_kind(url)
                    if kind == "svg":
                        qr_data.setdefault("qr_image_url_svg", url)
                    elif kind in ("png", "jpg"):
                        qr_data.setdefault("qr_image_url_png", url)
                break

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
                ctx = _upi_build_ctx(init, fingerprint, stripe_js_id)
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
                                _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or ""))
                                or return_url
                            ),
                        )
                        second_resp = stripe.post(
                            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
                            data=second_body, timeout=DEFAULT_TIMEOUT,
                        )
                        _upi_dump_http(
                            second_resp, "stripe_second_confirm", second_body, "POST",
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
        hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or f"https://pay.openai.com/c/pay/{cs_id}"
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
        return {
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
            "checkout_ui_mode": checkout_ui_mode,
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "approve_proxy": approve_proxy,
        }
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

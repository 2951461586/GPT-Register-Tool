"""Pure UPI data-extraction layer (offline: no network, no shared state).

Split out of ``sms_tool/upi_link.py`` so the amount / free-trial / QR /
redirect parsers can be unit-tested without importing the network pipeline.
``sms_tool.upi_link`` re-exports every name defined here, so
``upi_link._upi_*`` attribute access keeps working unchanged.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..checkout_contract import PLUS_TRIAL_CAMPAIGN_ID


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
    except Exception:
        text = str(init_data) if init_data is not None else ""
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount_due"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            value, _ = _upi_int_value(match.group(1))
            return value
    return 0


def _upi_display_amounts(init_data: Any) -> dict[str, str]:
    """custom 模式 confirm 的 ``last_displayed_line_item_group_details``。

    参考实现 ``display_amounts_from_init``。这些字段是 Stripe 用来校验
    「服务器算出的金额与客户端屏幕上显示的一致」的, 缺失时 confirm 会被拒
    或直接被要求重新 init。全部以**字符串**提交。
    """
    init = init_data if isinstance(init_data, dict) else {}
    _invoice = init.get("invoice")
    invoice: dict[str, Any] = _invoice if isinstance(_invoice, dict) else {}
    _total_summary = init.get("total_summary")
    total_summary: dict[str, Any] = _total_summary if isinstance(_total_summary, dict) else {}

    due, _ = _upi_int_value(_upi_first_non_empty(total_summary.get("due"), invoice.get("amount_due"), 0))
    total, _ = _upi_int_value(_upi_first_non_empty(total_summary.get("total"), invoice.get("amount_due"), due))
    subtotal, _ = _upi_int_value(_upi_first_non_empty(total_summary.get("subtotal"), total))

    exclusive_tax = 0
    inclusive_tax = 0
    tax_amounts = invoice.get("total_tax_amounts")
    if isinstance(tax_amounts, list):
        for item in tax_amounts:
            if not isinstance(item, dict):
                continue
            reason = str(
                item.get("taxability_reason") or item.get("taxability") or item.get("tax_behavior") or ""
            ).lower()
            amount, ok = _upi_int_value(_upi_first_non_empty(item.get("amount"), item.get("tax_amount"), 0))
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

    _total_summary = init.get("total_summary")
    total_summary: dict[str, Any] = _total_summary if isinstance(_total_summary, dict) else {}
    total_due, has_total_due = _upi_int_value(total_summary.get("due"))
    if has_total_due:
        expected = total_due

    _line_item = init.get("line_item_group")
    line_item: dict[str, Any] = _line_item if isinstance(_line_item, dict) else {}
    line_total, has_line_total = _upi_int_value(line_item.get("total"))
    if has_line_total:
        expected = line_total

    _auto_settings = line_item.get("automatic_surcharge_settings")
    auto_settings: dict[str, Any] = _auto_settings if isinstance(_auto_settings, dict) else {}
    if _upi_bool_value(auto_settings.get("enabled")):
        due, has_due = _upi_int_value(total_summary.get("due"))
        if has_due:
            expected = due

    _invoice = init.get("invoice")
    invoice: dict[str, Any] = _invoice if isinstance(_invoice, dict) else {}
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
    looks_like_trial = any(
        s in coupon_lower for s in ("free trial", "1 month free", "one month free", PLUS_TRIAL_CAMPAIGN_ID)
    )
    looks_like_full_discount = (
        signals["percent_off"] is not None and signals["percent_off"] >= 100
    ) or looks_like_trial
    return {
        "has_free_trial": due == 0
        or (looks_like_full_discount and signals["percent_off"] is not None and signals["percent_off"] >= 100),
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
UPI_STATIC_HOSTS = frozenset(
    {
        "stripe-camo.global.ssl.fastly.net",
        "files.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
        "q.stripe.com",
    }
)

#: 🔴 代码类静态资源后缀。**只用于**判断「这是不是一段可执行的页面脚本/样式」,
#: 绝不能拿去过滤 QR 候选 —— QR 图本身就是 ``.png`` / ``.svg``,
#: 一旦把图片后缀并进 QR 过滤，整个 QR 通道会被清空（曾经踩过）。
UPI_CODE_RESOURCE_SUFFIXES = (
    ".js",
    ".css",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".ico",
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


def _upi_custom_payment_method_id(custom_payment_methods: Any) -> str:
    """Return only a UPI-related ``cpmt_*`` id from a Checkout response.

    Aligned with the reference ``_upi_custom_payment_method_id``: never
    substitute a non-UPI custom method, and never invent an id. An empty
    result means the Checkout does not advertise the cpmt rail and the
    caller must fall through to the Stripe SetupIntent path.
    """
    if not isinstance(custom_payment_methods, list):
        return ""
    for item in custom_payment_methods:
        if isinstance(item, Mapping):
            cid = str(item.get("id") or "").strip()
            ctype = str(item.get("type") or "").strip()
        else:
            cid = str(item or "").strip()
            ctype = cid
        if cid.startswith("cpmt_") and ("upi" in ctype.lower() or "upi" in cid.lower()):
            return cid
    return ""


def _upi_copyable_link(payload: Any) -> str:
    """Prefer Stripe's hosted UPI instructions page, then the ``upi://`` deep link.

    Aligned with the reference ``_upi_copyable_link``: the hosted
    ``payments.stripe.com/upi/instructions/...`` page is the copyable link a
    browser can open, so it outranks a raw deep link when both exist. The
    reverse (deep-link first) only helps a scanner that consumes ``upi://``
    directly; a human operator needs the hosted page.
    """
    hosted = _upi_first_value_by_key(payload, "hosted_instructions_url")
    if _upi_is_instructions_url(str(hosted or "")):
        return str(hosted).strip()
    for url in _upi_collect_urls(payload):
        if _upi_is_instructions_url(url):
            return url
    deep = str(_upi_extract_next_action(payload).get("upi_uri") or "").strip()
    if not deep:
        deep = str(_upi_first_value_by_key(payload, "upi_deep_link") or "").strip()
    if deep.startswith("upi://"):
        return deep
    return ""


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
        elif value.startswith("https://payments.stripe.com/upi/instructions/") and not result.get(
            "hosted_instructions_url"
        ):
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



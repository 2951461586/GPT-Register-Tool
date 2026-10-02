"""India exit grading + checkout admission (Increment 1 + Increment 3 helper).

Split out of `pipeline.py` (2026-10-03).  The three ambient dependencies
(``resolve_proxy_geo`` / ``probe_openai_edge`` / ``rotate_session``) are injected
as :class:`ExitOps` instead of being imported, so the historical
``monkeypatch.setattr(pipeline, "resolve_proxy_geo", ...)`` seams still drive the
logic after the move.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

try:  # pragma: no cover - direct script execution
    from ..proxy_edge_probe import CHATGPT_CHECKOUT_PATH
    from .env import _env_bool, _env_int, _float_env
except ImportError:  # pragma: no cover
    from proxy_edge_probe import CHATGPT_CHECKOUT_PATH  # type: ignore
    from env import _env_bool, _env_int, _float_env  # type: ignore


@dataclass(frozen=True)
class ExitOps:
    """Ambient dependencies the exit selector probes through."""

    resolve_proxy_geo: Callable[..., Any]
    probe_openai_edge: Callable[..., Any]
    rotate_session: Callable[..., Any]


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


def _upi_exit_country(proxy: str, timeout: float, ops: ExitOps) -> str:
    try:
        geo = ops.resolve_proxy_geo(proxy, hint="IN", timeout=timeout)
    except Exception:
        return ""
    return str(getattr(geo, "country", "") or "").strip().upper()


def _upi_exit_admits_checkout(proxy: str, timeout: float, ops: ExitOps) -> bool:
    """True when a non-CF, sub-500 reply comes back from an admission path.

    A transport failure is *no evidence*, not a refusal: the reference lets a
    geo-unknown exit through rather than blocking on a failed lookup, and a
    single residential flap must not kill an otherwise usable exit.
    """
    saw_response = False
    for path in _UPI_ADMISSION_PATHS:
        verdict = ops.probe_openai_edge(proxy, path=path, timeout=timeout)
        code = int(getattr(verdict, "http_status", 0) or 0)
        if code <= 0:
            continue
        saw_response = True
        if not bool(getattr(verdict, "blocked_by_cloudflare", False)) and code < 500:
            return True
    return not saw_response


def _upi_rotate_region_session(proxy: str, country: str, ops: ExitOps) -> str:
    try:
        return str(ops.rotate_session(proxy, country) or proxy)
    except Exception:
        return proxy


def _upi_select_india_exit(
    proxy: Any,
    *,
    country: str,
    attempts: int,
    timeout: float,
    emit: Any = None,
    ops: ExitOps,
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
        observed = _upi_exit_country(candidate, timeout, ops)
        if observed and observed != expected:
            log("india_exit", f"[{index}/{total}] exit is {observed}, not {expected}; rotating session")
        elif _upi_exit_admits_checkout(candidate, timeout, ops):
            log("india_exit", f"[{index}/{total}] exit {observed or 'IN'} admits checkout")
            return candidate
        else:
            log("india_exit", f"[{index}/{total}] exit cannot enter checkout; rotating session")
        if index >= total:
            break
        rotated = _upi_rotate_region_session(candidate, expected, ops)
        if not rotated or rotated == candidate:
            break
        candidate = rotated
    return candidate


def _upi_rotate_proxy_set(values: tuple[Any, ...], country: str, ops: ExitOps) -> tuple[Any, ...]:
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
            mapping[text] = _upi_rotate_region_session(text, country, ops)
        out.append(mapping[text])
    return tuple(out)

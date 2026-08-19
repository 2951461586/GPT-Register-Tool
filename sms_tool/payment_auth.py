"""Just-in-time access-token gate for protocol payment workers."""

from __future__ import annotations

import time
from typing import Any

from .account_seed import extract_access_token, load_account_seed
from .token_telemetry import access_token_telemetry


def ensure_payment_access_token(
    *,
    email: str = "",
    session_file: str = "",
    proxy: Any = None,
    timeout: int = 30,
    relogin_on_401: bool = True,
    stabilization_probes: int = 1,
    stabilization_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    """Probe immediately before payment and run the recovery chain on HTTP 401.

    ``access_token`` is intentionally present for the in-process payment caller.
    Use :func:`public_payment_auth_result` before reporting or persistence.
    """
    from .account_liveness import probe_account_liveness
    from .account_recovery import (
        is_permanently_deactivated,
        relogin_codex_account,
    )

    data, json_path = load_account_seed(email=email, session_file=session_file)
    account_email = str(data.get("email") or email or "").strip().lower()
    if json_path:
        data["json_path"] = json_path
    if is_permanently_deactivated(data):
        return _auth_failure("account_deactivated", email=account_email, terminal=True)

    access_token = extract_access_token(data)
    if not access_token:
        return _auth_failure("missing_access_token", email=account_email)

    probe_count = max(1, min(int(stabilization_probes or 1), 3))
    delay = max(0.0, min(float(stabilization_delay_seconds or 0.0), 60.0))
    probes: list[dict[str, Any]] = []
    refreshed = False
    original_hash = access_token_telemetry(access_token).get("token_hash", "")

    for index in range(probe_count):
        probe = probe_account_liveness(data, proxy=proxy, timeout=max(5, int(timeout or 30)))
        probes.append(_public_probe(probe))
        status_code = _as_int(probe.get("status_code"))
        if status_code == 200:
            if index + 1 < probe_count and delay:
                time.sleep(delay)
            continue
        if status_code != 401 or not relogin_on_401 or refreshed:
            return _auth_failure(
                _probe_error_code(probe),
                email=account_email,
                probe=probe,
                probes=probes,
                token=access_token,
                refreshed=refreshed,
            )

        relogin = relogin_codex_account(
            data,
            proxy=proxy,
            timeout=max(int(timeout or 30), 30),
            mode="auto",
        )
        refreshed = True
        if not relogin.get("ok"):
            return _auth_failure(
                str(relogin.get("error") or "oauth_refresh_failed"),
                email=account_email,
                probe=probe,
                probes=probes,
                refreshed=True,
                terminal=bool(relogin.get("terminal")),
                relogin=relogin,
            )
        data, json_path = load_account_seed(email=account_email, session_file=json_path if not account_email else "")
        if json_path:
            data["json_path"] = json_path
        access_token = extract_access_token(data)
        if not access_token:
            return _auth_failure("oauth_refresh_missing_persisted_access_token", email=account_email, refreshed=True)

        candidate_probe = relogin.get("probe") if isinstance(relogin.get("probe"), dict) else {}
        probes.append(_public_probe(candidate_probe))
        if _as_int(candidate_probe.get("status_code")) != 200:
            return _auth_failure(
                _probe_error_code(candidate_probe), email=account_email, probe=candidate_probe,
                probes=probes, token=access_token, refreshed=True,
            )

    telemetry = access_token_telemetry(
        access_token,
        acquired_at=_as_int(data.get("access_token_updated_at") or data.get("refreshed_at")),
    )
    return {
        "ok": True,
        "email": account_email,
        "access_token": access_token,
        "auth_context": data,
        "probe": probes[-1] if probes else {},
        "probes": probes,
        "probed": len(probes),
        "refreshed": refreshed,
        "persisted": True,
        "token_changed": bool(refreshed and telemetry.get("token_hash") != original_hash),
        "token_telemetry": telemetry,
    }


def public_payment_auth_result(result: dict[str, Any]) -> dict[str, Any]:
    """Strip credentials and mailbox/session material from a JIT result."""
    blocked = {
        "access_token", "auth_context", "tokens", "id_token", "refresh_token",
        "oauth_refresh_token", "cookie_header", "mailbox", "password",
    }
    return {key: value for key, value in dict(result or {}).items() if key not in blocked}


def _auth_failure(
    error: str,
    *,
    email: str = "",
    probe: dict[str, Any] | None = None,
    probes: list[dict[str, Any]] | None = None,
    token: str = "",
    refreshed: bool = False,
    terminal: bool = False,
    relogin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "email": email,
        "error": str(error or "payment_auth_failed"),
        "probe": _public_probe(probe or {}),
        "probes": probes or [],
        "probed": len(probes or []),
        "refreshed": refreshed,
        "persisted": False,
        "terminal": terminal,
        "access_token": token,
        "token_telemetry": access_token_telemetry(token),
    }
    if relogin:
        result["relogin"] = public_payment_auth_result(relogin)
    return result


def _public_probe(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in dict(probe or {}).items()
        if key not in {"body", "access_token", "Authorization"}
    }


def _probe_error_code(probe: dict[str, Any]) -> str:
    status_code = _as_int(probe.get("status_code"))
    error = str(probe.get("error") or "").lower()
    if "deactivat" in error or "deleted" in error:
        return "account_deactivated"
    if status_code:
        return f"access_token_probe_http_{status_code}"
    return str(probe.get("error") or probe.get("status") or "access_token_probe_failed")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

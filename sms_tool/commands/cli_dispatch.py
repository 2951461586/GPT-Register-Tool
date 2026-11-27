"""CLI proxy-argument transport, early-exit commands, and registration exit helpers.

Extracted from :mod:`sms_tool.cli` so the root entrypoint stays focused on
command wiring, lazy dependency seams, and top-level dispatch.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable

#: Desktop -> backend proxy transport. The matching constants live in
#: ``SmsWorkbench.Contracts/ProxyArgumentTransport.cs``; both sides are pinned to
#: ``tests/fixtures/proxy_env_transport.json``.
_PROXY_ENV = "SMS_TOOL_PROXY"
_PROXY_POOL_ENV = "SMS_TOOL_PROXY_POOL"
_CHECKOUT_PROXY_POOL_ENV = "SMS_TOOL_CHECKOUT_PROXY_POOL"
_APPROVE_PROXY_POOL_ENV = "SMS_TOOL_APPROVE_PROXY_POOL"

_PROXY_TRANSPORT_ROUTES = (
    (_PROXY_ENV, "proxy"),
    (_PROXY_POOL_ENV, "proxy_pool"),
    (_CHECKOUT_PROXY_POOL_ENV, "checkout_proxy_pool"),
    (_APPROVE_PROXY_POOL_ENV, "approve_proxy_pool"),
)


def registration_proxy_lane(registration_driver: object, cfg: Any) -> str:
    from ..registration_drivers.base import normalize_registration_driver

    driver = normalize_registration_driver(registration_driver, cfg)
    return "protocol_registration" if driver == "protocol" else "browser_registration"


def configured_registration_proxy(cfg: Any, registration_driver: object = None, tracker=None) -> str:
    from ..proxy.routing import proxy_pool_for

    _raw_proxy_cfg: object = cfg.get("proxy")
    proxy_cfg: dict = _raw_proxy_cfg if isinstance(_raw_proxy_cfg, dict) else {}
    lane = registration_proxy_lane(registration_driver, cfg)
    lane_keys = (
        ("browser_pool", "browser_registration_pool")
        if lane == "browser_registration"
        else ("protocol_pool", "protocol_registration_pool")
    )
    if cfg.get("registration_proxy") and not any(proxy_cfg.get(key) for key in (*lane_keys, "registration", "pool")):
        return str(cfg["registration_proxy"]).strip()
    values = proxy_pool_for(cfg, lane)
    if values:
        from ..proxy.health import ProxyHealthTracker

        if len(values) > 1:
            tracker = tracker or ProxyHealthTracker(cfg)
            return tracker.rank(values)[0]
        return values[0]
    return str(proxy_cfg.get("default") or "").strip()


def apply_proxy_argument_transport(args) -> None:
    """Fill the proxy flags from the environment when the command line has none."""
    for env_name, attribute in _PROXY_TRANSPORT_ROUTES:
        value = str(os.environ.pop(env_name, "") or "").strip()
        if not value:
            continue
        if not hasattr(args, attribute):
            continue
        if str(getattr(args, attribute, "") or "").strip():
            continue
        setattr(args, attribute, value)


def normalize_proxy_args(args, cfg: Any) -> None:
    """Settle the proxy route: transport first, then explicitness, then default."""
    apply_proxy_argument_transport(args)
    args.proxy_explicit = bool(args.proxy)
    if not args.proxy:
        args.proxy = ((cfg.get("proxy") or {}).get("default") or "").strip() or None


def apply_registration_proxy_defaults(
    args,
    *,
    configured_proxy_fn: Callable[[object], str],
) -> None:
    if bool(getattr(args, "proxy_explicit", False)):
        return
    if str(getattr(args, "proxy_pool", "") or "").strip():
        args.proxy = None
        return
    args.proxy = configured_proxy_fn(getattr(args, "registration_driver", None)) or None


def proxy_pool_values(
    args,
    cfg: Any,
    *,
    configured_proxy_fn: Callable[[object], str],
) -> list[str]:
    from ..proxy.routing import proxy_pool_for

    raw = str(getattr(args, "proxy_pool", "") or "").strip()
    values = [item.strip() for item in re.split(r"[\r\n,;]+", raw) if item.strip()]
    primary = str(getattr(args, "proxy", "") or "").strip()
    if bool(getattr(args, "proxy_explicit", False)) and primary:
        values.insert(0, primary)
    if values:
        return list(dict.fromkeys(values))

    values.extend(proxy_pool_for(cfg, registration_proxy_lane(getattr(args, "registration_driver", None), cfg)))
    if not values:
        fallback = configured_proxy_fn(getattr(args, "registration_driver", None))
        if fallback:
            values.append(fallback)
    return list(dict.fromkeys(values))


def init_config_command(
    *,
    safe_print_fn: Callable[..., Any],
    initialize_config_shards_fn: Callable[[], Any],
    default_config_dir_fn: Callable[[], Any],
    config_error_cls: type[BaseException],
) -> int:
    """Bootstrap the config shards, then exit."""
    try:
        results = initialize_config_shards_fn()
    except config_error_cls as error:
        safe_print_fn(f"--init-config: {error}", file=sys.stderr)
        return 2
    config_dir = default_config_dir_fn()
    for filename, status in results:
        safe_print_fn(f"{status:8s} {config_dir / filename}")
    if all(status == "skipped" for _, status in results):
        safe_print_fn("nothing to do: every config shard already exists")
    return 0


def main_early_commands(
    args,
    *,
    cfg: Any,
    emit_result_fn: Callable[..., Any],
    import_account_json_fn: Callable[[Any], Any],
    inspect_accounts_fn: Callable[[Any], Any],
    safe_print_fn: Callable[..., Any],
    run_doctor_fn: Callable[[Any, str], dict[str, Any]],
    print_doctor_report_fn: Callable[[dict[str, Any]], Any],
    default_config_dir_fn: Callable[[], Any],
) -> bool:
    """Early-exit commands that run before the registration pipeline."""
    if getattr(args, "desktop_serve", False):
        from ..desktop.serve import serve_forever

        raise SystemExit(serve_forever())
    if getattr(args, "proxy_list", False) or getattr(args, "proxy_test", False) or getattr(args, "proxy_screen", False):
        from .proxy_admin import run_proxy_command

        run_proxy_command(args)
        raise SystemExit(0)
    if getattr(args, "doctor", False):
        report = run_doctor_fn(cfg, str(default_config_dir_fn()))
        if getattr(args, "json_output", False):
            safe_print_fn(json.dumps(report, ensure_ascii=False, indent=2))
        elif getattr(args, "desktop_ipc", False):
            emit_result_fn(dict(report), enabled=True)
        else:
            print_doctor_report_fn(report)
        raise SystemExit(0 if getattr(args, "desktop_ipc", False) else report["failed"])
    if args.desktop_read:
        from ..desktop.read import (
            create_account_file,
            create_mailbox_file,
            create_payment_url_file,
            read_account,
            read_accounts,
            read_mailbox_pool,
            read_proxy_pool,
        )

        if args.desktop_read == "accounts":
            payload = {"ok": True, "accounts": read_accounts(cfg, include_session=False)}
        elif args.desktop_read == "account":
            payload = {"ok": True, "account": read_account(args.account_id or "", args.email or "", cfg)}
        elif args.desktop_read == "mailbox-pool":
            extra_files = (args.chatai_mailbox_file,) if args.chatai_mailbox_file else ()
            payload = {"ok": True, **read_mailbox_pool(cfg, extra_files=extra_files)}
        elif args.desktop_read == "proxy-pool":
            payload = read_proxy_pool(
                cfg,
                lane=str(getattr(args, "proxy_read_lane", "") or "protocol_registration"),
            )
        elif args.desktop_read == "mailbox-file":
            payload = create_mailbox_file(args.account_id or "", args.email or "", cfg)
        elif args.desktop_read == "account-file":
            payload = create_account_file(args.account_id or "", args.email or "", cfg)
        else:
            payload = create_payment_url_file(args.account_id or "", args.email or "", cfg)
        emit_result_fn(payload, enabled=True)
        return True
    if getattr(args, "import_account_json", None):
        import_account_json_fn(args)
        return True
    if getattr(args, "inspect_accounts", False):
        inspect_accounts_fn(args)
        return True
    return False


def run_email_registration_batch(
    args,
    mailboxes,
    *,
    effective_count: int,
    target_success: int,
    phone_pool,
    base_dir,
    pipeline_started: float,
    mailbox_seconds: float,
    register_started: float,
    proxy_pool_values_fn: Callable[[Any], list[str]],
    registration_commands_mod: Any,
    registration_context_fn: Callable[[], Any],
    run_batch_fn: Callable[..., Any],
    run_email_fn: Any,
):
    proxy_pool = proxy_pool_values_fn(args)
    if effective_count > 1:

        def persist_completed_result(_index, result):
            registration_commands_mod.persist_registration_result(
                args,
                result,
                base_dir,
                registration_context_fn(),
                pipeline_timing=registration_commands_mod.registration_pipeline_timing(
                    pipeline_started,
                    mailbox_seconds,
                    register_started,
                ),
            )

        batch_kwargs = {
            "count": effective_count,
            "proxy": args.proxy,
            "proxy_pool": proxy_pool,
            "mailboxes": mailboxes,
            "workers": args.workers,
            "phone_pool": phone_pool,
            "codex_oauth": False,
            "registration_mode": args.registration_mode,
            "registration_driver": getattr(args, "registration_driver", None),
            "browser_headless": getattr(args, "browser_headless", None),
            "enroll_2fa": not getattr(args, "no_2fa", False),
            "run_email_func": run_email_fn,
            "on_result": persist_completed_result,
        }
        if target_success > 0:
            batch_kwargs["target_success"] = target_success
        return run_batch_fn(**batch_kwargs)

    mailbox = mailboxes[0] if mailboxes else None
    return run_batch_fn(
        count=1,
        proxy=args.proxy,
        proxy_pool=proxy_pool,
        mailboxes=[mailbox] if mailbox else [],
        workers=1,
        phone_pool=phone_pool,
        codex_oauth=False,
        registration_mode=args.registration_mode,
        registration_driver=getattr(args, "registration_driver", None),
        browser_headless=getattr(args, "browser_headless", None),
        enroll_2fa=not getattr(args, "no_2fa", False),
        run_email_func=run_email_fn,
    )


def resolve_target_success_plan(args, mailboxes, effective_count: int) -> tuple[int, int]:
    """Expand attempt pool to loaded mailboxes when ``--target-success`` is requested."""
    target = max(0, int(getattr(args, "target_success", 0) or 0))
    if target <= 0:
        return effective_count, 0
    if mailboxes and effective_count < len(mailboxes) and int(getattr(args, "count", 1) or 1) <= target:
        effective_count = len(mailboxes)
    return effective_count, target


def registration_exit_code(report, *, target_success: int = 0) -> int:
    """Map a finished registration batch report onto a process exit code."""
    _raw_data: object = report
    data: dict = _raw_data if isinstance(_raw_data, dict) else {}
    if target_success > 0:
        return 0 if data.get("ok") is True else 3
    if data.get("ok") is False:
        return 3
    try:
        failed = int(data.get("failed") or 0)
    except (TypeError, ValueError):
        return 0
    return 3 if failed > 0 else 0


def emit_registration_error(
    args,
    error: str,
    *,
    exit_code: int,
    emit_result_fn: Callable[..., Any],
    safe_print_fn: Callable[..., Any],
    sanitize_text_fn: Callable[[Any], str],
) -> dict:
    """Emit one standard terminal registration failure at the CLI boundary."""
    safe_error = sanitize_text_fn(error)[:500]
    payload = {
        "ok": False,
        "total": 0,
        "success": 0,
        "failed": 1,
        "batch_id": str(getattr(args, "registration_batch_id", "") or ""),
        "error": safe_error,
        "exit_code": int(exit_code),
    }
    if bool(getattr(args, "desktop_ipc", False)):
        emit_result_fn(payload, enabled=True)
    else:
        safe_print_fn(f"[Error] {safe_error}")
    return payload

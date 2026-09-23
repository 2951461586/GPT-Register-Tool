import argparse
import json
import os
import re
import sys
import time

from .config import CFG, initialize_runtime_config
from .diagnostics import install_safe_stdio, safe_print
from .desktop_ipc import emit_result
from .paths import output_dir, runtime_file
from .registration_drivers.base import driver_choices
from .batch_runner import filter_registered_mailboxes, run_batch_impl as run_batch
from .storage import database_path, get_paypal_url, list_paypal_accounts, rebuild_from_session_dir, upsert_account
from .commands.helpers import (
    read_email_file as _read_email_file,
    payment_method as _payment_method,
    mailbox_from_explicit_args as _mailbox_from_explicit_args,
    one_click_sms_max_reuse as _one_click_sms_max_reuse,
)
from .commands import accounts as account_commands
from .commands import mailbox_ops as mailbox_commands
from .commands import one_click as one_click_commands
from .commands import omakse as omakse_commands
from .commands import payment as payment_commands
from .commands import payment_links as payment_link_commands
from .commands import registration as registration_commands
from .commands.email_change import run_change_email
from .proxy_routing import proxy_pool_for
from .proxy_health import ProxyHealthTracker
from .sanitizer import sanitize_text

# `mailbox` and `registration` import `curl_cffi` at module top, so they must NOT
# be imported eagerly at the top of this file — otherwise `import sms_tool.cli`
# (and therefore the `--doctor` / `--desktop-read` lightweight commands) would
# crash whenever curl_cffi is missing. They are loaded on demand, only on the
# registration / one-click paths that actually need them.
_heavy_deps_loaded = False

# Module-level placeholders so these names always exist as attributes. This keeps
# `import sms_tool.cli` free of curl_cffi (the real values are imported lazily by
# _load_heavy_deps), while still letting tests `patch.object(cli, "...")` and direct
# handler calls reference them without a NameError/AttributeError.
_load_mailbox_pool = None
_remail_enabled = None
_build_session_file = None
_mailbox_snapshot = None
run_email = None


def _load_heavy_deps() -> None:
    global _heavy_deps_loaded
    global _load_mailbox_pool, _remail_enabled, _build_session_file, _mailbox_snapshot, run_email
    if _heavy_deps_loaded:
        return
    if _load_mailbox_pool is None or _remail_enabled is None:
        from .mailbox import (
            _load_mailbox_pool as _lmp,
            _remail_enabled as _re,
        )
        if _load_mailbox_pool is None:
            _load_mailbox_pool = _lmp
        if _remail_enabled is None:
            _remail_enabled = _re
    if _build_session_file is None or _mailbox_snapshot is None or run_email is None:
        from .registration import (
            _build_session_file as _bsf,
            _mailbox_snapshot as _ms,
            run_email as _re2,
        )
        if _build_session_file is None:
            _build_session_file = _bsf
        if _mailbox_snapshot is None:
            _mailbox_snapshot = _ms
        if run_email is None:
            run_email = _re2
    _heavy_deps_loaded = True


def _registration_proxy_lane(registration_driver: object = None) -> str:
    from .registration_drivers.base import normalize_registration_driver
    driver = normalize_registration_driver(registration_driver, CFG)
    return "protocol_registration" if driver == "protocol" else "browser_registration"


def _configured_registration_proxy(registration_driver: object = None, tracker=None) -> str:
    proxy_cfg = CFG.get("proxy") if isinstance(CFG.get("proxy"), dict) else {}
    lane = _registration_proxy_lane(registration_driver)
    lane_keys = (
        ("browser_pool", "browser_registration_pool")
        if lane == "browser_registration"
        else ("protocol_pool", "protocol_registration_pool")
    )
    if CFG.get("registration_proxy") and not any(proxy_cfg.get(key) for key in (*lane_keys, "registration", "pool")):
        return str(CFG["registration_proxy"]).strip()
    values = proxy_pool_for(CFG, lane)
    if values:
        # P2-1: when more than one candidate exists, pick the healthiest via the
        # shared ProxyHealthTracker instead of always pinning the first. On a fresh
        # tracker (no health data) rank() preserves input order, so behaviour is
        # unchanged for a single-account run that has never recorded failures.
        if len(values) > 1:
            tracker = tracker or ProxyHealthTracker(CFG)
            return tracker.rank(values)[0]
        return values[0]
    return str(
        proxy_cfg.get("default")
        or ""
    ).strip()


def _apply_registration_proxy_defaults(args) -> None:
    if bool(getattr(args, "proxy_explicit", False)):
        return
    if str(getattr(args, "proxy_pool", "") or "").strip():
        args.proxy = None
        return
    args.proxy = _configured_registration_proxy(getattr(args, "registration_driver", None)) or None


def _proxy_pool_values(args) -> list[str]:
    raw = str(getattr(args, "proxy_pool", "") or "").strip()
    values = [item.strip() for item in re.split(r"[\r\n,;]+", raw) if item.strip()]
    primary = str(getattr(args, "proxy", "") or "").strip()
    if bool(getattr(args, "proxy_explicit", False)) and primary:
        values.insert(0, primary)
    if values:
        return list(dict.fromkeys(values))

    values.extend(proxy_pool_for(CFG, _registration_proxy_lane(getattr(args, "registration_driver", None))))
    if not values:
        fallback = _configured_registration_proxy(getattr(args, "registration_driver", None))
        if fallback:
            values.append(fallback)
    return list(dict.fromkeys(values))


def _registration_command_context():
    _load_heavy_deps()
    return registration_commands.RegistrationCommandContext(
        proxy_pool_values=_proxy_pool_values,
        load_mailbox_pool=_load_mailbox_pool,
        run_batch=run_batch,
        run_email=run_email,
        build_session_file=_build_session_file,
        save_results=_save_registration_results,
        check_registered_promotions=_check_registered_promotions,
        import_registered_accounts=_import_registered_accounts,
        registration_phone_pool=_registration_phone_pool,
        upsert_account=upsert_account,
        database_path=database_path,
        runtime_file=lambda name: runtime_file(CFG, name),
        runtime_config=CFG,
    )


def _preflight_registration_before_mailbox(args) -> dict:
    return registration_commands.preflight_registration_before_mailbox(args, _registration_command_context())


def _protocol_proxy_pool() -> list[str]:
    return payment_commands.protocol_proxy_pool(CFG)


def _payment_proxy_pools(payment_method: str) -> dict[str, list[str]]:
    return payment_commands.payment_proxy_pools(CFG, payment_method)


def _has_explicit_payment_proxy(args) -> bool:
    return payment_commands.has_explicit_payment_proxy(args)


def _registration_phone_pool(args):
    return registration_commands.registration_phone_pool(args)


def _payment_country(payment_method: str, explicit: str = "") -> str:
    return payment_commands.payment_country(payment_method, explicit)


def _payment_method_choices() -> tuple[str, ...]:
    from .payment_catalog import PAYMENT_CATALOG

    return tuple(PAYMENT_CATALOG.aliases)


def _at_payment_stage_args(args, payment_method="paypal"):
    return payment_commands.payment_stage_args(
        args,
        payment_method,
        CFG,
        apply_country_overrides=_apply_stage_country_overrides,
    )


def _apply_stage_country_overrides(args, proxy, checkout_proxy, provider_proxy, approve_proxy):
    return payment_commands.apply_stage_country_overrides(
        args,
        proxy,
        checkout_proxy,
        provider_proxy,
        approve_proxy,
    )


def _at_promotion_proxy_arg(args, payment_method="paypal"):
    return payment_commands.promotion_proxy_arg(args, payment_method, CFG)


def build_parser():
    """Build the command-line argument parser.

    Extracted from ``main`` so the argument set (including the
    ``--registration-driver`` choices) is unit-testable without triggering the
    runtime/config initialization that ``main`` performs.
    """

    parser = argparse.ArgumentParser(description="ChatGPT Email Registration + PayPal link generation")

    from .cli_parsers import (
        codex,
        core,
        email_change,
        omakse,
        one_click,
        payment,
        paypal,
        quota,
        session,
        sub2api,
    )

    # Registration order is the original cli.py order, so --help output and
    # any ordering-sensitive parsing behaviour are unchanged.
    core.register(parser)
    payment.register(parser)
    omakse.register(parser)
    session.register(parser)
    paypal.register(parser)
    sub2api.register(parser)
    quota.register(parser)
    codex.register(parser)
    one_click.register(parser)
    email_change.register(parser)

    return parser


def main():
    initialize_runtime_config()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    install_safe_stdio()
    # Mirror stdout/stderr to `runtime/logs/backend_stdout.log`. Most of the
    # protocol lane reports through print(), which only ever reached the host's
    # IPC pipe -- so a run that failed before its first progress record left no
    # evidence on disk at all. The mirror is passive: it copies complete lines
    # without rerouting or decorating the stream the host parses.
    from .stdout_mirror import install_stdout_mirror

    install_stdout_mirror()
    # Wire the rotating file logger (`runtime/logs/sms_tool.log`). Without this
    # the module is dead code and `logger.*` calls vanish: the codebase reports
    # through print(), so nothing is ever persisted and no exception carries a
    # traceback. `to_console=False` is deliberate -- stdout is the WPF host's
    # IPC channel (the `@@SMSWORKBENCH_V2@@` envelope plus the "Saved session:"
    # marker), and a StreamHandler would inject formatter-decorated lines into
    # that contract.
    from .logging_setup import configure_logging

    configure_logging(to_console=False)

    parser = build_parser()
    args = parser.parse_args()
    if args.register_and_import:
        args.import_cpa = True
    # Keep whether --proxy came from the operator.  Some commands (notably
    # --generate-ba-link) need an omitted single proxy to mean "use the
    # configured stage proxies", even though the rest of the CLI still wants
    # CFG.proxy.default as its normal default proxy.
    args.proxy_explicit = bool(args.proxy)
    if not args.proxy:
        args.proxy = ((CFG.get("proxy") or {}).get("default") or "").strip() or None

    base_dir = args.output_dir or str(output_dir(CFG))
    if _main_early_commands(args):
        return
    if args.delete_account:
        from .accounts.account_lifecycle import AccountDeleteRequest, AccountLifecycle
        from .commands.helpers import read_email_file, unique_emails
        emails = read_email_file(args.email_file)
        if args.email:
            emails.insert(0, args.email)
        emails = unique_emails(emails)
        if not emails:
            raise SystemExit("--delete-account requires --email or --email-file")
        lifecycle = AccountLifecycle(CFG)
        results = lifecycle.delete_many(
            (AccountDeleteRequest(email) for email in emails),
            workers=max(1, int(args.workers or 1)),
        )
        failures = sum(isinstance(result, Exception) for result in results)
        payload = {
            "ok": failures == 0,
            "total": len(emails),
            "deleted": len(emails) - failures,
            "failed": failures,
            "results": [
                ({"ok": False, "email": email, "error": str(result)}
                 if isinstance(result, Exception)
                 else {"ok": True, **result.to_dict()})
                for email, result in zip(emails, results)
            ],
        }
        emit_result(payload, enabled=bool(args.desktop_ipc))
        if failures:
            raise SystemExit(3)
        return
    if args.change_email:
        from .accounts.account_email_change import EmailChangeRequest, change_email_batch, load_change_email_accounts
        run_change_email(
            args,
            load_accounts=load_change_email_accounts,
            change_email_batch=change_email_batch,
            request_type=EmailChangeRequest,
            emit_result=emit_result,
        )
        return
    if args.rebuild_sqlite:
        count = rebuild_from_session_dir(base_dir)
        print(f"[*] SQLite rebuilt: {database_path()} ({count} account record(s))")
        return
    if args.list_paypal_links:
        _print_paypal_links(args.email)
        return
    if args.open_paypal_link:
        _open_paypal_link(args.email)
        return
    if args.list_paypal_ba_queue:
        _list_paypal_ba_queue(args)
        return
    if args.process_paypal_ba_queue:
        _process_paypal_ba_queue(args)
        return
    if args.import_cpa and not args.register_and_import:
        _import_cpa(args)
        return
    if args.refresh_cpa_quota or args.refresh_local_quota:
        _refresh_cpa_quota(args)
        return
    if getattr(args, "quota_usage", False):
        _quota_usage(args)
        return
    if getattr(args, "check_promotion", False):
        _check_promotion(args)
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if args.list_payment_methods:
        _list_payment_methods()
        return
    if args.test_payment_proxies:
        _test_payment_proxies(args)
        return
    if args.extract_payment_link:
        _extract_payment_link(args)
        return
    if args.generate_ba_link:
        _generate_ba_link(args)
        return
    if args.generate_upi_qr:
        _generate_upi_qr(args)
        return
    if args.omakse_extract:
        _omakse_extract(args)
        return
    if args.omakse_us_pay:
        _omakse_us_pay(args)
        return
    if args.refresh_session:
        _refresh_session(args)
        return
    if args.view_inbox:
        _view_inbox(args)
        return
    if args.gmail_send:
        _gmail_send(args)
        return
    if args.auto_pay or args.auto_pay_reverse_only:
        _auto_pay(args)
        return
    if args.batch_auto_pay:
        _batch_auto_pay(args)
        return

    _load_heavy_deps()
    _apply_registration_proxy_defaults(args)

    if args.one_click_sms:
        _one_click_sms(args)
        return
    if args.one_click_scan:
        _one_click_scan(args)
        return
    
    if args.convert_session_json:
        _convert_session_json(args)
        return

    try:
        _preflight_registration_before_mailbox(args)
    except Exception as exc:
        _emit_registration_error(args, str(exc), exit_code=2)
        raise SystemExit(2) from None

    _main_registration_pipeline(args, base_dir)


def _main_early_commands(args) -> bool:
    """Early-exit commands that run before the registration pipeline.

    Returns True when the command was handled (desktop serve/doctor/read).
    Extracted from ``main`` so the pipeline bootstrap, the early exits, and
    the dispatch chain are three visible segments instead of one 402-line
    body.
    """
    if getattr(args, "desktop_serve", False):
        from .desktop_serve import serve_forever

        raise SystemExit(serve_forever())
    if getattr(args, "doctor", False):
        from .config import default_config_dir
        from .doctor import print_doctor_report, run_doctor

        # The canonical config is the proxy/runtime/payment shards under the
        # project root, so report that directory as the source. This used to be
        # `default_config_path().parent`, which silently became `sms_tool/` once
        # the legacy project-root config.json was archived -- i.e. the report
        # named the directory holding the *bundled fallback* as the live config
        # source. Nothing flagged it: `_probe_config` compares against the
        # bundled *file* path, so a directory never matches and the status stays
        # `ok`. The operator just read the wrong path.
        report = run_doctor(CFG, str(default_config_dir()))
        if getattr(args, "json_output", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif getattr(args, "desktop_ipc", False):
            emit_result(dict(report), enabled=True)
        else:
            print_doctor_report(report)
        raise SystemExit(0 if getattr(args, "desktop_ipc", False) else report["failed"])
    if args.desktop_read:
        from .desktop_read import (
            create_account_file,
            create_mailbox_file,
            create_payment_url_file,
            read_account,
            read_accounts,
            read_mailbox_pool,
        )
        if args.desktop_read == "accounts":
            payload = {"ok": True, "accounts": read_accounts(CFG, include_session=False)}
        elif args.desktop_read == "account":
            payload = {"ok": True, "account": read_account(args.account_id or "", args.email or "", CFG)}
        elif args.desktop_read == "mailbox-pool":
            extra_files = (args.chatai_mailbox_file,) if args.chatai_mailbox_file else ()
            payload = {"ok": True, **read_mailbox_pool(CFG, extra_files=extra_files)}
        elif args.desktop_read == "mailbox-file":
            payload = create_mailbox_file(args.account_id or "", args.email or "", CFG)
        elif args.desktop_read == "account-file":
            payload = create_account_file(args.account_id or "", args.email or "", CFG)
        else:
            payload = create_payment_url_file(args.account_id or "", args.email or "", CFG)
        emit_result(payload, enabled=True)
        return
    return False


def _main_registration_pipeline(args, base_dir) -> None:
    """The registration pipeline: load mailboxes, preflight, register, report.

    Extracted from ``main`` unchanged except for the two parameters --
    ``args`` and ``base_dir`` were the only locals it closed over.
    """
    if getattr(args, "target_at200", 0):
        _run_target_at200(args, base_dir)
        return

    pipeline_started = time.time()
    mailbox_started = time.time()
    mailboxes = _load_mailbox_pool(args)
    mailbox_seconds = time.time() - mailbox_started
    # Drop already-registered mailboxes before anything counts or bills them.
    # ``run_batch_impl`` filters as well, but by then they are already inside
    # ``effective_count`` -- and, on the replenishment path, inside
    # ``purchased``/``spent``, which turns skipped mailboxes into reported
    # failures and lets the loop spin over a pool that yields nothing.
    loaded_mailboxes = mailboxes
    mailboxes = filter_registered_mailboxes(mailboxes)
    if loaded_mailboxes and not mailboxes:
        _emit_registration_error(
            args,
            "every loaded mailbox already has a registered account; add fresh mailboxes, "
            "or set registration.skip_registered_mailboxes=false to attempt them anyway",
            exit_code=2,
        )
        raise SystemExit(2)
    explicit_mailbox_source = bool(
        args.chatai_mailbox_file
        or args.mailbox_file
        or args.email
        or args.email_refresh_token
        or args.email_access_token
        or args.remail_token
        or args.buy_remail_mailbox
        or args.remail_service_mode
        or args.buy_cfworker_mailbox
        or args.buy_smailr_mailbox
    )
    if not mailboxes and explicit_mailbox_source:
        _emit_registration_error(
            args,
            "no mailbox account was found from the requested source; "
            "check the selected mailbox row or mailbox file format",
            exit_code=2,
        )
        raise SystemExit(2)
    if not mailboxes and not _remail_enabled():
        _emit_registration_error(
            args,
            "no mailbox account was found; set email_registration.token_file, "
            "pass --email/--email-refresh-token, or configure ReMail",
            exit_code=2,
        )
        raise SystemExit(2)
    requested_count = max(1, int(args.count or 1))
    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"registration_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    effective_count = requested_count
    if getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), ReMail returned {effective_count}; registering returned mailboxes only.")
    elif getattr(args, "buy_cfworker_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), CFWorker returned {effective_count}; registering returned mailboxes only.")
    elif getattr(args, "buy_smailr_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), Smailr returned {effective_count}; registering returned mailboxes only.")
    elif mailboxes and requested_count > len(mailboxes):
        effective_count = len(mailboxes)
        print(f"[!] Requested {requested_count} account(s), but only {effective_count} mailbox(es) were loaded; registering loaded mailboxes only.")

    # Phone reuse pool (auto-enable when smsbower or paypal_auto phone is configured)
    phone_pool = _registration_phone_pool(args)

    # Phone registration mode (via SMSBower)
    if getattr(args, "phone_register", False):
        from .registration import run_phone_register
        proxy_pool = _proxy_pool_values(args)
        if len(proxy_pool) > 1:
            from .batch_runner import select_registration_proxy_base
            selected_proxy = select_registration_proxy_base(proxy_pool, args.proxy)
            proxy_pool = [selected_proxy] if selected_proxy else []
        registration_proxy = proxy_pool[0] if proxy_pool else args.proxy
        results = []
        register_started = time.time()
        for i in range(effective_count):
            print(f"\n{'='*60}")
            print(f"[*] Phone registration {i+1}/{effective_count}")
            print(f"{'='*60}")
            result = run_phone_register(
                proxy=registration_proxy,
                password=args.password,
                codex_oauth=False,
                smsbower_country=args.smsbower_country,
            )
            results.append(result)
            registration_commands.persist_registration_result(
                args,
                result,
                base_dir,
                _registration_command_context(),
                pipeline_timing=registration_commands.registration_pipeline_timing(
                    pipeline_started,
                    mailbox_seconds,
                    register_started,
                ),
            )
            if result.get("success"):
                print(f"[OK] Phone registered: {result.get('phone', '')} | AT: [REDACTED]")
            else:
                print(f"[FAIL] {result.get('error', 'unknown')}")
        report = _save_registration_results(
            args, results, effective_count=effective_count, base_dir=base_dir,
            pipeline_started=pipeline_started, mailbox_seconds=0,
            register_seconds=time.time() - register_started,
        )
        if bool(getattr(args, "desktop_ipc", False)):
            emit_result(report, enabled=True)
        exit_code = _registration_exit_code(report)
        if exit_code:
            raise SystemExit(exit_code)
        return

    register_started = time.time()
    if effective_count > 1:
        proxy_pool = _proxy_pool_values(args)
        def persist_completed_result(_index, result):
            registration_commands.persist_registration_result(
                args,
                result,
                base_dir,
                _registration_command_context(),
                pipeline_timing=registration_commands.registration_pipeline_timing(
                    pipeline_started,
                    mailbox_seconds,
                    register_started,
                ),
            )

        results = run_batch(
            count=effective_count,
            proxy=args.proxy,
            proxy_pool=proxy_pool,
            mailboxes=mailboxes,
            workers=args.workers,
            phone_pool=phone_pool,
            codex_oauth=False,
            registration_mode=args.registration_mode,
            registration_driver=getattr(args, "registration_driver", None),
            browser_headless=getattr(args, "browser_headless", None),
            enroll_2fa=not getattr(args, "no_2fa", False),
            run_email_func=run_email,
            on_result=persist_completed_result,
        )
    else:
        mailbox = mailboxes[0] if mailboxes else None
        proxy_pool = _proxy_pool_values(args)
        results = run_batch(
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
            run_email_func=run_email,
        )
    register_seconds = time.time() - register_started

    report = _save_registration_results(
        args,
        results,
        effective_count=effective_count,
        base_dir=base_dir,
        pipeline_started=pipeline_started,
        mailbox_seconds=mailbox_seconds,
        register_seconds=register_seconds,
    )
    if bool(getattr(args, "desktop_ipc", False)):
        emit_result(report, enabled=True)
    # `save_registration_results` has always reported `failed`, but this path
    # used to fall off the end of `main()` -- so a batch that registered 0 of 3
    # accounts still exited 0, and any script or CI job watching the exit status
    # read it as success.  Code 3 is the documented runtime/provider failure slot
    # (docs/architecture.md) and is what `--target-at200` and `--delete-account`
    # already raise for a batch that did not fully succeed.
    exit_code = _registration_exit_code(report)
    if exit_code:
        raise SystemExit(exit_code)


def _registration_exit_code(report) -> int:
    """Map a finished registration batch report onto a process exit code.

    Returns 3 when the batch did not fully succeed, 0 otherwise.  The failure
    is read from the report itself rather than recomputed, so the exit code can
    never disagree with the ``N/M registered successfully`` line printed next to
    it.

    The two fields are a union, not a vote: either an explicit ``ok: False`` or
    a non-zero ``failed`` marks the batch failed.  A real report sets both
    consistently (``ok`` *is* ``success == total``), so the union only decides
    the outcome for a truncated or hand-built report.  A report that carries
    neither field is left at 0 -- a missing verdict is not evidence of failure,
    and guessing would fail batches that actually succeeded.
    """
    data = report if isinstance(report, dict) else {}
    if data.get("ok") is False:
        return 3
    try:
        failed = int(data.get("failed") or 0)
    except (TypeError, ValueError):
        return 0
    return 3 if failed > 0 else 0


def _save_registration_results(
    args,
    results,
    effective_count,
    base_dir,
    pipeline_started,
    mailbox_seconds,
    register_seconds,
):
    return registration_commands.save_registration_results(
        args,
        results,
        effective_count,
        base_dir,
        pipeline_started,
        mailbox_seconds,
        register_seconds,
        _registration_command_context(),
    )


def _emit_registration_error(args, error: str, *, exit_code: int) -> dict:
    """Emit one standard terminal registration failure at the CLI boundary."""
    safe_error = sanitize_text(error)[:500]
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
        emit_result(payload, enabled=True)
    else:
        safe_print(f"[Error] {safe_error}")
    return payload


def _check_registered_promotions(emails, workers=4, proxy=None, timeout=20, proxy_pool=None, payment_eligibility=True):
    return registration_commands.check_registered_promotions(
        emails,
        workers=workers,
        proxy=proxy,
        timeout=timeout,
        proxy_pool=proxy_pool,
        payment_eligibility=payment_eligibility,
    )


def _run_target_at200(args, base_dir):
    return registration_commands.run_target_at200(args, base_dir, _registration_command_context())


def _account_command_context():
    return account_commands.AccountCommandContext(
        list_paypal_accounts=list_paypal_accounts,
        get_paypal_url=get_paypal_url,
    )


def _import_registered_accounts(args, emails):
    return account_commands.import_registered_accounts(args, emails)


def _print_paypal_links(email=""):
    return account_commands.print_paypal_links(email, _account_command_context())


def _open_paypal_link(email):
    return account_commands.open_paypal_link(email, _account_command_context())


def _refresh_session(args):
    return account_commands.refresh_session(args)


def _mailbox_command_context():
    return mailbox_commands.MailboxCommandContext(upsert_account=upsert_account)


def _view_inbox(args):
    return mailbox_commands.view_inbox(args, _mailbox_command_context())


def _gmail_send(args):
    return mailbox_commands.gmail_send(args)


def _export_codex_json(args):
    return account_commands.export_codex_json(args, _account_command_context())



def _import_cpa(args):
    return account_commands.import_cpa(args, _account_command_context())


def _check_promotion(args):
    return account_commands.check_promotion(args, _account_command_context())


def _refresh_cpa_quota(args):
    return account_commands.refresh_cpa_quota(args, _account_command_context())


def _quota_usage(args):
    return account_commands.quota_usage(args)


def _payment_link_command_context():
    return payment_link_commands.PaymentLinkCommandContext(
        payment_stage_args=_at_payment_stage_args,
        promotion_proxy_arg=_at_promotion_proxy_arg,
        stage_country_overrides=_payment_stage_country_overrides,
        runtime_config=CFG,
    )


def _generate_ba_link(args):
    return payment_link_commands.generate_ba_link(args, _payment_link_command_context())


def _generate_upi_qr(args):
    return payment_link_commands.generate_upi_qr(args, _payment_link_command_context())


def _payment_command_context():
    return payment_commands.PaymentCommandContext(
        read_email_file=_read_email_file,
        payment_method=_payment_method,
        resolve_access_token=_resolve_payment_access_token,
        payment_stage_args=_at_payment_stage_args,
        promotion_proxy_arg=_at_promotion_proxy_arg,
        stage_country_overrides=_payment_stage_country_overrides,
        payment_country=_payment_country,
        protocol_proxy_pool=_protocol_proxy_pool,
        has_explicit_payment_proxy=_has_explicit_payment_proxy,
        payment_proxy_pools=_payment_proxy_pools,
        runtime_config=CFG,
    )


def _list_payment_methods():
    return payment_commands.list_payment_methods()


def _payment_stage_country_overrides(args, payment_method="paypal"):
    return payment_commands.stage_country_overrides(args, payment_method, CFG)


def _resolve_payment_access_token(args):
    return payment_commands.resolve_access_token(args, stderr=sys.stderr)


def _test_payment_proxies(args):
    return payment_commands.test_payment_proxies(args, _payment_command_context())


def _extract_payment_link(args):
    return payment_commands.extract_payment_link(args, _payment_command_context())



def _convert_session_json(args):
    return account_commands.convert_session_json(args)


def _auto_pay(args):
    return payment_link_commands.auto_pay(args)


def _batch_auto_pay(args):
    return payment_link_commands.batch_auto_pay(args)


def _list_paypal_ba_queue(args):
    return payment_link_commands.list_paypal_ba_queue(args)


def _process_paypal_ba_queue(args):
    return payment_link_commands.process_paypal_ba_queue(args)


def _one_click_command_context():
    _load_heavy_deps()
    return one_click_commands.OneClickCommandContext(
        load_mailbox_pool=_load_mailbox_pool,
        max_reuse=_one_click_sms_max_reuse,
        mailbox_snapshot=_mailbox_snapshot,
        persist_failure=_persist_one_click_sms_failure,
        upsert_account=upsert_account,
    )


def _one_click_sms(args):
    _load_heavy_deps()
    return one_click_commands.one_click_sms(args, _one_click_command_context())


def _one_click_scan(args):
    return one_click_commands.one_click_scan(args)


def _persist_one_click_sms_failure(data, json_path, email, result):
    return one_click_commands.persist_one_click_sms_failure(
        data, json_path, email, result, _one_click_command_context()
    )


# ─── Omakse handlers ──────────────────────────────────────────────────────────

def _omakse_extract(args):
    return omakse_commands.omakse_extract(args, omakse_commands.OmakseCommandContext(runtime_config=CFG))


def _omakse_us_pay(args):
    return omakse_commands.omakse_us_pay(args, omakse_commands.OmakseCommandContext(runtime_config=CFG))

import argparse
import importlib
import os
import sys
import time
from typing import Any

from .config import CFG, ConfigError, default_config_dir, initialize_config_shards, initialize_runtime_config
from .desktop.ipc import emit_result
from .kernel.paths import output_dir, runtime_file
from .kernel.sanitizer import sanitize_text
from .observability.diagnostics import install_safe_stdio, safe_print

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


class _LazyModule:
    """Import a command adapter on first attribute access.

    ``import sms_tool.cli`` used to pull every command adapter -- and through
    them ``requests`` / ``curl_cffi`` / ``gen_pp_link`` / ``upi_link`` -- before
    argparse even ran: ~890 ms of imports for a help screen. A proxy keeps every
    ``payment_commands.X`` call site and every ``patch.object(cli, ...)`` seat
    unchanged while moving the import into the branch that actually needs it.
    Deliberately no ``__slots__``: ``patch.object`` may need to set an attribute
    on the proxy.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._module: Any = None

    def __getattr__(self, attribute: str) -> Any:
        module = self._module
        if module is None:
            module = importlib.import_module(self._name, __package__)
            self._module = module
        return getattr(module, attribute)


account_commands = _LazyModule(".commands.accounts")
cli_dispatch = _LazyModule(".commands.cli_dispatch")
mailbox_commands = _LazyModule(".commands.mailbox_ops")
one_click_commands = _LazyModule(".commands.one_click")
payment_commands = _LazyModule(".commands.payment")
payment_link_commands = _LazyModule(".commands.payment_links")
registration_commands = _LazyModule(".commands.registration")

# `mailbox` and `registration` import `curl_cffi` at module top, so they must NOT
# be imported eagerly at the top of this file — otherwise `import sms_tool.cli`
# (and therefore the `--doctor` / `--desktop-read` lightweight commands) would
# crash whenever curl_cffi is missing. They are loaded on demand, only on the
# registration / one-click paths that actually need them.

# Module-level placeholders so these names always exist as attributes. This keeps
# `import sms_tool.cli` free of curl_cffi (the real values are imported lazily by
# _load_heavy_deps), while still letting tests `patch.object(cli, "...")` and direct
# handler calls reference them without a NameError/AttributeError.
# Annotated ``Any`` rather than left bare: the values are real callables after
# ``_load_heavy_deps()`` runs, but they start as ``None`` so `import sms_tool.cli`
# stays free of curl_cffi. A bare ``= None`` makes the checker type them ``None``
# and every call site then reports "object of type None cannot be called".
load_mailbox_pool: Any = None
remail_enabled: Any = None
_build_session_file: Any = None
_mailbox_snapshot: Any = None
run_email: Any = None

# Batch / storage / helper symbols use the same self-healing pattern: a
# placeholder plus a resolver that fills only the missing slots. Tests patch
# these names on the module, so they must exist as attributes before first use.
run_batch: Any = None
filter_registered_mailboxes: Any = None
database_path: Any = None
get_paypal_url: Any = None
list_paypal_accounts: Any = None
rebuild_from_session_dir: Any = None
upsert_account: Any = None
_read_email_file: Any = None
_payment_method: Any = None
_mailbox_from_explicit_args: Any = None
_one_click_sms_max_reuse: Any = None


def _load_batch_deps() -> None:
    """Resolve the batch runner (imports ``requests`` through its deps)."""
    global run_batch, filter_registered_mailboxes
    if run_batch is not None and filter_registered_mailboxes is not None:
        return
    from .registration.batch_runner import (
        filter_registered_mailboxes as _filter,
        run_batch_impl as _run,
    )

    if filter_registered_mailboxes is None:
        filter_registered_mailboxes = _filter
    if run_batch is None:
        run_batch = _run


def _load_storage_deps() -> None:
    """Resolve the SQLite index helpers used by commands outside registration."""
    global database_path, get_paypal_url, list_paypal_accounts, rebuild_from_session_dir, upsert_account
    if (
        database_path is not None
        and get_paypal_url is not None
        and list_paypal_accounts is not None
        and rebuild_from_session_dir is not None
        and upsert_account is not None
    ):
        return
    from .storage import (
        database_path as _database_path,
        get_paypal_url as _get_paypal_url,
        list_paypal_accounts as _list_paypal_accounts,
        rebuild_from_session_dir as _rebuild_from_session_dir,
        upsert_account as _upsert_account,
    )

    if database_path is None:
        database_path = _database_path
    if get_paypal_url is None:
        get_paypal_url = _get_paypal_url
    if list_paypal_accounts is None:
        list_paypal_accounts = _list_paypal_accounts
    if rebuild_from_session_dir is None:
        rebuild_from_session_dir = _rebuild_from_session_dir
    if upsert_account is None:
        upsert_account = _upsert_account


def _load_helper_deps() -> None:
    """Resolve the CLI helper hooks that command contexts inject."""
    global _read_email_file, _payment_method, _mailbox_from_explicit_args, _one_click_sms_max_reuse
    if (
        _read_email_file is not None
        and _payment_method is not None
        and _mailbox_from_explicit_args is not None
        and _one_click_sms_max_reuse is not None
    ):
        return
    from .commands.helpers import (
        mailbox_from_explicit_args as _mailbox_from_explicit_args_impl,
        one_click_sms_max_reuse as _one_click_sms_max_reuse_impl,
        payment_method as _payment_method_impl,
        read_email_file as _read_email_file_impl,
    )

    if _read_email_file is None:
        _read_email_file = _read_email_file_impl
    if _payment_method is None:
        _payment_method = _payment_method_impl
    if _mailbox_from_explicit_args is None:
        _mailbox_from_explicit_args = _mailbox_from_explicit_args_impl
    if _one_click_sms_max_reuse is None:
        _one_click_sms_max_reuse = _one_click_sms_max_reuse_impl


def _load_heavy_deps() -> None:
    """Resolve the lazily imported CLI dependencies for the current process.

    Deliberately **not** guarded by a one-shot latch. ``patch.object`` restores
    a patched symbol to its pre-patch value, so a latch set while
    ``load_mailbox_pool`` was a mock would leave the real symbol permanently
    ``None`` and a later ``--one-click-sms`` call would invoke ``None(...)``.
    (Reproduced 2026-10-18 by running the registration tests before the
    workbench tests; the full-suite order hid it because an earlier test had
    already resolved the symbols outside a patch.)

    Resolving by symbol presence instead is self-healing and no more expensive:
    once the modules are imported each ``from .x import ...`` is a
    ``sys.modules`` hit.  A symbol that is already resolved — including one a
    test intentionally patched — is never overwritten.
    """
    global load_mailbox_pool, remail_enabled, _build_session_file, _mailbox_snapshot, run_email
    _load_batch_deps()
    _load_storage_deps()
    _load_helper_deps()
    if load_mailbox_pool is None or remail_enabled is None:
        from .mailbox.router import (
            load_mailbox_pool as _lmp,
            remail_enabled as _re,
        )

        if load_mailbox_pool is None:
            load_mailbox_pool = _lmp
        if remail_enabled is None:
            remail_enabled = _re
    if _build_session_file is None or _mailbox_snapshot is None or run_email is None:
        from .registration.orchestrator import (
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


def _registration_proxy_lane(registration_driver: object = None) -> str:
    return cli_dispatch.registration_proxy_lane(registration_driver, CFG)


def _configured_registration_proxy(registration_driver: object = None, tracker=None) -> str:
    return cli_dispatch.configured_registration_proxy(CFG, registration_driver, tracker=tracker)


def _apply_proxy_argument_transport(args) -> None:
    cli_dispatch.apply_proxy_argument_transport(args)


def _normalize_proxy_args(args) -> None:
    cli_dispatch.normalize_proxy_args(args, CFG)


def _apply_registration_proxy_defaults(args) -> None:
    cli_dispatch.apply_registration_proxy_defaults(args, configured_proxy_fn=_configured_registration_proxy)


def _proxy_pool_values(args) -> list[str]:
    return cli_dispatch.proxy_pool_values(args, CFG, configured_proxy_fn=_configured_registration_proxy)


def _init_config_command() -> int:
    return cli_dispatch.init_config_command(
        safe_print_fn=safe_print,
        initialize_config_shards_fn=initialize_config_shards,
        default_config_dir_fn=default_config_dir,
        config_error_cls=ConfigError,
    )


def _registration_command_context():
    _load_heavy_deps()
    return registration_commands.RegistrationCommandContext(
        proxy_pool_values=_proxy_pool_values,
        load_mailbox_pool=load_mailbox_pool,
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
    from .payment.catalog import PAYMENT_CATALOG

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
        one_click,
        payment,
        paypal,
        proxy,
        quota,
        session,
        sub2api,
    )

    # Registration order is the original cli.py order, so --help output and
    # any ordering-sensitive parsing behaviour are unchanged.
    core.register(parser)
    payment.register(parser)
    session.register(parser)
    paypal.register(parser)
    sub2api.register(parser)
    quota.register(parser)
    codex.register(parser)
    one_click.register(parser)
    email_change.register(parser)
    proxy.register(parser)

    return parser


def main():
    # Parsed ahead of the real parser so `--init-config` can run on a machine
    # whose config is empty -- `initialize_runtime_config` would raise first.
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--init-config", action="store_true")
    pre_args, _ = pre_parser.parse_known_args()
    if pre_args.init_config:
        raise SystemExit(_init_config_command())
    initialize_runtime_config()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    install_safe_stdio()
    # Mirror stdout/stderr to `runtime/logs/backend_stdout.log`. Most of the
    # protocol lane reports through print(), which only ever reached the host's
    # IPC pipe -- so a run that failed before its first progress record left no
    # evidence on disk at all. The mirror is passive: it copies complete lines
    # without rerouting or decorating the stream the host parses.
    from .observability.stdout_mirror import install_stdout_mirror

    install_stdout_mirror()
    # Wire the rotating file logger (`runtime/logs/sms_tool.log`). Without this
    # the module is dead code and `logger.*` calls vanish: the codebase reports
    # through print(), so nothing is ever persisted and no exception carries a
    # traceback. `to_console=False` is deliberate -- stdout is the WPF host's
    # IPC channel (the `@@SMSWORKBENCH_V2@@` envelope plus the "Saved session:"
    # marker), and a StreamHandler would inject formatter-decorated lines into
    # that contract.
    from .observability.logging_setup import configure_logging

    configure_logging(to_console=False)

    parser = build_parser()
    args = parser.parse_args()
    if args.register_and_import:
        args.import_cpa = True
    _normalize_proxy_args(args)

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
                (
                    {"ok": False, "email": email, "error": str(result)}
                    if isinstance(result, Exception)
                    else {"ok": True, **result.to_dict()}
                )
                for email, result in zip(emails, results)
            ],
        }
        emit_result(payload, enabled=bool(args.desktop_ipc))
        if failures:
            raise SystemExit(3)
        return
    if args.change_email:
        from .accounts.account_email_change import EmailChangeRequest, change_email_batch, load_change_email_accounts
        from .commands.email_change import run_change_email

        run_change_email(
            args,
            load_accounts=load_change_email_accounts,
            change_email_batch=change_email_batch,
            request_type=EmailChangeRequest,
            emit_result=emit_result,
        )
        return
    if args.rebuild_sqlite:
        _load_storage_deps()
        count = rebuild_from_session_dir(base_dir)
        safe_print(f"[*] SQLite rebuilt: {database_path()} ({count} account record(s))")
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
    if getattr(args, "check_payment_qualification", False):
        _check_payment_qualification(args)
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if _dispatch_payment_cli(args):
        return
    if args.generate_ba_link:
        _generate_ba_link(args)
        return
    if args.generate_upi_qr:
        _generate_upi_qr(args)
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
    def _run_doctor_lazy(cfg, config_dir):
        from .doctor import run_doctor

        return run_doctor(cfg, config_dir)

    def _print_doctor_lazy(report):
        from .doctor import print_doctor_report

        return print_doctor_report(report)

    return cli_dispatch.main_early_commands(
        args,
        cfg=CFG,
        emit_result_fn=emit_result,
        import_account_json_fn=_import_account_json,
        inspect_accounts_fn=_inspect_accounts,
        safe_print_fn=safe_print,
        run_doctor_fn=_run_doctor_lazy,
        print_doctor_report_fn=_print_doctor_lazy,
        default_config_dir_fn=default_config_dir,
    )


def _resolve_target_success_plan(args, mailboxes, effective_count: int) -> tuple[int, int]:
    return cli_dispatch.resolve_target_success_plan(args, mailboxes, effective_count)


def _registration_exit_code(report, *, target_success: int = 0) -> int:
    return cli_dispatch.registration_exit_code(report, target_success=target_success)


def _run_email_registration_batch(
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
):
    return cli_dispatch.run_email_registration_batch(
        args,
        mailboxes,
        effective_count=effective_count,
        target_success=target_success,
        phone_pool=phone_pool,
        base_dir=base_dir,
        pipeline_started=pipeline_started,
        mailbox_seconds=mailbox_seconds,
        register_started=register_started,
        proxy_pool_values_fn=_proxy_pool_values,
        registration_commands_mod=registration_commands,
        registration_context_fn=_registration_command_context,
        run_batch_fn=run_batch,
        run_email_fn=run_email,
    )


def _main_registration_pipeline(args, base_dir) -> None:
    """The registration pipeline: load mailboxes, preflight, register, report.

    Extracted from ``main`` unchanged except for the two parameters --
    ``args`` and ``base_dir`` were the only locals it closed over.
    """
    _load_batch_deps()
    if getattr(args, "target_at200", 0):
        _run_target_at200(args, base_dir)
        return

    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"registration_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    pipeline_started = time.time()
    mailbox_started = time.time()
    mailboxes = load_mailbox_pool(args)
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
    if not mailboxes and not remail_enabled():
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
            safe_print(
                f"[!] Requested {requested_count} mailbox(es), ReMail returned {effective_count}; registering returned mailboxes only."
            )
    elif getattr(args, "buy_cfworker_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            safe_print(
                f"[!] Requested {requested_count} mailbox(es), CFWorker returned {effective_count}; registering returned mailboxes only."
            )
    elif getattr(args, "buy_smailr_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            safe_print(
                f"[!] Requested {requested_count} mailbox(es), Smailr returned {effective_count}; registering returned mailboxes only."
            )
    elif mailboxes and requested_count > len(mailboxes):
        effective_count = len(mailboxes)
        safe_print(
            f"[!] Requested {requested_count} account(s), but only {effective_count} mailbox(es) were loaded; registering loaded mailboxes only."
        )

    # Phone reuse pool (auto-enable when smsbower or paypal_auto phone is configured)
    phone_pool = _registration_phone_pool(args)

    # Phone registration mode (via SMSBower)
    if getattr(args, "phone_register", False):
        from .registration.orchestrator import run_phone_register

        proxy_pool = _proxy_pool_values(args)
        if len(proxy_pool) > 1:
            from .registration.batch_runner import select_registration_proxy_base

            selected_proxy = select_registration_proxy_base(proxy_pool, args.proxy)
            proxy_pool = [selected_proxy] if selected_proxy else []
        registration_proxy = proxy_pool[0] if proxy_pool else args.proxy
        results = []
        register_started = time.time()
        for i in range(effective_count):
            safe_print(f"\n{'=' * 60}")
            safe_print(f"[*] Phone registration {i + 1}/{effective_count}")
            safe_print(f"{'=' * 60}")
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
                safe_print(f"[OK] Phone registered: {result.get('phone', '')} | AT: [REDACTED]")
            else:
                safe_print(f"[FAIL] {result.get('error', 'unknown')}")
        report = _save_registration_results(
            args,
            results,
            effective_count=effective_count,
            base_dir=base_dir,
            pipeline_started=pipeline_started,
            mailbox_seconds=0,
            register_seconds=time.time() - register_started,
        )
        if bool(getattr(args, "desktop_ipc", False)):
            emit_result(report, enabled=True)
        exit_code = _registration_exit_code(report)
        if exit_code:
            raise SystemExit(exit_code)
        return

    effective_count, target_success = _resolve_target_success_plan(args, mailboxes, effective_count)
    register_started = time.time()
    results = _run_email_registration_batch(
        args,
        mailboxes,
        effective_count=effective_count,
        target_success=target_success,
        phone_pool=phone_pool,
        base_dir=base_dir,
        pipeline_started=pipeline_started,
        mailbox_seconds=mailbox_seconds,
        register_started=register_started,
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
    exit_code = _registration_exit_code(report, target_success=target_success)
    if exit_code:
        raise SystemExit(exit_code)


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
    return cli_dispatch.emit_registration_error(
        args,
        error,
        exit_code=exit_code,
        emit_result_fn=emit_result,
        safe_print_fn=safe_print,
        sanitize_text_fn=sanitize_text,
    )


def _check_registered_promotions(emails, workers=4, proxy=None, timeout=20, proxy_pool=None):
    return registration_commands.check_registered_promotions(
        emails,
        workers=workers,
        proxy=proxy,
        timeout=timeout,
        proxy_pool=proxy_pool,
    )


def _run_target_at200(args, base_dir):
    return registration_commands.run_target_at200(args, base_dir, _registration_command_context())


def _account_command_context():
    _load_storage_deps()
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
    _load_storage_deps()
    return mailbox_commands.MailboxCommandContext(upsert_account=upsert_account)


def _view_inbox(args):
    return mailbox_commands.view_inbox(args, _mailbox_command_context())


def _gmail_send(args):
    return mailbox_commands.gmail_send(args)


def _export_codex_json(args):
    return account_commands.export_codex_json(args, _account_command_context())


def _import_cpa(args):
    return account_commands.import_cpa(args, _account_command_context())


def _import_account_json(args):
    return account_commands.import_account_json(args)


def _check_promotion(args):
    return account_commands.check_promotion(args, _account_command_context())


def _check_payment_qualification(args):
    # Payment-lane command boundary: the probe calls the shared checkout
    # capability code, so it is dispatched alongside the other payment commands
    # (lane-coupling ratchet keeps the inventory of payment importers frozen).
    return payment_commands.check_payment_qualification(args, _payment_command_context())


def _refresh_cpa_quota(args):
    return account_commands.refresh_cpa_quota(args, _account_command_context())


def _inspect_accounts(args):
    return account_commands.inspect_accounts(args, _account_command_context())


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
    _load_helper_deps()
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


def _dispatch_payment_cli(args) -> bool:
    if args.list_payment_methods:
        _list_payment_methods()
        return True
    if args.test_payment_proxies:
        _test_payment_proxies(args)
        return True
    if args.extract_payment_link:
        _extract_payment_link(args)
        return True
    if getattr(args, "protocol_card_quote", False) or getattr(args, "protocol_card_pay", False):
        _protocol_card_payment(args)
        return True
    return False


def _protocol_card_payment(args):
    return payment_commands.protocol_card_payment(args, _payment_command_context())


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
        load_mailbox_pool=load_mailbox_pool,
        max_reuse=_one_click_sms_max_reuse,
        mailbox_snapshot=_mailbox_snapshot,
        persist_failure=_persist_one_click_sms_failure,
    )


def _one_click_sms(args):
    _load_heavy_deps()
    return one_click_commands.one_click_sms(args, _one_click_command_context())


def _one_click_scan(args):
    return one_click_commands.one_click_scan(args)


def _persist_one_click_sms_failure(data, json_path, email, result):
    from .codex.oauth import persist_one_click_sms_failure

    _load_storage_deps()
    return persist_one_click_sms_failure(data, json_path, email, result, upsert=upsert_account)

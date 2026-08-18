"""CLI boundary for registration pipeline commands.

The registration domain modules (``sms_tool.registration``, ``batch_runner``,
``storage``) own protocol behavior and persistence.  This module only
orchestrates ``argparse`` values into those domain calls.
``RegistrationCommandContext`` keeps the legacy CLI's replaceable hooks
explicit so tests can continue patching ``sms_tool.cli`` symbols.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import unique_emails


@dataclass(frozen=True)
class RegistrationCommandContext:
    """Legacy CLI hooks required by registration command orchestration."""

    proxy_pool_values: Callable[[Any], list[str]]
    load_mailbox_pool: Callable[[Any], list[Any]]
    run_batch: Callable[..., Any]
    run_email: Callable[..., Any]
    build_session_file: Callable[[Any], dict[str, Any]]
    save_results: Callable[..., Any]
    check_registered_promotions: Callable[..., Any]
    import_registered_accounts: Callable[..., Any]
    registration_phone_pool: Callable[[Any], Any]
    upsert_account: Callable[..., Any]
    database_path: Callable[[], str]
    runtime_file: Callable[[str], Path]
    runtime_config: Mapping[str, Any]


def preflight_registration_before_mailbox(args: Any, ctx: RegistrationCommandContext) -> dict:
    """Select a healthy auth route before a paid/disposable mailbox is claimed."""
    from ..registration import registration_network_preflight

    candidates = ctx.proxy_pool_values(args) or [None]
    last_error = None
    for candidate in candidates:
        try:
            result = registration_network_preflight(candidate, proxy_attempts=2)
        except Exception as exc:
            last_error = exc
            continue
        selected = str(result.get("proxy") or candidate or "").strip()
        if selected:
            ordered = [selected]
            ordered.extend(str(item).strip() for item in candidates if item and str(item).strip() != selected)
            args.proxy_pool = "\n".join(dict.fromkeys(ordered))
            args.proxy = selected
        else:
            args.proxy_pool = ""
            args.proxy = None
        return result
    raise RuntimeError(
        "registration_preflight_failed:no_healthy_route:"
        + (type(last_error).__name__ if last_error is not None else "unknown")
    )


def registration_phone_pool(args: Any):
    """Create the configured phone pool for registration flows that require SMS."""
    if getattr(args, "no_phone_reuse", False) or getattr(args, "registration_at_only", False):
        return None

    from ..phone_reuse import create_phone_pool, has_phone_reuse_config, print_phone_pool_status

    explicit = bool(getattr(args, "phone_reuse", False))
    auto_enable = has_phone_reuse_config()
    if not explicit and not auto_enable:
        return None

    phone_pool = create_phone_pool(
        max_reuse_count=getattr(args, "max_reuse_count", 0),
        send_cooldown_seconds=getattr(args, "phone_send_cooldown", None),
        source_override=getattr(args, "phone_source", None),
    )
    if not phone_pool.phones:
        if explicit:
            print("[Error] --phone-reuse enabled but no phone numbers configured. Add phone_reuse.smsbower.api_key, SMSBOWER_API_KEY, phone_reuse.phone_pool, or paypal_auto.phone_numbers")
            raise SystemExit(2)
        return None

    if auto_enable and not explicit:
        first = phone_pool.phones[0] if phone_pool.phones else None
        source = first.provider if first else "configured"
        print(f"[*] Auto-enabled phone verification ({source} mode)")
    print_phone_pool_status(phone_pool)
    return phone_pool


def check_registered_promotions(emails, workers=4, proxy=None, timeout=20):
    from ..account_promotion import refresh_promotion_statuses
    from ..sanitizer import sanitize

    targets = unique_emails(emails)
    if not targets:
        report = {"ok": True, "total": 0, "success": 0, "failed": 0, "trial_eligible": 0, "results": []}
        print("[*] Promotion check: no saved successful account to probe.")
        return report

    print(f"[*] Promotion check: probing {len(targets)} saved successful account(s)...")
    try:
        report = refresh_promotion_statuses(
            emails=targets,
            workers=max(1, int(workers or 1)),
            proxy=proxy,
            timeout=max(5, int(timeout or 20)),
        )
    except Exception as exc:
        report = {
            "ok": False,
            "total": len(targets),
            "success": 0,
            "failed": len(targets),
            "trial_eligible": 0,
            "results": [],
            "error": str(sanitize(exc)),
        }
        print(f"[!] Promotion check failed: {report['error']}")
        return report

    results = report.get("results") if isinstance(report.get("results"), list) else []
    trial_eligible = sum(
        1
        for item in results
        if isinstance(item, dict)
        and isinstance(item.get("probe"), dict)
        and bool(item["probe"].get("plus_trial_eligible"))
    )
    report["trial_eligible"] = trial_eligible
    print(
        "[*] Promotion check: "
        f"success={int(report.get('success') or 0)}/{int(report.get('total') or 0)} "
        f"trial_eligible={trial_eligible}"
    )
    for item in results:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        label = str(item.get("promotion_status") or "检测失败").strip()
        print(f"    {email}: {label}")
    return report


_PERSISTENCE_KEY = "_registration_persistence"


def registration_pipeline_timing(pipeline_started, mailbox_seconds, register_started):
    now = time.time()
    return {
        "mailbox_load_seconds": round(float(mailbox_seconds or 0), 2),
        "registration_batch_seconds": round(max(0.0, now - register_started), 2),
        "total_seconds": round(max(0.0, now - pipeline_started), 2),
    }


def persist_registration_result(
    args,
    data,
    base_dir,
    ctx: RegistrationCommandContext,
    *,
    pipeline_timing=None,
):
    """Persist one completed registration result and make retries idempotent."""
    from ..storage import record_registration_audit

    if not isinstance(data, dict):
        return {
            "status": "complete",
            "session_saved": 0,
            "db_saved": 0,
            "import_email": "",
        }

    marker = data.get(_PERSISTENCE_KEY)
    marker = marker if isinstance(marker, dict) else {}
    if marker.get("status") == "complete":
        return marker

    batch_id = str(getattr(args, "registration_batch_id", "") or "")
    data["batch_id"] = batch_id
    if isinstance(pipeline_timing, dict):
        data["pipeline_timing"] = dict(pipeline_timing)

    marker.setdefault("session_saved", 0)
    marker.setdefault("db_saved", 0)
    marker.setdefault("import_email", "")
    data[_PERSISTENCE_KEY] = marker

    try:
        if not data.get("success", False):
            failed_email = data.get("email") or data.get("phone") or "unknown"
            failed_error = str(data.get("error") or "registration_failed")
            if not marker.get("failure_reported"):
                print(f"[!] Registration failed for {failed_email}: {failed_error[:500]}")
                marker["failure_reported"] = True
            if not marker.get("failure_audited"):
                record_registration_audit(
                    data,
                    batch_id=batch_id,
                    state="terminal" if "account_deactivated" in failed_error.lower() else "failed",
                    runtime_config=ctx.runtime_config,
                )
                marker["failure_audited"] = True
            if "account_deactivated" in failed_error.lower() and not marker.get("dead_remail_recorded"):
                try:
                    from ..mailbox_remail import record_dead_remail_account

                    record_dead_remail_account(data, reason="account_deactivated")
                except Exception:
                    pass
                marker["dead_remail_recorded"] = True
            if (
                failed_error == "phone_already_registered_or_login_redirect"
                and not marker.get("skip_reported")
            ):
                print("    Skipped: phone number already registered, not saving to database")
                marker["skip_reported"] = True
            marker["status"] = "complete"
            marker.pop("error_type", None)
            return marker

        data["registration_state"] = "pending"
        if not marker.get("pending_audited"):
            record_registration_audit(
                data,
                batch_id=batch_id,
                state="pending",
                runtime_config=ctx.runtime_config,
            )
            marker["pending_audited"] = True

        session_data = ctx.build_session_file(data)
        if not session_data.get("access_token"):
            if not marker.get("missing_token_reported"):
                print("[!] Successful registration has no access_token; session file was not saved")
                marker["missing_token_reported"] = True
            marker["status"] = "complete"
            marker.pop("error_type", None)
            return marker

        session_data["batch_id"] = batch_id
        session_data["registration_state"] = "active"
        out_pattern = ctx.runtime_config.get("output", {}).get(
            "filename_pattern", "session_{email}_{timestamp}.json"
        )
        os.makedirs(base_dir, exist_ok=True)
        if not marker.get("session_path"):
            identifier = (session_data.get("email") or session_data.get("phone") or "unknown").replace("+", "")
            safe_identifier = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", identifier)
            fname = out_pattern.format(
                email=safe_identifier,
                phone=safe_identifier,
                timestamp=int(time.time()),
            )
            marker["session_path"] = os.path.join(base_dir, fname)
        out_path = marker["session_path"]

        if not marker.get("session_written"):
            temp_path = f"{out_path}.{os.getpid()}.tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as file_handle:
                    json.dump(session_data, file_handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, out_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            marker["session_written"] = True
            marker["session_saved"] = 1

        if not marker.get("db_completed"):
            marker["db_saved"] = 1 if ctx.upsert_account(session_data, json_path=out_path) else 0
            marker["db_completed"] = True
        if not marker.get("active_audited"):
            record_registration_audit(
                data,
                batch_id=batch_id,
                state="active",
                runtime_config=ctx.runtime_config,
            )
            marker["active_audited"] = True
        marker["import_email"] = str(session_data.get("email") or "")
        if not marker.get("saved_reported"):
            print(f"[*] Saved session: {out_path}")
            marker["saved_reported"] = True
        marker["status"] = "complete"
        marker.pop("error_type", None)
        return marker
    except Exception as exc:
        marker["status"] = "failed"
        marker["error_type"] = type(exc).__name__
        print(
            "[!] Immediate registration persistence failed "
            f"({marker['error_type']}); it will be retried during finalization."
        )
        return marker


def save_registration_results(
    args,
    results,
    effective_count,
    base_dir,
    pipeline_started,
    mailbox_seconds,
    register_seconds,
    ctx: RegistrationCommandContext,
):
    batch_id = str(getattr(args, "registration_batch_id", "") or "")
    pipeline_seconds = time.time() - pipeline_started
    pipeline_timing = {
        "mailbox_load_seconds": round(mailbox_seconds, 2),
        "registration_batch_seconds": round(register_seconds, 2),
        "total_seconds": round(pipeline_seconds, 2),
    }
    for data in filter(None, results):
        data["pipeline_timing"] = pipeline_timing

    saved_count = 0
    db_saved_count = 0
    import_emails = []
    for data in filter(None, results):
        outcome = persist_registration_result(
            args,
            data,
            base_dir,
            ctx,
            pipeline_timing=pipeline_timing,
        )
        saved_count += int(outcome.get("session_saved") or 0)
        db_saved_count += int(outcome.get("db_saved") or 0)
        if outcome.get("import_email"):
            import_emails.append(outcome["import_email"])

    success_count = sum(1 for r in results if r and r.get("success"))
    print(f"[*] SQLite index: {ctx.database_path()} ({db_saved_count} record(s) upserted)")
    print(f"\n[*] Done. {success_count}/{effective_count} registered successfully, {saved_count} session file(s) saved.")
    quality = None
    if getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None):
        from ..mailbox_remail import record_remail_batch_quality
        quality = record_remail_batch_quality(batch_id, results, requested=effective_count)
        print(
            f"[*] ReMail quality: deactivated={quality['account_deactivated']}/"
            f"{quality['requested']} halt={quality['halt_replenishment']}"
        )

    promotion_report = None
    if getattr(args, "check_promotion_after_registration", False):
        promotion_report = ctx.check_registered_promotions(
            import_emails,
            workers=max(1, int(getattr(args, "workers", 4) or 4)),
            proxy=getattr(args, "proxy", None),
            timeout=max(5, int(getattr(args, "refresh_timeout", 20) or 20)),
        )

    if getattr(args, "import_cpa", False):
        ctx.import_registered_accounts(args, import_emails)
    return {
        "batch_id": batch_id,
        "success": success_count,
        "session_saved": saved_count,
        "db_saved": db_saved_count,
        "quality": quality,
        "promotion": promotion_report,
    }


def run_target_at200(args, base_dir, ctx: RegistrationCommandContext):
    """Bounded ReMail replenishment mode for a stable AT-200 target."""
    if not (getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None)):
        print("[Error] --target-at200 requires --buy-remail-mailbox or --remail-service-mode")
        raise SystemExit(2)
    target = max(1, int(args.target_at200 or 1))
    max_purchases = max(target, int(args.max_mailbox_purchases or target * 2))
    max_cost = max(0.0, float(args.max_remail_cost or 0.0))
    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"target_at200_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    original_count = args.count
    purchased = 0
    active = 0
    spent = 0.0
    rounds = []
    halted = False
    promotion_total = 0
    promotion_success = 0
    trial_eligible = 0
    started = time.time()
    phone_pool = ctx.registration_phone_pool(args)
    try:
        while active < target and purchased < max_purchases and not halted:
            quantity = min(target - active, max_purchases - purchased)
            args.count = quantity
            mailboxes = ctx.load_mailbox_pool(args)
            if not mailboxes:
                break
            purchased += len(mailboxes)
            for mailbox in mailboxes:
                try:
                    spent += float(getattr(mailbox, "price", 0) or 0)
                except (TypeError, ValueError):
                    pass
            if max_cost and spent > max_cost:
                halted = True
                break
            round_started = time.time()
            def persist_completed_result(_index, result):
                persist_registration_result(
                    args,
                    result,
                    base_dir,
                    ctx,
                    pipeline_timing=registration_pipeline_timing(
                        round_started,
                        0,
                        round_started,
                    ),
                )

            results = ctx.run_batch(
                count=len(mailboxes),
                proxy=args.proxy,
                proxy_pool=ctx.proxy_pool_values(args),
                mailboxes=mailboxes,
                workers=args.workers,
                phone_pool=phone_pool,
                codex_oauth=False,
                registration_mode=args.registration_mode,
                enroll_2fa=not getattr(args, "no_2fa", False),
                run_email_func=ctx.run_email,
                on_result=persist_completed_result,
            )
            saved = ctx.save_results(
                args,
                results,
                effective_count=len(mailboxes),
                base_dir=base_dir,
                pipeline_started=round_started,
                mailbox_seconds=0,
                register_seconds=time.time() - round_started,
            ) or {}
            gained = int(saved.get("success") or 0)
            active += gained
            quality = saved.get("quality") if isinstance(saved.get("quality"), dict) else {}
            promotion = saved.get("promotion") if isinstance(saved.get("promotion"), dict) else {}
            promotion_total += int(promotion.get("total") or 0)
            promotion_success += int(promotion.get("success") or 0)
            trial_eligible += int(promotion.get("trial_eligible") or 0)
            halted = bool(quality.get("halt_replenishment"))
            rounds.append({
                "requested": quantity,
                "mailboxes": len(mailboxes),
                "active": gained,
                "deactivated": int(quality.get("account_deactivated") or 0),
                "promotion_total": int(promotion.get("total") or 0),
                "promotion_success": int(promotion.get("success") or 0),
                "trial_eligible": int(promotion.get("trial_eligible") or 0),
                "halted": halted,
            })
    finally:
        args.count = original_count
    report = {
        "ok": active >= target,
        "batch_id": args.registration_batch_id,
        "target_at200": target,
        "active": active,
        "purchased": purchased,
        "max_purchases": max_purchases,
        "estimated_cost": round(spent, 4),
        "max_cost": max_cost,
        "supplier_halted": halted,
        "promotion_total": promotion_total,
        "promotion_success": promotion_success,
        "trial_eligible": trial_eligible,
        "elapsed_seconds": round(time.time() - started, 2),
        "rounds": rounds,
    }
    report_path = ctx.runtime_file(f"registration_target_{args.registration_batch_id}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(3)

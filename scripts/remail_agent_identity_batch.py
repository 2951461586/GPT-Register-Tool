import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.agent_identity import load_agent_identity, validate_agent_identity
from sms_tool.config import CFG
from sms_tool.mailbox_remail import (
    _mailbox_from_order,
    _order_options,
    _redact,
    _remail_request,
)
from sms_tool.paths import output_dir, runtime_file
from sms_tool.registration import _build_session_file, run_batch
from sms_tool.storage import get_account_record, upsert_account
from sms_tool.sub2api_import import import_sub2api_sessions


def _write_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _recover_orders(created_since, limit):
    response = _remail_request("GET", "/v1/open/orders", auth=True, params={"limit": 500})
    items = response.get("items") if isinstance(response, dict) else []
    candidates = [
        item for item in (items or [])
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() == "active"
        and str(item.get("serviceMode") or "").lower() == "purchase"
        and _timestamp(item.get("createdAt")) >= float(created_since or 0)
    ]
    candidates.sort(key=lambda item: (_timestamp(item.get("createdAt")), int(item.get("id") or 0)))
    accounts = []
    for item in candidates[:max(0, int(limit or 0))]:
        detail = _remail_request(
            "GET",
            "/v1/open/orders/" + str(item.get("orderNo") or ""),
            auth=True,
        )
        accounts.append(_mailbox_from_order(detail, service_mode="purchase"))
    return accounts


def _purchase_chunk(quantity, args):
    mode, supply, payload = _order_options(args, service_mode="purchase")
    payload["quantity"] = max(1, int(quantity))
    idempotency_key = str(uuid.uuid4())
    last_error = None
    for attempt in range(3):
        try:
            results = _remail_request(
                "POST",
                "/v1/open/orders/batch",
                auth=True,
                headers={"Idempotency-Key": idempotency_key},
                params={"serviceMode": mode, "supply": supply},
                json=payload,
                timeout=120,
            )
            accounts = []
            failures = []
            for item in results if isinstance(results, list) else []:
                if str(item.get("status") or "").lower() == "succeeded" and item.get("order"):
                    accounts.append(_mailbox_from_order(item["order"], service_mode=mode))
                else:
                    failures.append(_redact(item.get("error") or {"index": item.get("index")}))
            if failures:
                print(f"[!] ReMail chunk returned {len(failures)} failed item(s)")
            return accounts
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"ReMail chunk failed after idempotent retries: {last_error}")


def _acquire_mailboxes(args, state_path):
    accounts = _recover_orders(args.recover_since, args.count) if args.recover_since else []
    seen = {account.email.lower() for account in accounts}
    _write_json(state_path, {"mailboxes": [asdict(account) for account in accounts]}, private=True)
    attempts = 0
    while not args.recover_only and len(accounts) < args.count and attempts < 12:
        attempts += 1
        needed = args.count - len(accounts)
        chunk = _purchase_chunk(min(args.purchase_chunk_size, needed), args)
        for account in chunk:
            if account.email.lower() not in seen:
                seen.add(account.email.lower())
                accounts.append(account)
        _write_json(state_path, {"mailboxes": [asdict(account) for account in accounts]}, private=True)
        if not chunk:
            break
    if len(accounts) < args.count and not args.allow_partial:
        raise RuntimeError(f"only {len(accounts)} of {args.count} ReMail mailboxes were acquired")
    return accounts[:args.count]


def _persist_registration(result, index):
    if not result.get("success"):
        upsert_account(result)
        return ""
    session = _build_session_file(result)
    if not session.get("access_token") or not session.get("email"):
        return ""
    directory = output_dir(CFG)
    directory.mkdir(parents=True, exist_ok=True)
    safe_email = "".join(ch if ch.isalnum() or ch in "_.@-" else "_" for ch in session["email"])
    path = directory / f"session_{safe_email}_{int(time.time()) + index}.json"
    _write_json(path, session, private=True)
    upsert_account(session, json_path=str(path))
    return str(path)


def _safe_registration_result(result, index):
    email = str(result.get("email") or "").strip().lower()
    identity_summary = result.get("agent_identity_registration")
    identity_summary = identity_summary if isinstance(identity_summary, dict) else {}
    local = load_agent_identity(email) if email else {"ok": False}
    structural = validate_agent_identity(local.get("data") or {}) if local.get("ok") else local
    return {
        "index": index + 1,
        "email": email,
        "registered": bool(result.get("success") and result.get("access_token")),
        "agent_identity": bool(identity_summary.get("ok")),
        "structural_valid": bool(structural.get("ok")),
        "error": str(result.get("error") or identity_summary.get("error") or "")[:120],
    }


def _import_results(emails, args):
    result = import_sub2api_sessions(
        emails,
        workers=args.workers,
        refresh=False,
        proxy=args.registration_proxy,
        timeout=120,
        group_name=args.group_name,
        proxy_name=args.proxy_name,
        priority=1,
        concurrency=args.account_concurrency,
        auth_mode="agent_identity",
        verify_after_import=True,
    )
    rows = []
    for item in result.get("results") or []:
        sub2api = item.get("sub2api") if isinstance(item.get("sub2api"), dict) else {}
        verification = sub2api.get("verification") if isinstance(sub2api.get("verification"), dict) else {}
        rows.append({
            "email": str(item.get("email") or "").strip().lower(),
            "imported": bool(
                int(sub2api.get("failed") or 0) == 0
                and int(sub2api.get("created") or 0) + int(sub2api.get("updated") or 0) > 0
            ),
            "remote_config_valid": bool(
                verification.get("ok")
                and verification.get("structural_only")
                and not verification.get("execution_tested")
            ),
            "created": int(sub2api.get("created") or 0),
            "updated": int(sub2api.get("updated") or 0),
            "account_id": verification.get("account_id"),
            "group_ids": verification.get("group_ids") or [],
            "proxy_id": verification.get("proxy_id"),
            "status": str(verification.get("status") or ""),
            "error": str(item.get("error") or sub2api.get("error") or verification.get("error") or "")[:120],
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description="Register ReMail Agent Identity accounts without delivery-account execution probes")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--purchase-chunk-size", type=int, default=20)
    parser.add_argument("--recover-since", type=float, default=0)
    parser.add_argument("--registration-proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--group-name", default="GPT-Free")
    parser.add_argument("--proxy-name", default="mihomo-JP")
    parser.add_argument("--account-concurrency", type=int, default=10)
    parser.add_argument("--remail-supply", choices=["private_first", "public_only"], default=None)
    parser.add_argument("--remail-project-id", type=int, default=None)
    parser.add_argument("--remail-product-id", type=int, default=None)
    parser.add_argument("--remail-email-suffix", default=None)
    parser.add_argument("--output-report", default="")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--skip-local-registered", action="store_true")
    args = parser.parse_args()
    args.count = max(1, min(int(args.count or 1), 100))
    args.workers = max(1, min(int(args.workers or 1), 10))
    args.purchase_chunk_size = max(1, min(int(args.purchase_chunk_size or 20), 20))
    args.remail_service_mode = "purchase"

    started = time.time()
    stamp = int(started)
    state_path = runtime_file(CFG, f"remail_agent_identity_batch_{stamp}.private.json")
    report_path = Path(args.output_report) if args.output_report else runtime_file(
        CFG,
        f"remail_agent_identity_batch_{stamp}.json",
    )
    mailboxes = _acquire_mailboxes(args, state_path)
    print(f"[*] ReMail mailboxes ready: {len(mailboxes)}")
    registration_mailboxes = mailboxes
    if args.skip_local_registered:
        registration_mailboxes = [
            mailbox for mailbox in mailboxes
            if not get_account_record(mailbox.email).get("success")
        ]
        print(f"[*] ReMail mailboxes pending registration: {len(registration_mailboxes)}")
    raw_results = run_batch(
        count=len(registration_mailboxes),
        proxy=args.registration_proxy,
        mailboxes=registration_mailboxes,
        workers=args.workers,
        phone_pool=None,
        codex_oauth=False,
    )
    registration_rows = []
    for index, result in enumerate(raw_results):
        _persist_registration(result, index)
        registration_rows.append(_safe_registration_result(result, index))
    ready_emails = [row["email"] for row in registration_rows if row["registered"] and row["structural_valid"]]
    import_rows = _import_results(ready_emails, args)
    import_by_email = {row["email"]: row for row in import_rows}
    rows = [{**row, **import_by_email.get(row["email"], {})} for row in registration_rows]
    summary = {
        "requested": args.count,
        "mailboxes": len(mailboxes),
        "registration_attempted": len(registration_mailboxes),
        "registered": sum(bool(row.get("registered")) for row in rows),
        "agent_identity": sum(bool(row.get("agent_identity")) for row in rows),
        "structural_valid": sum(bool(row.get("structural_valid")) for row in rows),
        "imported": sum(bool(row.get("imported")) for row in rows),
        "remote_config_valid": sum(bool(row.get("remote_config_valid")) for row in rows),
        "created": sum(int(row.get("created") or 0) for row in rows),
        "updated": sum(int(row.get("updated") or 0) for row in rows),
        "errors": dict(Counter(row.get("error") for row in rows if row.get("error"))),
        "elapsed_seconds": round(time.time() - started, 1),
        "actual_responses_requests": 0,
    }
    report = {"ok": summary["remote_config_valid"] == args.count, "summary": summary, "results": rows}
    _write_json(report_path, report)
    print(json.dumps({"report_path": str(report_path), "state_path": str(state_path), **report}, ensure_ascii=False))
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())

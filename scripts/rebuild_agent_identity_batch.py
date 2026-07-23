import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sms_tool.agent_identity import rebuild_agent_identity, validate_agent_identity
from sms_tool.config import CFG
from sms_tool.paths import runtime_file
from sms_tool.storage import database_path, init_database
from sms_tool.sub2api_import import import_sub2api_session


def _read_json(path):
    raw = Path(path).read_bytes()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return json.loads(raw.decode(encoding))


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _emails_from_report(report):
    rows = report.get("results") if isinstance(report.get("results"), list) else []
    emails = [str(row.get("email") or "").strip().lower() for row in rows if isinstance(row, dict)]
    return list(dict.fromkeys(email for email in emails if email))


def _local_registered_emails():
    init_database()
    connection = sqlite3.connect(database_path())
    try:
        rows = connection.execute(
            "SELECT email FROM accounts WHERE success=1 AND access_token<>'' ORDER BY updated_at, email"
        ).fetchall()
    finally:
        connection.close()
    return list(dict.fromkeys(str(row[0] or "").strip().lower() for row in rows if str(row[0] or "").strip()))


def _safe_error(result, default):
    value = str((result or {}).get("error") or default)
    if value.startswith("HTTP "):
        return value.split(":", 1)[0]
    return value[:100]


def _validate_private_key(identity):
    try:
        der = base64.b64decode(str(identity.get("agent_private_key") or ""), validate=True)
        key = serialization.load_der_private_key(der, password=None)
        return isinstance(key, Ed25519PrivateKey)
    except Exception:
        return False


def _process(email, args):
    rebuilt = rebuild_agent_identity(
        email=email,
        proxy=args.registration_proxy,
        timeout=args.timeout,
    )
    item = {
        "email": email,
        "rebuilt": bool(rebuilt.get("ok")),
        "token_source": str(rebuilt.get("token_source") or ""),
        "runtime_id_valid": False,
        "ed25519_pkcs8_valid": False,
        "imported": False,
        "remote_config_valid": False,
        "created": 0,
        "updated": 0,
        "account_ids": [],
        "group_ids": [],
        "proxy_id": None,
        "status": "",
    }
    if not rebuilt.get("ok"):
        item["attempts"] = [
            {
                "source": str(attempt.get("source") or ""),
                "ok": bool(attempt.get("ok")),
                "error": _safe_error(attempt, "agent_registration_failed"),
            }
            for attempt in (rebuilt.get("attempts") or [])
            if isinstance(attempt, dict)
        ]
        last_attempt = item["attempts"][-1] if item["attempts"] else {}
        item["error"] = str(last_attempt.get("error") or _safe_error(rebuilt, "agent_identity_rebuild_failed"))
        return item

    validation = validate_agent_identity(rebuilt.get("data") or {})
    if not validation.get("ok"):
        item["error"] = _safe_error(validation, "agent_identity_validation_failed")
        return item
    identity = validation["data"]["agent_identity"]
    item["runtime_id_valid"] = bool(str(identity.get("agent_runtime_id") or "").strip())
    item["ed25519_pkcs8_valid"] = _validate_private_key(identity)
    if not (item["runtime_id_valid"] and item["ed25519_pkcs8_valid"]):
        item["error"] = "agent_identity_structural_validation_failed"
        return item

    imported = import_sub2api_session(
        email=email,
        session_file=str(rebuilt.get("path") or ""),
        refresh=False,
        timeout=max(args.timeout, 120),
        group_name=args.group_name,
        proxy_name=args.proxy_name,
        priority=args.priority,
        concurrency=args.account_concurrency,
        auth_mode="agent_identity",
        verify_after_import=True,
    )
    sub2api = imported.get("sub2api") if isinstance(imported.get("sub2api"), dict) else {}
    item["created"] = int(sub2api.get("created") or 0)
    item["updated"] = int(sub2api.get("updated") or 0)
    item["imported"] = bool(
        int(sub2api.get("failed") or 0) == 0
        and item["created"] + item["updated"] > 0
    )
    data = sub2api.get("data") if isinstance(sub2api.get("data"), dict) else {}
    item["account_ids"] = [
        int(row["account_id"])
        for row in (data.get("items") or [])
        if isinstance(row, dict) and row.get("account_id")
    ]
    verification = sub2api.get("verification") if isinstance(sub2api.get("verification"), dict) else {}
    item["remote_config_valid"] = bool(
        verification.get("ok")
        and verification.get("structural_only")
        and not verification.get("execution_tested")
    )
    item["group_ids"] = verification.get("group_ids") or []
    item["proxy_id"] = verification.get("proxy_id")
    item["status"] = str(verification.get("status") or "")
    if not (item["imported"] and item["remote_config_valid"]):
        item["error"] = _safe_error(
            verification if not item["remote_config_valid"] else sub2api,
            "sub2api_import_failed",
        )
    return item


def _summary(results, elapsed_seconds):
    return {
        "requested": len(results),
        "rebuilt": sum(bool(row.get("rebuilt")) for row in results),
        "runtime_id_valid": sum(bool(row.get("runtime_id_valid")) for row in results),
        "ed25519_pkcs8_valid": sum(bool(row.get("ed25519_pkcs8_valid")) for row in results),
        "imported": sum(bool(row.get("imported")) for row in results),
        "remote_config_valid": sum(bool(row.get("remote_config_valid")) for row in results),
        "created": sum(int(row.get("created") or 0) for row in results),
        "updated": sum(int(row.get("updated") or 0) for row in results),
        "token_sources": dict(Counter(row.get("token_source") or "none" for row in results)),
        "proxy_ids": sorted({row.get("proxy_id") for row in results if row.get("proxy_id")}),
        "group_ids": sorted({value for row in results for value in (row.get("group_ids") or [])}),
        "statuses": dict(Counter(row.get("status") or "unknown" for row in results)),
        "errors": dict(Counter(row.get("error") for row in results if row.get("error"))),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "actual_responses_requests": 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Rebuild unused Agent Identities and import them without execution probes")
    parser.add_argument("--input-report", default="")
    parser.add_argument("--all-local", action="store_true")
    parser.add_argument("--output-report", default="")
    parser.add_argument("--registration-proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--group-name", default="GPT-Free")
    parser.add_argument("--proxy-name", default="mihomo-JP")
    parser.add_argument("--priority", type=int, default=1)
    parser.add_argument("--account-concurrency", type=int, default=10)
    args = parser.parse_args()

    if args.all_local:
        emails = _local_registered_emails()
    elif args.input_report:
        source = _read_json(args.input_report)
        emails = _emails_from_report(source)
    else:
        raise RuntimeError("--input-report or --all-local is required")
    if not emails:
        raise RuntimeError("input report contains no account emails")
    output_path = Path(args.output_report) if args.output_report else runtime_file(
        CFG,
        f"remail_agent_identity_rebuild_{int(time.time())}.json",
    )
    workers = max(1, min(int(args.workers or 1), 10, len(emails)))
    started = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process, email, args): email for email in emails}
        for future in as_completed(futures):
            email = futures[future]
            try:
                item = future.result()
            except Exception as exc:
                item = {
                    "email": email,
                    "rebuilt": False,
                    "imported": False,
                    "remote_config_valid": False,
                    "error": type(exc).__name__,
                }
            results.append(item)
            print(json.dumps({
                "progress": len(results),
                "total": len(emails),
                "rebuilt": item.get("rebuilt", False),
                "imported": item.get("imported", False),
                "remote_config_valid": item.get("remote_config_valid", False),
                "error": item.get("error", ""),
            }, ensure_ascii=False), flush=True)

    order = {email: index for index, email in enumerate(emails)}
    results.sort(key=lambda row: order[row["email"]])
    summary = _summary(results, time.time() - started)
    report = {
        "ok": summary["remote_config_valid"] == len(emails),
        "summary": summary,
        "results": results,
    }
    _write_json(output_path, report)
    print(json.dumps({"report_path": str(output_path), "ok": report["ok"], "summary": summary}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())

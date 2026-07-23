import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi import requests as curl_requests

from sms_tool.config import CFG
from sms_tool.agent_identity import load_agent_identity, validate_agent_identity
from sms_tool.mailbox_remail import _create_remail_mailboxes
from sms_tool.mailbox_types import MailboxAccount
from sms_tool.paypal_proxy import probe_proxy, rotate_proxy_session
from sms_tool.phone_reuse import create_phone_pool, has_phone_reuse_config
from sms_tool.paths import output_dir, runtime_file
from sms_tool.registration import _build_session_file, _mailbox_snapshot, run_email
from sms_tool.storage import upsert_account
from sms_tool.sub2api_import import (
    _request_json,
    _resolve_sub2api_config,
    import_sub2api_session,
)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _proxy_template():
    upi = CFG.get("upi") if isinstance(CFG.get("upi"), dict) else {}
    stages = upi.get("stage_proxies") if isinstance(upi.get("stage_proxies"), dict) else {}
    value = str(stages.get("checkout") or stages.get("provider") or "").strip()
    if value:
        return value
    paypal = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    proxies = paypal.get("proxies") if isinstance(paypal.get("proxies"), list) else []
    return str(proxies[0] if proxies else "").strip()


def _sub2api_groups_and_proxies(count, prefix):
    cfg = _resolve_sub2api_config()
    origin = cfg["origin"]
    token = cfg["api_token"]
    groups_result = _request_json(origin, "/api/v1/admin/groups/all", token=token, method="GET")
    if not groups_result.get("ok"):
        raise RuntimeError(f"SUB2API groups lookup failed: {groups_result.get('error', 'unknown')}")
    groups = groups_result.get("data") if isinstance(groups_result.get("data"), list) else []
    group = next(
        (
            item
            for item in groups
            if str(item.get("name") or "").strip().lower() == "gpt-free"
            and str(item.get("platform") or "openai").strip().lower() in {"", "openai"}
        ),
        None,
    )
    if not group:
        raise RuntimeError("SUB2API GPT-Free group is missing")

    proxies_result = _request_json(
        origin,
        "/api/v1/admin/proxies/all?with_count=true",
        token=token,
        method="GET",
    )
    if not proxies_result.get("ok"):
        raise RuntimeError(f"SUB2API proxies lookup failed: {proxies_result.get('error', 'unknown')}")
    proxies = proxies_result.get("data") if isinstance(proxies_result.get("data"), list) else []
    by_name = {str(item.get("name") or ""): item for item in proxies}
    resolved = []
    for index in range(1, count + 1):
        name = f"{prefix}{index:02d}"
        proxy = by_name.get(name)
        proxy_id = int((proxy or {}).get("id") or 0)
        if proxy_id <= 0:
            raise RuntimeError(f"SUB2API proxy is missing: {name}")
        resolved.append({"id": proxy_id, "name": name})
    return cfg, int(group.get("id") or 0), resolved


def _update_remote_proxy(cfg, remote_proxy, proxy_url):
    parsed = urlsplit(proxy_url)
    payload = {
        "name": remote_proxy["name"],
        "protocol": parsed.scheme or "http",
        "host": parsed.hostname or "",
        "port": parsed.port or 80,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "status": "active",
        "fallback_mode": "none",
        "expiry_warn_days": 0,
    }
    result = _request_json(
        cfg["origin"],
        f"/api/v1/admin/proxies/{remote_proxy['id']}",
        token=cfg["api_token"],
        method="PUT",
        body=payload,
        timeout=45,
    )
    if not result.get("ok"):
        raise RuntimeError(f"SUB2API proxy update failed: {result.get('error', 'unknown')}")


def _registration_proxy(template, country, attempts=4):
    last = None
    for _ in range(max(1, attempts)):
        proxy = rotate_proxy_session(template, country)
        probe = probe_proxy(proxy, expected_country=country, stage="registration", timeout=20).to_dict()
        last = probe
        if not probe.get("ok"):
            continue
        try:
            response = curl_requests.get(
                "https://chatgpt.com/",
                proxies={"http": proxy, "https": proxy},
                timeout=25,
                impersonate="chrome110",
            )
            marker = "chatgpt" in str(response.text or "").lower() or "_next/static" in str(response.text or "").lower()
            if response.status_code == 200 and marker:
                return proxy, probe
        except Exception:
            pass
    raise RuntimeError(f"registration proxy preflight failed: {(last or {}).get('error', 'chatgpt_unreachable')}")


def _set_remote_schedulable(cfg, account_ids, schedulable):
    for account_id in account_ids:
        result = _request_json(
            cfg["origin"],
            f"/api/v1/admin/accounts/{int(account_id)}/schedulable",
            token=cfg["api_token"],
            method="POST",
            body={"schedulable": bool(schedulable)},
            timeout=30,
        )
        if not result.get("ok"):
            raise RuntimeError(f"SUB2API schedulable update failed for account {int(account_id)}")


def _persist_session(result, batch_started):
    if not result.get("success") or not result.get("access_token"):
        upsert_account(result)
        return ""
    session = _build_session_file(result)
    pattern = CFG.get("output", {}).get("filename_pattern", "session_{email}_{timestamp}.json")
    safe_email = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", str(session.get("email") or "unknown"))
    path = Path(output_dir(CFG)) / pattern.format(email=safe_email, phone=safe_email, timestamp=batch_started)
    _write_json(path, session)
    upsert_account(session, json_path=str(path))
    return str(path)


def main():
    parser = argparse.ArgumentParser(description="Serial ReMail purchase-mode registration and SUB2API import")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--country", default="US")
    parser.add_argument("--delay-seconds", type=int, default=20)
    parser.add_argument("--proxy-prefix", default="GPT-Free-US-")
    parser.add_argument("--state-file", default="", help="Resume purchased mailboxes from an existing batch state")
    args = parser.parse_args()

    count = max(1, min(int(args.count or 10), 20))
    country = str(args.country or "US").strip().upper()
    template = _proxy_template()
    if not template:
        raise RuntimeError("no authenticated external proxy template is configured")
    sub2api_cfg, group_id, remote_proxies = _sub2api_groups_and_proxies(count, args.proxy_prefix)
    if not has_phone_reuse_config():
        raise RuntimeError("Codex OAuth verification requires a configured phone pool")
    phone_pool = create_phone_pool()
    if phone_pool.total_capacity < count:
        raise RuntimeError(f"phone pool capacity {phone_pool.total_capacity} is below requested count {count}")

    if str(args.state_file or "").strip():
        state_path = Path(str(args.state_file).strip()).resolve()
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        mailboxes = [MailboxAccount(**{
            key: value
            for key, value in dict(item).items()
            if key in MailboxAccount.__dataclass_fields__
        }) for item in (state.get("mailboxes") or []) if isinstance(item, dict)]
        if not mailboxes:
            raise RuntimeError("resume state contains no mailboxes")
        count = len(mailboxes)
        batch_started = int(state.get("started_at") or time.time())
        previous_results = state.get("results") if isinstance(state.get("results"), list) else []
        state["previous_results"] = previous_results
        state["results"] = []
        state["retry_started_at"] = int(time.time())
        state.pop("completed_at", None)
        state.pop("summary", None)
    else:
        batch_started = int(time.time())
        state_path = runtime_file(CFG, f"remail_sub2api_batch_{batch_started}.json")
        purchase_args = SimpleNamespace(
            count=count,
            remail_service_mode="purchase",
            remail_supply=None,
            remail_project_id=None,
            remail_product_id=None,
            remail_email_suffix=None,
        )
        mailboxes = _create_remail_mailboxes(purchase_args, service_mode="purchase")
        state = {
            "started_at": batch_started,
            "requested": count,
            "purchased": len(mailboxes),
            "group_id": group_id,
            "mailboxes": [_mailbox_snapshot(mailbox) for mailbox in mailboxes],
            "results": [],
        }
    _write_json(state_path, state)

    for index, mailbox in enumerate(mailboxes):
        remote_proxy = remote_proxies[index]
        item = {
            "index": index + 1,
            "email": mailbox.email,
            "proxy_id": remote_proxy["id"],
            "registered": False,
            "agent_identity": False,
            "local_agent_verified": False,
            "imported": False,
            "verified": False,
        }
        try:
            proxy, probe = _registration_proxy(template, country)
            item["exit_ip"] = probe.get("ip", "")
            item["exit_country"] = probe.get("country_code", "")
            _update_remote_proxy(sub2api_cfg, remote_proxy, proxy)
            result = run_email(
                proxy=proxy,
                mailbox=mailbox,
                paypal_link=False,
                codex_oauth=True,
                phone_pool=phone_pool,
            )
            item["registered"] = bool(result.get("success") and result.get("access_token"))
            item["registration_error"] = "" if item["registered"] else str(result.get("error") or "registration_failed")[:300]
            identity = result.get("agent_identity_registration") if isinstance(result.get("agent_identity_registration"), dict) else {}
            item["agent_identity"] = bool(identity.get("ok"))
            item["agent_error"] = "" if item["agent_identity"] else str(identity.get("error") or "")[:200]
            session_path = _persist_session(result, batch_started + index)
            item["session_path"] = session_path
            if item["registered"] and item["agent_identity"]:
                local_identity = load_agent_identity(mailbox.email)
                if local_identity.get("ok"):
                    local_probe = validate_agent_identity(local_identity["data"])
                else:
                    local_probe = local_identity
                item["local_agent_verified"] = bool(local_probe.get("ok"))
                item["local_agent_error"] = "" if item["local_agent_verified"] else str(
                    local_probe.get("error") or local_probe.get("message") or "agent_identity_probe_failed"
                )[:300]
            if item["registered"] and item["agent_identity"] and item["local_agent_verified"]:
                imported = import_sub2api_session(
                    email=mailbox.email,
                    session_file=session_path,
                    refresh=False,
                    proxy=proxy,
                    timeout=120,
                    group_ids=[group_id],
                    proxy_id=remote_proxy["id"],
                    priority=1,
                    concurrency=10,
                    auth_mode="agent_identity",
                    verify_after_import=True,
                )
                sub = imported.get("sub2api") if isinstance(imported.get("sub2api"), dict) else {}
                item["imported"] = bool((sub.get("created", 0) or sub.get("updated", 0)) and sub.get("failed", 0) == 0)
                item["verified"] = bool((sub.get("verification") or {}).get("ok"))
                item["sub2api_account_ids"] = [
                    row.get("account_id")
                    for row in ((sub.get("data") or {}).get("items") or [])
                    if isinstance(row, dict) and row.get("account_id")
                ]
                item["import_error"] = "" if item["verified"] else str(sub.get("error") or (sub.get("verification") or {}).get("error") or "")[:300]
                if item["sub2api_account_ids"]:
                    _set_remote_schedulable(sub2api_cfg, item["sub2api_account_ids"], item["verified"])
        except Exception as exc:
            item["error"] = str(exc)[:300]
        state["results"].append(item)
        _write_json(state_path, state)
        if index + 1 < len(mailboxes) and args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    state["completed_at"] = int(time.time())
    state["summary"] = {
        "purchased": len(mailboxes),
        "registered": sum(bool(item.get("registered")) for item in state["results"]),
        "agent_identity": sum(bool(item.get("agent_identity")) for item in state["results"]),
        "local_agent_verified": sum(bool(item.get("local_agent_verified")) for item in state["results"]),
        "imported": sum(bool(item.get("imported")) for item in state["results"]),
        "verified": sum(bool(item.get("verified")) for item in state["results"]),
        "unique_exit_ips": len({item.get("exit_ip") for item in state["results"] if item.get("exit_ip")}),
    }
    _write_json(state_path, state)
    print(json.dumps({"ok": state["summary"]["verified"] == len(mailboxes), "state_path": str(state_path), **state["summary"]}, ensure_ascii=False))
    return 0 if state["summary"]["verified"] == len(mailboxes) else 3


if __name__ == "__main__":
    sys.exit(main())

from .codex_export import export_codex_session
from .cpa_import import (
    classify_cpa_auth_file,
    extract_cpa_auth_email,
)
from .import_targets import fetch_target_auth_files, import_account_session, normalize_import_target, target_label
from .mailbox import MailboxAccount, _cfworker_cfg, _fetch_mailbox_messages, _load_mailbox_pool


DEACTIVATION_SUBJECT = "access deactivated"
DEACTIVATION_BODY_MARKERS = (
    "important update",
    "chatgpt account associated with",
)


def reimport_cpa_401_survivors(
    target="cpa",
    chatai_mailbox_file="",
    export_dir="",
    refresh=True,
    proxy=None,
    timeout=300,
    api_url="",
    api_token="",
    sub2api_url="",
    sub2api_token="",
    sub2api_email="",
    sub2api_password="",
    sub2api_group="",
    sub2api_group_ids=None,
    sub2api_proxy="",
    sub2api_proxy_id=None,
    sub2api_priority=None,
    sub2api_concurrency=None,
    message_limit=100,
    include_cfworker=True,
    cfworker_domain="",
):
    target = normalize_import_target(target)
    label = target_label(target)
    auth_files = fetch_target_auth_files(
        target,
        cpa_api_url=api_url,
        cpa_api_token=api_token,
        sub2api_url=sub2api_url,
        sub2api_token=sub2api_token,
        sub2api_email=sub2api_email,
        sub2api_password=sub2api_password,
    )
    if not auth_files.get("ok"):
        return {
            "ok": False,
            "target": target,
            "error": auth_files.get("error", "fetch_auth_files_failed"),
            "source": auth_files,
        }

    mailbox_args = type("MailboxArgs", (), {"chatai_mailbox_file": chatai_mailbox_file})()
    mailbox_pool = _load_mailbox_pool(mailbox_args)
    mailbox_by_email = {
        str(getattr(mailbox, "email", "") or "").strip().lower(): mailbox
        for mailbox in mailbox_pool
        if str(getattr(mailbox, "email", "") or "").strip()
    }

    candidates, skipped, seen = [], [], set()
    for item in auth_files.get("files", []):
        email = extract_cpa_auth_email(item)
        status = classify_cpa_auth_file(item)
        if not email:
            skipped.append({"reason": "missing_email", "status": status})
            continue
        if status != "token_invalid":
            skipped.append({"email": email, "reason": "not_401", "status": status})
            continue
        if email in seen:
            continue
        seen.add(email)
        mailbox = mailbox_by_email.get(email)
        if mailbox is None and include_cfworker and _is_cfworker_email(email, cfworker_domain):
            mailbox = MailboxAccount(
                email=email,
                source=_cfworker_cfg().get("worker_url", ""),
                provider="cfworker",
            )
        if mailbox is None:
            skipped.append({"email": email, "reason": "not_in_chatai_pool", "status": status})
            continue
        candidates.append((email, mailbox))

    results = []
    for index, (email, mailbox) in enumerate(candidates, start=1):
        print(f"[{index}/{len(candidates)}] Checking {label} 401 account: {email}")
        deactivation = has_deactivation_notice(mailbox, email, limit=message_limit, proxy=proxy)
        if deactivation.get("found"):
            print(f"[SKIP] {email} has Access deactivated mail")
            results.append({
                "ok": False,
                "email": email,
                "skipped": True,
                "reason": "access_deactivated_mail_found",
                "deactivation": deactivation,
            })
            continue

        export_result = export_codex_session(
            email=email,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            require_refresh_token=True,
            force_email_otp_login=True,
        )
        if not export_result.get("ok"):
            results.append({
                "ok": False,
                "email": email,
                "stage": "export_codex_session",
                "export": export_result,
            })
            continue

        import_result = import_account_session(
            target,
            email=email,
            session_file=export_result.get("path", ""),
            export_dir=export_dir,
            refresh=False,
            proxy=proxy,
            timeout=timeout,
            cpa_api_url=api_url,
            cpa_api_token=api_token,
            sub2api_url=sub2api_url,
            sub2api_token=sub2api_token,
            sub2api_email=sub2api_email,
            sub2api_password=sub2api_password,
            sub2api_group=sub2api_group,
            sub2api_group_ids=sub2api_group_ids,
            sub2api_proxy=sub2api_proxy,
            sub2api_proxy_id=sub2api_proxy_id,
            sub2api_priority=sub2api_priority,
            sub2api_concurrency=sub2api_concurrency,
        )
        results.append({
            "ok": bool(import_result.get("ok")),
            "email": email,
            "stage": f"import_{target}",
            "export": export_result,
            "import": import_result,
        })

    success = sum(1 for result in results if result.get("ok"))
    skipped_count = sum(1 for result in results if result.get("skipped"))
    failed = len(results) - success - skipped_count
    return {
        "ok": failed == 0,
        "target": target,
        "total_401": len(candidates),
        "success": success,
        "skipped_deactivated": skipped_count,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def has_deactivation_notice(mailbox, target_email, limit=100, proxy=None):
    email = str(target_email or "").strip().lower()
    try:
        messages = _fetch_mailbox_messages(mailbox, limit=limit, proxy=proxy)
    except Exception as exc:
        return {"found": False, "error": str(exc)}

    for msg in messages:
        subject = str((msg or {}).get("subject") or "")
        body = _message_text(msg)
        text = (subject + "\n" + body).lower()
        if DEACTIVATION_SUBJECT not in subject.lower():
            continue
        if email and email not in text:
            continue
        if not all(marker in text for marker in DEACTIVATION_BODY_MARKERS):
            continue
        return {
            "found": True,
            "subject": subject,
            "receivedDateTime": str((msg or {}).get("receivedDateTime") or ""),
        }
    return {"found": False}


def _message_text(msg):
    msg = msg or {}
    body = msg.get("body")
    content = ""
    if isinstance(body, dict):
        content = str(body.get("content") or "")
    elif body:
        content = str(body)
    return "\n".join([
        str(msg.get("bodyPreview") or ""),
        content,
    ])


def _is_cfworker_email(email, cfworker_domain=""):
    domain = str(cfworker_domain or _cfworker_cfg().get("domain") or "edu.liziai.cloud").strip().lstrip("@").lower()
    return bool(domain and str(email or "").strip().lower().endswith("@" + domain))

from concurrent.futures import ThreadPoolExecutor, as_completed

from .error_classification import classify_error

def _unique_mailboxes(mailboxes):
    if not mailboxes:
        return []
    unique = []
    seen = set()
    for mailbox in mailboxes:
        email = str(getattr(mailbox, "email", "") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        unique.append(mailbox)
    return unique


def run_batch_impl(
    count=1,
    proxy=None,
    mailboxes=None,
    paypal_link=True,
    workers=4,
    phone_pool=None,
    codex_oauth=True,
    payment_method="paypal",
    paypal_generation_type=None,
    registration_mode=None,
    run_email_func=None,
):
    if run_email_func is None:
        raise ValueError("run_email_func is required")
    mailboxes = _unique_mailboxes(mailboxes)
    if mailboxes and int(count or 1) > len(mailboxes):
        print(f"[!] Requested {count} account(s), but only {len(mailboxes)} unique mailbox(es) are available; capping batch size.")
        count = len(mailboxes)
    results = []
    print(f"\n{'=' * 60}")
    print(f"  ChatGPT Email Batch Registration - {count} accounts")
    print(f"{'=' * 60}\n")

    sentinel_data = None
    if int(count or 1) > 1:
        print("[*] Batch mode: using a fresh sentinel/auth state per account.")

    def _run_one(i):
        print(f"\n{'#' * 40}")
        print(f"  Account {i + 1}/{count}")
        print(f"{'#' * 40}")
        try:
            mailbox = mailboxes[i] if mailboxes else None
            result = run_email_func(
                proxy=proxy,
                mailbox=mailbox,
                paypal_link=paypal_link,
                phone_pool=phone_pool,
                codex_oauth=codex_oauth,
                sentinel_data=None,
                payment_method=payment_method,
                paypal_generation_type=paypal_generation_type,
                registration_mode=registration_mode,
            )
            if isinstance(result, dict) and not result.get("success", False):
                result.setdefault("failure_class", classify_error(result))
                if result["failure_class"] == "network":
                    result.setdefault("dropped", False)
                elif result["failure_class"] == "account":
                    result.setdefault("dropped", True)
            return i, result
        except Exception as e:
            import traceback; traceback.print_exc()
            failure_class = classify_error(str(e))
            return i, {
                "success": False,
                "error": str(e),
                "failure_class": failure_class,
                "dropped": True if failure_class == "account" else False if failure_class == "network" else None,
            }

    workers = max(1, min(int(workers or 1), 5, int(count or 1)))
    if workers <= 1:
        for i in range(count):
            _, result = _run_one(i)
            results.append(result)
        return results

    ordered = [None] * count
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_one, i) for i in range(count)]
        for future in as_completed(futures):
            i, result = future.result()
            ordered[i] = result
    results.extend(result for result in ordered if result is not None)
    return results

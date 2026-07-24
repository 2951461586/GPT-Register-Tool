import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

from .error_classification import classify_error
from .sentinel_tokens import _extract_sentinel, _sentinel_device_id

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

    workers = max(1, min(int(workers or 1), 20, int(count or 1)))

    # ── Sentinel token pool ──────────────────────────────────────────
    # CRITICAL: Each concurrent worker MUST have its own sentinel token
    # (and thus its own oai-did / device_id).  Sharing a single sentinel
    # token across workers causes all of them to reuse the same device_id,
    # which makes OpenAI rate-limit concurrent signups with HTTP 429 on
    # the email-otp/send endpoint — the entire batch fails.
    #
    # We pre-extract one unique token per concurrent worker slot and lend
    # them via a thread-safe queue.  When a worker finishes it returns
    # its token so the next queued worker can reuse it.
    sentinel_pool = None
    pool_size = min(workers, int(count or 1))
    if pool_size > 1:
        sentinel_pool = queue.Queue()
        print(f"[*] Batch mode: pre-extracting {pool_size} unique sentinel tokens (one per worker)...")
        for idx in range(pool_size):
            try:
                token = _extract_sentinel(proxy=proxy, force_fresh=True)
            except Exception as exc:
                print(f"[!] Sentinel pre-extraction {idx + 1}/{pool_size} failed: {exc}")
                token = None
            did_preview = _sentinel_device_id(token)[:8] if token else "N/A"
            status = "ready" if token else "empty (worker will self-extract)"
            print(f"[*] Sentinel token {idx + 1}/{pool_size}: {status} (did={did_preview}...)")
            sentinel_pool.put(token)
        ready = sum(1 for t in list(sentinel_pool.queue) if t)
        print(f"[*] {ready}/{pool_size} unique sentinel token(s) ready; each worker gets its own device_id.")

    def _run_one(i):
        print(f"\n{'#' * 40}")
        print(f"  Account {i + 1}/{count}")
        print(f"{'#' * 40}")
        # Borrow a unique sentinel token from the pool (blocks until one
        # is available — another worker must finish and return theirs).
        worker_sentinel = None
        if sentinel_pool is not None:
            try:
                worker_sentinel = sentinel_pool.get(timeout=600)
            except Exception:
                print(f"  [!] Sentinel pool timeout; worker will self-extract.")
                worker_sentinel = None
        try:
            mailbox = mailboxes[i] if mailboxes else None
            result = run_email_func(
                proxy=proxy,
                mailbox=mailbox,
                paypal_link=paypal_link,
                phone_pool=phone_pool,
                codex_oauth=codex_oauth,
                sentinel_data=worker_sentinel,
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
        finally:
            # Return the sentinel token to the pool so the next worker can reuse it.
            if sentinel_pool is not None and worker_sentinel:
                sentinel_pool.put(worker_sentinel)

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

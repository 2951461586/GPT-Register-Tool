from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from .error_classification import classify_error
from .config import CFG
from .paypal_proxy import infer_proxy_country
from .phone_proxy import probe_proxy_with_scheme_detection, refresh_proxy_sid


def _registration_proxy_candidates(proxy_pool, fallback=None):
    candidates = [str(item or "").strip() for item in (proxy_pool or []) if str(item or "").strip()]
    fallback = str(fallback or "").strip()
    if fallback and fallback not in candidates:
        candidates.insert(0, fallback)
    return list(dict.fromkeys(candidates))


def select_registration_proxy_pool(proxy_pool, fallback=None):
    candidates = _registration_proxy_candidates(proxy_pool, fallback)
    if len(candidates) <= 1:
        return candidates

    def check(base: str) -> bool:
        candidate = refresh_proxy_sid(base)
        expected = infer_proxy_country(candidate)
        checked = probe_proxy_with_scheme_detection(candidate, expected, use_cache=True)
        return bool(checked.get("ok"))

    # Serial probing made batch start-up delay grow linearly with pool size;
    # probe concurrently instead. executor.map preserves candidate order and
    # the probe cache is lock-guarded for concurrent workers.
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        outcomes = list(executor.map(check, candidates))
    healthy = [base for base, ok in zip(candidates, outcomes) if ok]
    return healthy or candidates


def select_registration_proxy_base(proxy_pool, fallback=None):
    candidates = select_registration_proxy_pool(proxy_pool, fallback)
    return candidates[0] if candidates else str(fallback or "").strip()


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
    proxy_pool=None,
    mailboxes=None,
    workers=4,
    phone_pool=None,
    codex_oauth=False,
    registration_mode=None,
    max_attempts=2,
    retry_delay_seconds=1.0,
    run_email_func=None,
    browser_headless: bool | None = None,
    enroll_2fa: bool = True,
    on_result=None,
):
    if run_email_func is None:
        raise ValueError("run_email_func is required")
    mailboxes = _unique_mailboxes(mailboxes)
    proxy_pool = [str(item or "").strip() for item in (proxy_pool or []) if str(item or "").strip()]
    if proxy and str(proxy).strip() not in proxy_pool:
        proxy_pool.insert(0, str(proxy).strip())
    if not proxy_pool and proxy:
        proxy_pool = [str(proxy).strip()]
    proxy_pool = select_registration_proxy_pool(proxy_pool, proxy)
    proxy = proxy_pool[0] if proxy_pool else proxy
    if mailboxes and int(count or 1) > len(mailboxes):
        print(f"[!] Requested {count} account(s), but only {len(mailboxes)} unique mailbox(es) are available; capping batch size.")
        count = len(mailboxes)
    results = []
    print(f"\n{'=' * 60}")
    print(f"  ChatGPT Email Batch Registration - {count} accounts")
    print(f"{'=' * 60}\n")

    workers = max(1, min(int(workers or 1), 20, int(count or 1)))
    max_attempts = max(1, min(int(max_attempts or 1), 3))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds or 0.0))

    email_cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    try:
        prewarm_window = max(0, min(int(email_cfg.get("sentinel_prewarm_window") or 0), workers, count))
    except (TypeError, ValueError):
        prewarm_window = 0
    prewarm_executor = None
    prewarmed = {}
    first_attempt_proxies = {}
    if prewarm_window:
        from .sentinel_tokens import _extract_sentinel, _sentinel_max_concurrency

        prewarm_executor = ThreadPoolExecutor(max_workers=min(prewarm_window, _sentinel_max_concurrency()))
        for index in range(prewarm_window):
            base_proxy = proxy_pool[index % len(proxy_pool)] if proxy_pool else proxy
            worker_proxy = refresh_proxy_sid(base_proxy) if base_proxy else base_proxy
            first_attempt_proxies[index] = worker_proxy
            prewarmed[index] = prewarm_executor.submit(
                _extract_sentinel, proxy=worker_proxy, force_fresh=True, persist=False,
            )

    def _prewarmed_sentinel(index):
        future = prewarmed.get(index)
        if future is None:
            return None
        try:
            return future.result()
        except Exception:
            return None

    def _run_one(i):
        print(f"\n{'#' * 40}")
        print(f"  Account {i + 1}/{count}")
        print(f"{'#' * 40}")
        mailbox = mailboxes[i] if mailboxes else None
        for attempt in range(1, max_attempts + 1):
            base_proxy = proxy_pool[(i + attempt - 1) % len(proxy_pool)] if proxy_pool else proxy
            worker_proxy = (
                first_attempt_proxies[i]
                if attempt == 1 and i in first_attempt_proxies
                else (refresh_proxy_sid(base_proxy) if base_proxy else base_proxy)
            )
            sentinel_data = _prewarmed_sentinel(i) if attempt == 1 else None
            try:
                result = run_email_func(
                    proxy=worker_proxy,
                    mailbox=mailbox,
                    phone_pool=phone_pool,
                    codex_oauth=codex_oauth,
                    sentinel_data=sentinel_data,
                    registration_mode=registration_mode,
                    browser_headless=browser_headless,
                    enroll_2fa=enroll_2fa,
                )
            except Exception as e:
                import traceback; traceback.print_exc()
                failure_class = classify_error(str(e))
                result = {
                    "success": False,
                    "error": str(e),
                    "failure_class": failure_class,
                    "dropped": True if failure_class == "account" else False if failure_class in {"network", "mailbox", "auth_state"} else None,
                }
            if not isinstance(result, dict):
                result = {"success": False, "error": "invalid_registration_result", "failure_class": "unknown"}
            result["registration_attempts"] = attempt
            if result.get("success", False):
                return i, result
            result.setdefault("failure_class", classify_error(result))
            if result["failure_class"] in {"network", "mailbox", "auth_state", "rate_limit"}:
                result.setdefault("dropped", False)
            elif result["failure_class"] == "account":
                result.setdefault("dropped", True)
            if result["failure_class"] not in {"network", "auth_state"} or attempt >= max_attempts:
                return i, result
            print(
                f"[!] Retryable {result['failure_class']} failure; "
                f"retrying account {i + 1} with a fresh proxy session "
                f"({attempt + 1}/{max_attempts})"
            )
            if retry_delay_seconds:
                time.sleep(retry_delay_seconds)
        return i, result

    def _notify_result(index, result):
        if on_result is None:
            return
        try:
            on_result(index, result)
        except Exception as exc:
            print(
                f"[!] Result callback failed for account {index + 1}: "
                f"{type(exc).__name__}; batch continues."
            )

    if workers <= 1:
        for i in range(count):
            _, result = _run_one(i)
            results.append(result)
            _notify_result(i, result)
        if prewarm_executor is not None:
            prewarm_executor.shutdown(wait=True)
        return results

    ordered = [None] * count
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_one, i) for i in range(count)]
        for future in as_completed(futures):
            i, result = future.result()
            ordered[i] = result
            _notify_result(i, result)
    results.extend(result for result in ordered if result is not None)
    if prewarm_executor is not None:
        prewarm_executor.shutdown(wait=True)
    return results

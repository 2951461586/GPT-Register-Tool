import json
import threading
import time
import uuid

from curl_cffi import requests as curl_requests

from .codex_sentinel import import_cookie_header
from .auth_headers import auth_impersonate, auth_user_agent
from .config import CFG
from .paths import runtime_file

SENTINEL_CACHE_FILE = runtime_file(CFG, "sentinel_cache.json")

# Guards reads/writes of the shared sentinel cache file. Batch workers can reach
# _save_sentinel_cache concurrently (e.g. a mid-flow oauth-token refresh), so
# serialize file access to avoid interleaved writes and torn reads.
_sentinel_cache_lock = threading.Lock()
_sentinel_metrics_lock = threading.Lock()
_sentinel_metrics = {
    "requests": 0,
    "success": 0,
    "failure": 0,
    "fallbacks": 0,
    "queue_wait_ms": 0.0,
    "duration_ms": 0.0,
    "providers": {},
}
_sentinel_provider_health: dict[str, dict[str, float]] = {}

def _get_cached_sentinel(force_fresh=False):
    if force_fresh: return None
    with _sentinel_cache_lock:
        if SENTINEL_CACHE_FILE.exists():
            try:
                with open(SENTINEL_CACHE_FILE) as f: cache = json.load(f)
                age = time.time() - cache.get("ts", 0)
                ttl = int((CFG.get("timeouts") or {}).get("token_cache_ttl", 600) or 600)
                if age < ttl and cache.get("sentinel_token"):
                    print(f"[*] Using cached sentinel token (age: {age:.0f}s)")
                    return cache
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"  [!] Sentinel cache read failed: {exc}")
    return None

def _save_sentinel_cache(data):
    payload = dict(data or {})
    payload["ts"] = time.time()
    with _sentinel_cache_lock:
        tmp_path = SENTINEL_CACHE_FILE.with_name(f"{SENTINEL_CACHE_FILE.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp_path.replace(SENTINEL_CACHE_FILE)
    print(f"[*] Sentinel token cached")


def _cookie_jar_header(cookies):
    if not cookies:
        return ""
    try:
        if hasattr(cookies, "get_dict"):
            items = cookies.get_dict().items()
        else:
            items = []
            for cookie in cookies:
                if hasattr(cookie, "name") and hasattr(cookie, "value"):
                    items.append((cookie.name, cookie.value))
                elif isinstance(cookie, str):
                    try:
                        value = cookies.get(cookie)
                    except Exception:
                        value = ""
                    items.append((cookie, value))
        return "; ".join(f"{name}={value}" for name, value in items if name and value)
    except Exception:
        return ""


def _sentinel_device_id(sentinel_data):
    data = sentinel_data or {}
    did = str(data.get("oai_did") or "").strip()
    if did:
        return did
    try:
        token = json.loads(str(data.get("sentinel_token") or "{}"))
        return str(token.get("id") or "").strip()
    except Exception:
        return ""


def _set_oai_did_cookie(session, did):
    if did:
        try:
            session.cookies.set("oai-did", did, domain=".openai.com", path="/")
        except Exception:
            try:
                session.cookies.set("oai-did", did, domain="auth.openai.com", path="/")
            except Exception:
                pass


def _import_sentinel_cookies(session, sentinel_data, did):
    cookie_str = str((sentinel_data or {}).get("cookie_str") or "").strip()
    if cookie_str:
        import_cookie_header(session, cookie_str, "auth.openai.com")
    _set_oai_did_cookie(session, did)

def _solve_pow(seed, difficulty_hex):
    """Solve sentinel proof-of-work (SHA3-512). Mirrors standalone-phone-protocol."""
    import base64
    import hashlib
    import struct
    try:
        difficulty_int = int(difficulty_hex, 16)
    except (ValueError, TypeError):
        return ""
    prefix_len = (len(difficulty_hex) + 1) // 2
    for n in range(500000):
        digest = hashlib.sha3_512(f"{seed}{n}".encode()).digest()
        value = 0
        for i in range(prefix_len):
            value = (value << 8) + digest[i]
        if value <= difficulty_int:
            return base64.b64encode(struct.pack(">Q", n)).decode()
    return ""


def _sentinel_frame_version():
    try:
        from .sentinel_quickjs import sentinel_version

        return sentinel_version()
    except Exception:
        return "20260219f9f6"


def _build_sentinel_pow_token(flow_data, did, flow):
    if not flow_data or not flow_data.get("token"):
        return ""
    pow_info = flow_data.get("proofofwork", {}) if isinstance(flow_data, dict) else {}
    t = ""
    if pow_info.get("required") and pow_info.get("seed") and pow_info.get("difficulty"):
        print(f"  [*] Solving sentinel PoW flow={flow} difficulty={pow_info['difficulty']}...")
        t = _solve_pow(str(pow_info["seed"]), str(pow_info["difficulty"]))
        if not t:
            print(f"  [!] PoW solve failed for flow={flow}, proceeding without it")
    return json.dumps({
        "p": flow_data.get("p", ""),
        "t": t,
        "c": flow_data.get("token", ""),
        "id": did,
        "flow": flow,
    })


def _extract_sentinel_http(proxy=None, persist=True, device_id=None):
    """Extract sentinel tokens via direct HTTP POST (no browser needed).

    Mirrors fetchSentinelViaProtocol from standalone-phone-protocol.
    Uses curl_cffi which handles socks5h:// properly with remote DNS.

    ``persist`` controls whether the freshly-extracted token is written to the
    shared cache. Mid-flow per-account refreshes must pass ``persist=False`` so a
    token carrying a new device_id does not clobber the cache another concurrent
    worker is relying on.
    """
    did = str(device_id or uuid.uuid4())
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    flows = ["username_password_create", "oauth_create_account"]
    results = {}

    for flow in flows:
        try:
            resp = session.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                data=json.dumps({"p": "", "id": did, "flow": flow}),
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "*/*",
                    "Origin": "https://sentinel.openai.com",
                    "Referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={_sentinel_frame_version()}",
                    "User-Agent": auth_user_agent(),
                },
                timeout=30,
                impersonate=auth_impersonate(),
            )
            data = resp.json()
            results[flow] = data
        except Exception as e:
            print(f"  [!] HTTP sentinel fetch failed for flow={flow}: {e}")
            return None

    upc = results.get("username_password_create", {})
    oauth = results.get("oauth_create_account", {})

    if not upc.get("token"):
        print("  [!] HTTP sentinel: no token in username_password_create response")
        return None

    # Build sentinel_token (same structure as browser path)
    sentinel_token = _build_sentinel_pow_token(upc, did, "username_password_create")
    sentinel_oauth_token = _build_sentinel_pow_token(oauth, did, "oauth_create_account")

    # Build sentinel_so_token
    sentinel_so_obj = {
        "so": oauth.get("so", oauth.get("token", "")),
        "c": oauth.get("token", ""),
        "id": did,
        "flow": "oauth_create_account",
    }
    sentinel_so_token = json.dumps(sentinel_so_obj)

    # Get auth cookies via HTTP (prime the session)
    try:
        auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
        session.cookies.set("oai-did", did, domain=".openai.com", path="/")
        prime_resp = session.get(
            f"{auth_base}/create-account",
            headers={
                "User-Agent": auth_user_agent(),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=30,
            impersonate=auth_impersonate(),
        )
        # Extract cookies from the session
        cookie_str = _cookie_jar_header(session.cookies)
        # Ensure oai-did cookie is set
        session.cookies.set("oai-did", did, domain=".openai.com")
        cookie_str = "; ".join(item for item in (f"oai-did={did}", cookie_str) if item)
    except Exception as e:
        print(f"  [!] Auth prime request failed: {e}")
        cookie_str = f"oai-did={did}"

    result = {
        "sentinel_token": sentinel_token,
        "sentinel_oauth_token": sentinel_oauth_token or sentinel_token,
        "sentinel_so_token": sentinel_so_token,
        "cookie_str": cookie_str,
        "oai_did": did,
    }
    if persist:
        _save_sentinel_cache(result)
    return result


def _sentinel_mode():
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    raw = str(
        cfg.get("sentinel_mode")
        or cfg.get("sentinel_provider")
        or CFG.get("sentinel_mode")
        or "auto"
    ).strip().lower()
    if raw in {"quickjs", "js", "sdk"}:
        return "quickjs"
    if raw in {"http", "pow", "python", "legacy"}:
        return "http"
    if raw in {"browser", "playwright"}:
        return "browser"
    return "auto"


def _quickjs_enabled():
    return _sentinel_mode() in {"auto", "quickjs"}


def _sentinel_max_concurrency():
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    try:
        value = int(cfg.get("sentinel_max_concurrency", 2) or 2)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 4))


def _sentinel_circuit_config() -> tuple[int, int]:
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    try:
        failures = max(1, min(int(cfg.get("sentinel_circuit_failures") or 3), 20))
    except (TypeError, ValueError):
        failures = 3
    try:
        cooldown = max(5, min(int(cfg.get("sentinel_circuit_cooldown_seconds") or 60), 900))
    except (TypeError, ValueError):
        cooldown = 60
    return failures, cooldown


def _provider_available(provider: str, *, explicit: bool = False) -> bool:
    if explicit:
        return True
    with _sentinel_metrics_lock:
        state = _sentinel_provider_health.get(provider) or {}
        return float(state.get("cooldown_until") or 0.0) <= time.time()


def _record_provider(provider: str, ok: bool, duration_ms: float) -> None:
    failure_limit, cooldown = _sentinel_circuit_config()
    with _sentinel_metrics_lock:
        providers = _sentinel_metrics.setdefault("providers", {})
        metrics = providers.setdefault(provider, {"attempts": 0, "success": 0, "failure": 0, "duration_ms": 0.0})
        metrics["attempts"] += 1
        metrics["success" if ok else "failure"] += 1
        metrics["duration_ms"] += round(duration_ms, 3)
        health = _sentinel_provider_health.setdefault(provider, {"consecutive_failures": 0.0, "cooldown_until": 0.0})
        if ok:
            health["consecutive_failures"] = 0.0
            health["cooldown_until"] = 0.0
        else:
            health["consecutive_failures"] = float(health.get("consecutive_failures") or 0.0) + 1.0
            if health["consecutive_failures"] >= failure_limit:
                health["cooldown_until"] = time.time() + cooldown


def sentinel_metrics_snapshot(reset: bool = False) -> dict:
    """Return aggregate, token-free extraction performance metrics."""
    with _sentinel_metrics_lock:
        snapshot = json.loads(json.dumps(_sentinel_metrics))
        snapshot["circuits"] = {
            provider: {
                "consecutive_failures": int(state.get("consecutive_failures") or 0),
                "cooldown_remaining_seconds": max(0, int(float(state.get("cooldown_until") or 0) - time.time())),
            }
            for provider, state in _sentinel_provider_health.items()
        }
        if reset:
            _sentinel_metrics.update({
                "requests": 0, "success": 0, "failure": 0, "fallbacks": 0,
                "queue_wait_ms": 0.0, "duration_ms": 0.0, "providers": {},
            })
        return snapshot


def _extract_sentinel_quickjs(proxy=None, persist=True, device_id=None):
    """Extract Sentinel tokens by running the real Sentinel SDK through Node.

    This is intentionally an optional enhancement: if Node/SDK execution fails,
    callers in ``auto`` mode can still fall back to the existing HTTP PoW or
    browser extraction paths.
    """
    if not _quickjs_enabled():
        return None
    did = str(device_id or uuid.uuid4())
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        from .sentinel_quickjs import get_sentinel_token_via_quickjs
    except Exception as exc:
        print(f"  [!] Sentinel QuickJS unavailable: {exc}")
        return None

    tokens = {}
    for flow in ("username_password_create", "oauth_create_account"):
        token = get_sentinel_token_via_quickjs(
            session,
            device_id=did,
            flow=flow,
            log=lambda message: print(f"  {message}"),
        )
        if not token:
            return None
        tokens[flow] = token

    try:
        oauth_payload = json.loads(tokens.get("oauth_create_account") or "{}")
    except Exception:
        oauth_payload = {}
    sentinel_so_obj = {
        "so": oauth_payload.get("so") or oauth_payload.get("c") or "",
        "c": oauth_payload.get("c") or "",
        "id": did,
        "flow": "oauth_create_account",
    }

    try:
        auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
        session.cookies.set("oai-did", did, domain=".openai.com", path="/")
        session.get(
            f"{auth_base}/create-account",
            headers={
                "User-Agent": auth_user_agent(),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=30,
            impersonate=auth_impersonate(),
        )
        cookie_str = _cookie_jar_header(session.cookies)
        cookie_str = "; ".join(item for item in (f"oai-did={did}", cookie_str) if item)
    except Exception as exc:
        print(f"  [!] Auth prime request failed after QuickJS sentinel: {exc}")
        cookie_str = f"oai-did={did}"

    result = {
        "sentinel_token": tokens["username_password_create"],
        "sentinel_oauth_token": tokens["oauth_create_account"],
        "sentinel_so_token": json.dumps(sentinel_so_obj, separators=(",", ":"), ensure_ascii=False),
        "cookie_str": cookie_str,
        "oai_did": did,
        "sentinel_source": "quickjs",
    }
    if persist:
        _save_sentinel_cache(result)
    return result


# Fresh account registrations may extract Sentinel data concurrently, but the
# gate remains deliberately small to avoid overwhelming sentinel.openai.com.
_sentinel_extraction_gate = threading.BoundedSemaphore(_sentinel_max_concurrency())
_sentinel_cache_fill_lock = threading.Lock()


def _extract_sentinel_uncached(proxy=None, persist=True):
    mode = _sentinel_mode()
    providers = [mode] if mode != "auto" else ["quickjs", "http", "browser"]
    attempted = 0
    for provider in providers:
        if not _provider_available(provider, explicit=mode != "auto"):
            continue
        if attempted:
            with _sentinel_metrics_lock:
                _sentinel_metrics["fallbacks"] += 1
        attempted += 1
        started = time.perf_counter()
        if provider == "quickjs":
            print("[*] Extracting sentinel tokens via QuickJS SDK...")
            result = _extract_sentinel_quickjs(proxy, persist=persist)
        elif provider == "http":
            print("[*] Extracting sentinel tokens via HTTP protocol...")
            result = _extract_sentinel_http(proxy, persist=persist)
        else:
            print("[*] Falling back to browser Sentinel extraction...")
            browser_proxy = proxy.replace("socks5h://", "socks5://") if proxy and proxy.startswith("socks5h://") else proxy
            result = _extract_sentinel_cloakbrowser(browser_proxy, persist=persist)
        duration_ms = (time.perf_counter() - started) * 1000
        _record_provider(provider, bool(result), duration_ms)
        if result:
            result.setdefault("sentinel_source", provider)
            return result
    if mode != "auto":
        print(f"[!] Sentinel {mode} mode failed and fallback is disabled")
    return None


def _extract_sentinel(proxy=None, force_fresh=False, persist=True):
    cached = _get_cached_sentinel(force_fresh=force_fresh)
    if cached:
        return cached

    if force_fresh:
        queued = time.perf_counter()
        _sentinel_extraction_gate.acquire()
        queue_ms = (time.perf_counter() - queued) * 1000
        started = time.perf_counter()
        try:
            result = _extract_sentinel_uncached(proxy, persist=persist)
        finally:
            _sentinel_extraction_gate.release()
        duration_ms = (time.perf_counter() - started) * 1000
        with _sentinel_metrics_lock:
            _sentinel_metrics["requests"] += 1
            _sentinel_metrics["success" if result else "failure"] += 1
            _sentinel_metrics["queue_wait_ms"] += round(queue_ms, 3)
            _sentinel_metrics["duration_ms"] += round(duration_ms, 3)
        return result

    # Preserve single-flight cache population for callers that allow reuse.
    with _sentinel_cache_fill_lock:
        cached = _get_cached_sentinel(force_fresh=force_fresh)
        if cached:
            return cached
        with _sentinel_extraction_gate:
            return _extract_sentinel_uncached(proxy, persist=persist)


def _extract_sentinel_cloakbrowser(browser_proxy, persist=True):
    """Extract sentinel tokens using CloakBrowser."""
    try:
        from cloakbrowser import launch
    except ImportError:
        print("[Error] pip install cloakbrowser")
        return None

    browser = launch(headless=True, humanize=True, proxy=browser_proxy)
    ctx = browser.new_context(
        user_agent=auth_user_agent(),
        viewport={"width": 1280, "height": 800}, locale="en-US", timezone_id="America/New_York")
    page = ctx.new_page()

    # Use create-account page (lighter, fewer redirects)
    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    page_url = f"{auth_base}/create-account"

    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=120000)
    except Exception as e:
        err_msg = str(e)
        if "ERR_PROXY" in err_msg or "ERR_TUNNEL" in err_msg or "ERR_CONNECTION" in err_msg:
            print(f"  [Error] Proxy connection failed: {browser_proxy}")
            print(f"  [Error] Please check if your proxy (Clash/V2Ray etc.) is running on the correct port.")
            browser.close(); return None
        try: page.goto(page_url, wait_until="commit", timeout=120000)
        except Exception as e2:
            print(f"  [Error] Page navigation failed: {e2}"); browser.close(); return None

    if "error" in page.url:
        print(f"  [Error] Auth page returned error: {page.url[:200]}")
        browser.close(); return None

    # Wait for Cloudflare challenge to resolve (title changes from "Just a moment..." or empty)
    cf_deadline = time.time() + 180
    cf_waited = 0
    while time.time() < cf_deadline:
        try:
            title = page.title()
        except Exception:
            time.sleep(1); continue
        if title and "just a moment" not in title.lower():
            if cf_waited > 5:
                print(f"  Cloudflare challenge resolved after {cf_waited}s")
            break
        if cf_waited > 0 and cf_waited % 30 == 0:
            print(f"  Waiting for Cloudflare challenge... ({cf_waited}s)")
        cf_waited += 1
        time.sleep(1)
    else:
        print("  [Error] Cloudflare challenge did not resolve in 180s")
        browser.close(); return None

    # Now wait for SentinelSDK to load (CF challenge can take 10s to 2+ minutes)
    # Use page.evaluate() instead of wait_for_function to avoid CSP unsafe-eval violations
    sdk_deadline = time.time() + 180
    sdk_loaded = False
    while time.time() < sdk_deadline:
        try:
            if page.evaluate("() => typeof window.SentinelSDK !== 'undefined'"):
                sdk_loaded = True; break
        except Exception:
            pass
        time.sleep(1)
    if not sdk_loaded:
        print("  SentinelSDK not loaded after 180s! Check proxy connectivity to auth.openai.com")
        browser.close(); return None
    print("  SentinelSDK loaded")

    result = _collect_sentinel_tokens(page, ctx, persist=persist)
    browser.close()
    return result


def _collect_sentinel_tokens(page, ctx, persist=True):
    """Call SentinelSDK.init() and extract tokens from the loaded page."""
    page.evaluate("() => SentinelSDK.init()"); time.sleep(0.5)
    did = page.evaluate("() => document.cookie.match(/oai-did=([^;]+)/)?.[1] || ''")

    sentinel_token = page.evaluate(f"""(did) => {{
        return SentinelSDK.token().then(raw => {{
            const parsed = JSON.parse(raw);
            parsed.id = did;
            parsed.flow = 'username_password_create';
            return JSON.stringify(parsed);
        }});
    }}""", did)

    sentinel_so = page.evaluate(f"""(did) => {{
        return SentinelSDK.token().then(raw => {{
            const parsed = JSON.parse(raw);
            return JSON.stringify({{
                so: raw, c: parsed.c, id: did, flow: 'oauth_create_account'
            }});
        }});
    }}""", did)

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())

    result = {
        "sentinel_token": sentinel_token,
        "sentinel_so_token": sentinel_so,
        "cookie_str": cookie_str,
        "oai_did": did,
    }
    if persist:
        _save_sentinel_cache(result)
    return result

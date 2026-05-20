import json
from pathlib import Path

from .config import CFG
from .paths import runtime_file


def load_cached_sentinel():
    path = runtime_file(CFG, "sentinel_cache.json")
    try:
        if not Path(path).exists():
            return {}
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def import_cookie_header(session, cookie_header, domain):
    for item in str(cookie_header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        try:
            session.cookies.set(name, value, domain=domain, path="/")
        except Exception:
            pass


def import_cached_auth_cookies(session):
    sentinel = load_cached_sentinel()
    # Keep Cloudflare/auth cookies, but never reuse a global oai-did across accounts.
    import_cookie_header(session, strip_cookie_names(sentinel.get("cookie_str", ""), {"oai-did"}), "auth.openai.com")
    return sentinel


def with_sentinel(headers, sentinel=None):
    merged = dict(headers or {})
    attach_sentinel(merged, sentinel if sentinel is not None else load_cached_sentinel())
    return merged


def attach_sentinel(headers, sentinel):
    token = str((sentinel or {}).get("sentinel_token") or "").strip()
    if token:
        headers["openai-sentinel-token"] = token
    so_token = str((sentinel or {}).get("sentinel_so_token") or "").strip()
    if so_token:
        headers.setdefault("openai-sentinel-so-token", so_token)


def strip_cookie_names(cookie_header, names):
    blocked = {str(name or "").strip().lower() for name in (names or []) if str(name or "").strip()}
    kept = []
    for item in str(cookie_header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        if name.strip().lower() in blocked:
            continue
        kept.append(f"{name.strip()}={value.strip()}")
    return "; ".join(kept)

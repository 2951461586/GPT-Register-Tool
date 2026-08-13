import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from curl_cffi import requests as curl_requests

from .codex_export import build_codex_json
from .config import CFG
from .paths import output_dir
from .phone_proxy import normalize_proxy_url


REGISTER_URL = "https://auth.openai.com/api/accounts/v1/agent/register"
TASK_REGISTER_URL = "https://auth.openai.com/api/accounts/v1/agent/{runtime_id}/task/register"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"


def create_agent_identity(source_data, proxy="", timeout=30):
    token_data, warnings = build_codex_json(source_data if isinstance(source_data, dict) else {})
    access_token = _normalize_access_token(token_data.get("access_token"))
    if not access_token:
        return {"ok": False, "error": "missing_access_token"}

    account_id = str(token_data.get("chatgpt_account_id") or token_data.get("account_id") or "").strip()
    user_id = _chatgpt_user_id(access_token)
    if not account_id:
        return {"ok": False, "error": "missing_chatgpt_account_id"}
    if not user_id:
        return {"ok": False, "error": "missing_chatgpt_user_id"}
    if _token_expired(access_token):
        return {"ok": False, "error": "access_token_expired"}

    private_key = Ed25519PrivateKey.generate()
    private_der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")

    request_body = {
        "abom": {
            "agent_version": AGENT_VERSION,
            "agent_harness_id": AGENT_HARNESS_ID,
            "running_location": RUNNING_LOCATION,
        },
        "agent_public_key": public_key,
    }
    request_kwargs = {
        "json": request_body,
        "headers": {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        "timeout": max(1, int(timeout or 30)),
    }
    resolved_proxy = _normalize_proxy(proxy)
    session = _create_agent_registration_session(resolved_proxy)
    try:
        response = session.post(REGISTER_URL, **request_kwargs)
    except Exception:
        return {"ok": False, "error": "agent_registration_request_failed"}
    finally:
        try:
            session.close()
        except Exception:
            pass
    if response.status_code < 200 or response.status_code >= 300:
        return {
            "ok": False,
            "error": f"agent_registration_http_{response.status_code}",
            "status_code": response.status_code,
            "message": _safe_response_message(response),
        }
    try:
        response_data = response.json()
    except Exception:
        return {"ok": False, "error": "agent_registration_invalid_json", "status_code": response.status_code}
    runtime_id = str((response_data or {}).get("agent_runtime_id") or "").strip()
    if not runtime_id:
        return {"ok": False, "error": "agent_registration_missing_runtime_id", "status_code": response.status_code}

    auth_json = {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": runtime_id,
            "agent_private_key": base64.b64encode(private_der).decode("ascii"),
            "account_id": account_id,
            "chatgpt_user_id": user_id,
            "email": str(token_data.get("email") or "").strip(),
            "plan_type": str(token_data.get("plan_type") or "free").strip() or "free",
            "chatgpt_account_is_fedramp": False,
        },
    }
    validation = validate_agent_identity(auth_json)
    if not validation.get("ok"):
        return validation
    normalized = validation["data"]
    return {
        "ok": True,
        "data": normalized,
        "email": normalized["agent_identity"]["email"],
        "plan_type": normalized["agent_identity"]["plan_type"],
        "warnings": warnings,
    }


def provision_agent_identity(source_data, export_dir="", proxy="", timeout=30, reuse_existing=True):
    token_data, warnings = build_codex_json(source_data if isinstance(source_data, dict) else {})
    email = str(token_data.get("email") or "").strip().lower()
    plan_type = str(token_data.get("plan_type") or "").strip().lower()
    if plan_type and plan_type != "free":
        return {"ok": False, "error": "agent_identity_requires_free_plan", "plan_type": plan_type}

    if reuse_existing and email:
        existing = load_agent_identity(email, export_dir=export_dir)
        if existing.get("ok"):
            data = existing["data"]
            identity = data["agent_identity"]
            return {
                "ok": True,
                "data": data,
                "path": str(agent_identity_path(email, export_dir)),
                "email": email,
                "plan_type": str(identity.get("plan_type") or plan_type or "free"),
                "reused": True,
                "warnings": warnings,
            }

    created = create_agent_identity(source_data, proxy=proxy, timeout=timeout)
    if not created.get("ok"):
        return created
    persisted = write_agent_identity(created["data"], export_dir=export_dir)
    if not persisted.get("ok"):
        return persisted
    identity = persisted["data"]["agent_identity"]
    return {
        "ok": True,
        "data": persisted["data"],
        "path": persisted["path"],
        "email": str(identity.get("email") or email).strip().lower(),
        "plan_type": str(identity.get("plan_type") or plan_type or "free"),
        "reused": False,
        "warnings": created.get("warnings", warnings),
    }


def rebuild_agent_identity(email="", source_data=None, export_dir="", proxy="", timeout=30):
    data = dict(source_data) if isinstance(source_data, dict) else {}
    json_path = str(data.get("json_path") or "").strip()
    target_email = str(email or data.get("email") or "").strip().lower()
    if not data and target_email:
        from .session_refresh import _load_seed_session

        data, json_path = _load_seed_session(email=target_email)
    if not target_email:
        target_email = str(data.get("email") or "").strip().lower()
    if not target_email:
        return {"ok": False, "error": "missing_email"}

    attempts = []
    refresh_errors = []
    seen = set()

    def try_candidate(token_source, candidate):
        token_data, _ = build_codex_json(candidate if isinstance(candidate, dict) else {})
        token = _normalize_access_token(token_data.get("access_token"))
        if not token or token in seen:
            return None
        seen.add(token)
        created = create_agent_identity(candidate, proxy=proxy, timeout=timeout)
        attempts.append({
            "source": token_source,
            "ok": bool(created.get("ok")),
            **({"error": str(created.get("error") or "agent_registration_failed")} if not created.get("ok") else {}),
        })
        if not created.get("ok"):
            return None
        persisted = write_agent_identity(created["data"], export_dir=export_dir)
        if not persisted.get("ok"):
            return persisted
        identity = persisted["data"]["agent_identity"]
        return {
            "ok": True,
            "data": persisted["data"],
            "path": persisted["path"],
            "email": str(identity.get("email") or target_email).strip().lower(),
            "plan_type": str(identity.get("plan_type") or "free"),
            "reused": False,
            "rebuilt": True,
            "token_source": token_source,
            "attempts": attempts,
            "refresh_errors": refresh_errors,
        }

    codex_source, error = _refresh_agent_identity_codex_rt(data, json_path=json_path, proxy=proxy)
    if error:
        refresh_errors.append({"source": "codex_rt", "error": error})
    if codex_source:
        result = try_candidate("codex_rt", codex_source)
        if result:
            return result

    session_source, session_path, error = _refresh_agent_identity_chatgpt_session(
        data,
        target_email,
        json_path=json_path,
        proxy=proxy,
        timeout=timeout,
    )
    if session_path:
        json_path = session_path
    if error:
        refresh_errors.append({"source": "chatgpt_session", "error": error})
    if session_source:
        result = try_candidate("chatgpt_session", session_source)
        if result:
            return result

    result = try_candidate("stored_at", data)
    if result:
        return result
    return {
        "ok": False,
        "email": target_email,
        "error": "agent_identity_rebuild_failed",
        "attempts": attempts,
        "refresh_errors": refresh_errors,
    }


def _refresh_agent_identity_codex_rt(data, json_path="", proxy=""):
    source = dict(data or {})
    auth_session = source.get("auth_session") if isinstance(source.get("auth_session"), dict) else {}
    try:
        from .codex_export import _openai_refresh_token, _refresh_with_openai_oauth

        refresh_token = _openai_refresh_token(source, auth_session)
        if not refresh_token:
            return None, ""
        refreshed = _refresh_with_openai_oauth(source, refresh_token, proxy=proxy)
        if not refreshed.get("ok"):
            return None, str(refreshed.get("error") or "oauth_refresh_failed")
        refreshed_source = {**source, **refreshed["data"]}
        refreshed_source["refresh_token_status"] = "oauth_present"
        _persist_agent_identity_source(refreshed_source, json_path)
        return refreshed_source, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _refresh_agent_identity_chatgpt_session(data, email, json_path="", proxy="", timeout=30):
    source = dict(data or {})
    if not _has_chatgpt_session(source):
        return None, json_path, ""
    try:
        from .session_refresh import _load_seed_session, refresh_session

        refreshed = refresh_session(
            email=email,
            session_file=json_path if json_path and Path(json_path).is_file() else "",
            timeout=max(30, int(timeout or 30)),
            proxy=proxy,
        )
        if not refreshed.get("ok"):
            return None, json_path, str(refreshed.get("error") or "session_refresh_failed")
        refreshed_source, refreshed_path = _load_seed_session(
            email=email,
            session_file=str(refreshed.get("json_path") or json_path or ""),
        )
        return refreshed_source, refreshed_path or json_path, ""
    except Exception as exc:
        return None, json_path, f"{type(exc).__name__}: {exc}"


def _persist_agent_identity_source(data, json_path=""):
    from .session_refresh import _save_refreshed

    return _save_refreshed(dict(data or {}), json_path)


def _has_chatgpt_session(data):
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    return bool(
        str(data.get("session_token") or "").strip()
        or str(auth_session.get("sessionToken") or auth_session.get("session_token") or "").strip()
        or "__Secure-next-auth.session-token=" in str(data.get("cookie_header") or "")
    )


def register_agent_identity_task(auth_json, proxy="", timeout=30, disposable_canary=False):
    if not disposable_canary:
        return {"ok": False, "error": "agent_task_registration_requires_disposable_canary"}
    validation = validate_agent_identity(auth_json)
    if not validation.get("ok"):
        return validation
    identity = validation["data"]["agent_identity"]
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    try:
        der = base64.b64decode(str(identity.get("agent_private_key") or "").strip(), validate=True)
        private_key = serialization.load_der_private_key(der, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("not_ed25519")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        signature = base64.b64encode(
            private_key.sign(f"{runtime_id}:{timestamp}".encode("utf-8"))
        ).decode("ascii")
    except Exception:
        return {"ok": False, "error": "invalid_agent_identity_private_key"}

    request_kwargs = {
        "json": {"timestamp": timestamp, "signature": signature},
        "headers": {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        "timeout": max(1, int(timeout or 30)),
        "impersonate": "chrome110",
    }
    resolved_proxy = _normalize_proxy(proxy)
    if resolved_proxy:
        request_kwargs["proxies"] = {"http": resolved_proxy, "https": resolved_proxy}
    try:
        response = curl_requests.post(TASK_REGISTER_URL.format(runtime_id=runtime_id), **request_kwargs)
    except Exception:
        return {"ok": False, "error": "agent_task_registration_request_failed"}
    if response.status_code < 200 or response.status_code >= 300:
        return {
            "ok": False,
            "error": f"agent_task_registration_http_{response.status_code}",
            "status_code": response.status_code,
            "message": _safe_response_message(response),
        }
    try:
        response_data = response.json()
    except Exception:
        return {"ok": False, "error": "agent_task_registration_invalid_json", "status_code": response.status_code}
    task_id = str((response_data or {}).get("task_id") or (response_data or {}).get("taskId") or "").strip()
    encrypted = str(
        (response_data or {}).get("encrypted_task_id")
        or (response_data or {}).get("encryptedTaskId")
        or ""
    ).strip()
    if not task_id and encrypted:
        try:
            from nacl.public import SealedBox
            from nacl.signing import SigningKey

            seed = private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            task_id = SealedBox(SigningKey(seed).to_curve25519_private_key()).decrypt(
                base64.b64decode(encrypted, validate=True)
            ).decode("utf-8").strip()
        except Exception:
            return {"ok": False, "error": "agent_task_decryption_failed"}
    if not task_id:
        return {"ok": False, "error": "agent_task_registration_missing_task_id"}
    return {"ok": True, "task_id": task_id}


def build_agent_identity_authorization(auth_json, timestamp=""):
    raw_identity = _identity_dict(auth_json)
    validation = validate_agent_identity(auth_json)
    if not validation.get("ok"):
        return validation
    identity = validation["data"]["agent_identity"]
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    task_id = str(raw_identity.get("task_id") or raw_identity.get("taskId") or "").strip()
    if not task_id:
        return {"ok": False, "error": "agent_identity_missing_task_id"}
    try:
        der = base64.b64decode(str(identity.get("agent_private_key") or "").strip(), validate=True)
        private_key = serialization.load_der_private_key(der, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("not_ed25519")
        timestamp = str(timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        signature = base64.b64encode(
            private_key.sign(f"{runtime_id}:{task_id}:{timestamp}".encode("utf-8"))
        ).decode("ascii")
        envelope = {
            "agent_runtime_id": runtime_id,
            "signature": signature,
            "task_id": task_id,
            "timestamp": timestamp,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii").rstrip("=")
    except Exception:
        return {"ok": False, "error": "invalid_agent_identity_private_key"}
    return {"ok": True, "authorization": f"AgentAssertion {encoded}", "timestamp": timestamp}


def probe_agent_identity(auth_json, proxy="", timeout=90, model="gpt-5.5", disposable_canary=False):
    if not disposable_canary:
        return {"ok": False, "error": "agent_identity_probe_requires_disposable_canary"}
    validation = validate_agent_identity(auth_json)
    if not validation.get("ok"):
        return validation
    task = register_agent_identity_task(
        validation["data"],
        proxy=proxy,
        timeout=timeout,
        disposable_canary=True,
    )
    if not task.get("ok"):
        return task
    canary_data = validation["data"]
    canary_data["agent_identity"]["task_id"] = task["task_id"]
    assertion = build_agent_identity_authorization(canary_data)
    if not assertion.get("ok"):
        return assertion
    identity = validation["data"]["agent_identity"]
    headers = {
        "Authorization": assertion["authorization"],
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "ChatGPT-Account-ID": str(identity.get("account_id") or "").strip(),
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.144.1",
        "x-client-request-id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
    }
    body = {
        "model": str(model or "gpt-5.5").strip() or "gpt-5.5",
        "instructions": "Reply with exactly OK.",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Reply with exactly OK."}],
        }],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low", "summary": "auto"},
        "store": False,
        "stream": True,
        "include": [],
    }
    request_kwargs = {
        "headers": headers,
        "json": body,
        "timeout": max(1, int(timeout or 90)),
        "impersonate": "chrome110",
    }
    resolved_proxy = _normalize_proxy(proxy)
    if resolved_proxy:
        request_kwargs["proxies"] = {"http": resolved_proxy, "https": resolved_proxy}
    try:
        response = curl_requests.post(RESPONSES_URL, **request_kwargs)
    except Exception:
        return {"ok": False, "error": "agent_identity_probe_request_failed"}
    if response.status_code < 200 or response.status_code >= 300:
        return {
            "ok": False,
            "error": f"agent_identity_probe_http_{response.status_code}",
            "status_code": response.status_code,
            "message": _safe_response_message(response),
        }
    event_types = []
    failure = ""
    for raw_line in str(response.text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if event_type:
            event_types.append(event_type)
        if event_type in {"error", "response.failed"}:
            failure = str(event.get("error") or (event.get("response") or {}).get("error") or "")[:300]
    completed = "response.completed" in event_types
    return {
        "ok": completed,
        "status_code": response.status_code,
        "event": "response.completed" if completed else "",
        "consumed": True,
        **({"error": "agent_identity_probe_failed", "message": failure} if not completed else {}),
    }


def _agent_runtime_deleted(result):
    text = " ".join([
        str((result or {}).get("error") or ""),
        str((result or {}).get("message") or ""),
    ]).lower()
    return "deleted_agent_runtime_id" in text


def validate_agent_identity(value):
    identity = _identity_dict(value)
    if not identity:
        return {"ok": False, "error": "invalid_agent_identity"}
    missing = [
        key for key in ("agent_runtime_id", "agent_private_key", "account_id", "chatgpt_user_id")
        if not str(identity.get(key) or "").strip()
    ]
    if missing:
        return {"ok": False, "error": "agent_identity_missing_fields", "missing": missing}
    try:
        der = base64.b64decode(str(identity["agent_private_key"]).strip(), validate=True)
        key = serialization.load_der_private_key(der, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not_ed25519")
    except Exception:
        return {"ok": False, "error": "invalid_agent_identity_private_key"}
    normalized = dict(identity)
    for key in ("task_id", "taskId", "encrypted_task_id", "encryptedTaskId"):
        normalized.pop(key, None)
    return {"ok": True, "data": {"auth_mode": "agent_identity", "agent_identity": normalized}}


def load_agent_identity(email, export_dir=""):
    path = agent_identity_path(email, export_dir)
    if not path.exists():
        return {"ok": False, "error": "agent_identity_not_found", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "error": "read_agent_identity_failed", "message": str(exc), "path": str(path)}
    result = validate_agent_identity(data)
    if result.get("ok") and result.get("data") != data:
        migrated = write_agent_identity(result["data"], export_dir=export_dir)
        if not migrated.get("ok"):
            return migrated
        result = {"ok": True, "data": migrated["data"]}
    result["path"] = str(path)
    return result


def write_agent_identity(auth_json, export_dir=""):
    validation = validate_agent_identity(auth_json)
    if not validation.get("ok"):
        return validation
    data = validation["data"]
    email = str(data["agent_identity"].get("email") or "").strip()
    if not email:
        return {"ok": False, "error": "agent_identity_missing_email"}
    path = agent_identity_path(email, export_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"ok": True, "path": str(path), "data": data}


def agent_identity_path(email, export_dir=""):
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "agent_identities"
    safe_email = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", str(email or "unknown").strip())
    return directory / f"agent-{safe_email}.json"


def is_agent_identity(value):
    return bool(_identity_dict(value))


def _identity_dict(value):
    if not isinstance(value, dict):
        return {}
    nested = value.get("agent_identity")
    if isinstance(nested, dict):
        return nested
    if str(value.get("auth_mode") or "").strip().lower().replace("_", "") == "agentidentity":
        return value
    return {}


def _normalize_access_token(value):
    token = str(value or "").strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token if token.count(".") == 2 else ""


def _jwt_claims(access_token):
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _chatgpt_user_id(access_token):
    claims = _jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    return str(auth.get("chatgpt_user_id") or auth.get("user_id") or claims.get("sub") or "").strip()


def _token_expired(access_token):
    expires_at = _jwt_claims(access_token).get("exp")
    try:
        return float(expires_at) <= time.time()
    except (TypeError, ValueError):
        return False


def _normalize_proxy(value):
    return normalize_proxy_url(value)


def _create_agent_registration_session(proxy=""):
    session = curl_requests.Session(impersonate="safari18_0")
    session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy} if proxy else {"http": "", "https": ""}
    return session


def _safe_response_message(response):
    try:
        payload = response.json()
    except Exception:
        return "agent registration rejected"
    if not isinstance(payload, dict):
        return "agent registration rejected"
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code") or error.get("type")
    return str(payload.get("message") or error or "agent registration rejected")[:300]

"""Auth-session diagnostics for protocol registration flows.

This module keeps the diagnostic seam small: callers ask for a redacted
``client_auth_session_dump`` summary and do not need to know which fields are
sensitive or how the nested auth-state payload is shaped.
"""

import json

from .auth_headers import auth_impersonate
from .http_client import request_with_retry
from .http_utils import _json_or_raw


def _redact_auth_dump_value(value):
    if isinstance(value, str):
        text = value.strip()
        return f"[REDACTED](len={len(text)})" if text else ""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return f"<list:{len(value)}>"
    if isinstance(value, dict):
        return f"<dict:{len(value)}>"
    return f"<{type(value).__name__}>"


def _find_auth_dump_keys(data, wanted):
    found = {}
    wanted_lc = {str(key).lower() for key in wanted}

    def walk(node, path="", depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_s = str(key)
                key_lc = key_s.lower()
                next_path = f"{path}.{key_s}" if path else key_s
                if key_lc in wanted_lc or any(part in key_lc for part in ("verifier", "session", "challenge")):
                    found.setdefault(next_path, _redact_auth_dump_value(value))
                if isinstance(value, (dict, list)):
                    walk(value, next_path, depth + 1)
        elif isinstance(node, list):
            for idx, item in enumerate(node[:10]):
                walk(item, f"{path}[{idx}]", depth + 1)

    walk(data)
    return found


def auth_dump_summary(data):
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    client_auth_session = data.get("client_auth_session") or data.get("clientAuthSession") or {}
    return {
        "top_keys": sorted(str(k) for k in list(data.keys())[:20]),
        "client_auth_session_keys": sorted(str(k) for k in list(client_auth_session.keys())[:20]) if isinstance(client_auth_session, dict) else [],
        "signals": _find_auth_dump_keys(
            data,
            {
                "state",
                "session_id",
                "sessionId",
                "flow",
                "page_type",
                "pageType",
                "continue_url",
                "continueUrl",
                "login_verifier",
                "verifier",
                "email_verification_mode",
            },
        ),
    }


#: Last dump summary seen per stage, as a canonical JSON snapshot.  Measured
#: 2026-09-14 over 81 live dumps: the whole summary changed only **16 times**,
#: and ``top_keys`` had exactly one value in all 81.  So a repeated summary is
#: the same 17 names reprinted once per account -- 10.9% of
#: ``backend_stdout.log`` carrying nothing new.  The server's key set is the
#: only readable field (all values are redacted to a length), and
#: ``email_verification_mode`` + ``passwordless_disabled`` versus
#: ``passwordless_login_magic_link_sent`` is the whole passwordless-vs-password
#: distinction.
#:
#: A snapshot rather than the caller's dict: :func:`fetch_client_auth_session_dump`
#: returns the same object it prints, so a caller is free to mutate it.
#:
#: Why the change case prints a *diff* rather than the whole summary -- measured
#: again on the 18:43 batch: one 13-key account among 17-key ones flips this
#: fingerprint **twice** (there and back), and both flips reprinted all 795
#: characters.  One remembered value cannot tell "the server changed shape" from
#: "the server is alternating shapes", and the 15 keys that did *not* move were
#: the part carrying nothing.
_LAST_DUMP_TEXT: dict[str, str] = {}

#: Every summary shape each stage has already printed a *diff* for, as canonical
#: JSON snapshots.  ``_LAST_DUMP_TEXT`` answers "did this change since the last
#: dump"; this answers "has this shape been explained yet".
#:
#: Measured 2026-09-14 on the 18:43 batch: of the 20 changes in the log, **10
#: were the server walking back to a shape it had already shown** (17 -> 13 ->
#: 17).  A diff alone still paid 363 characters for each leg of a round trip it
#: had already described once.
_SEEN_DUMP_SHAPES: dict[str, set[str]] = {}


def _dump_key_count(summary) -> int:
    keys = summary.get("client_auth_session_keys")
    return len(keys) if isinstance(keys, list) else 0


def _dump_key_diff(previous, current) -> dict:
    """Which keys moved, carrying only the *incoming* value.

    Lists report ``added``/``removed`` instead of the whole list: a 17-key shape
    losing four names and gaining one *is* the observation, and reprinting all
    17 names to say so is what made the line 795 characters.  A key that
    disappeared reports ``None`` -- that is the observation too, and dropping it
    would make "gone" and "still there" look alike.
    """
    diff = {}
    for key in sorted(set(previous) | set(current)):
        before, after = previous.get(key), current.get(key)
        if before == after:
            continue
        if isinstance(before, list) and isinstance(after, list):
            added = [item for item in after if item not in before]
            removed = [item for item in before if item not in after]
            if added or removed:
                diff[key] = {"added": added, "removed": removed}
        elif isinstance(before, dict) and isinstance(after, dict):
            nested = _dump_key_diff(before, after)
            if nested:
                diff[key] = nested
        else:
            diff[key] = after
    return diff


def compact_auth_dump_text(stage, summary) -> str:
    """The text to print for one dump: the summary, a diff, or a repeat marker.

    Only the *printing* is compacted -- :func:`fetch_client_auth_session_dump`
    still returns the full summary, so no caller loses data.

    Three outcomes, in order: the first dump of a stage prints in full, a shape
    the stage has not shown before prints its diff, and a shape it *has* shown
    prints a count **plus its diff**.  That last case is what an alternating
    server needs -- see ``_SEEN_DUMP_SHAPES``.

    🔴 2026-09-16（P2-1）：``(seen before)`` 分支**必须带 diff**。
    它原来只打 ``changed: N keys (seen before)``，**不给键名**，于是日志里
    「服务端回到已经见过的形状」这一步变成了不可回溯的一行 —— 任何基于日志的
    离线判据都会在那里系统性漏数据。实测代价：本扫描第一版关联脚本因此把
    19 个 dump 信号只识别出 3 个（见
    ``docs/audits/scan-2026-09-16-protocol-registration-optimization.md`` P2-1）。

    这不是把降噪撤掉：``unchanged`` 分支（最常见的那种，实测占 41 行 dump 的 20 行）
    仍然塌成一行计数，省下的大头不动；``seen before`` 只从约 40 字符涨到与
    diff 分支同量级（首个 diff 实测 < 400 字符，本批 8 行 ⇒ 约 +2.4 KB）。
    换来的是**从首行全文起、逐行走 diff 即可完整重建每个 run 的键集合**。
    """
    if not isinstance(summary, dict):
        return str(summary)
    snapshot = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    previous = _LAST_DUMP_TEXT.get(stage)
    _LAST_DUMP_TEXT[stage] = snapshot
    seen = _SEEN_DUMP_SHAPES.setdefault(stage, set())
    already_seen = snapshot in seen
    seen.add(snapshot)
    if previous is None:
        return json.dumps(summary, ensure_ascii=False)
    if previous == snapshot:
        return f"{_dump_key_count(summary)} keys unchanged"
    diff = _dump_key_diff(json.loads(previous), summary)
    if not diff:
        # Reached by a reordered key list: ``changed: {}`` reads as a change with
        # nothing in it, so it has to collapse like any other repeat.
        return f"{_dump_key_count(summary)} keys unchanged"
    if already_seen:
        return f"changed: {_dump_key_count(summary)} keys (seen before) {json.dumps(diff, ensure_ascii=False)}"
    return f"changed: {json.dumps(diff, ensure_ascii=False)}"


#: ``client_auth_session`` 里代表「发码事务**挂在半路**」的键 —— 服务端收下了请求、
#: 却**没有完成**派发。反过来，它缺席（且 dump 形状可读）才说明派发已经走完。
#:
#: 实测 2026-09-16（批次 25288，21 账号）：6 个 ``email_otp_poll_timeout`` 的 run
#: 在 ``after_otp_send`` 时**都还带着** ``passwordless_email_otp_send_pending``，
#: 而 15 个拿到码的 run 从头到尾没有这个键（18 键形状 vs 17 键形状）。
#:
#: 🔴 只放这一个键。``passwordless_login_magic_link_sent`` **不属于**「挂起」：
#: 它表示服务端按**登录**泳道发了一封 magic link，实测那三个 run 里有 2 个
#: **正常收到了码**（`da7df644` 20:39:04 取码 → validate 200）。把它算成挂起会
#: 让登录泳道被误判为「不可能拿到码」。它的语义见 ``LOGIN_LANE_KEYS``。
DISPATCH_PENDING_KEYS: tuple[str, ...] = ("passwordless_email_otp_send_pending",)

#: ``client_auth_session`` 里代表「服务端把这次 authorize 当**登录**处理」的键。
#: 它出现在 ``after_signup_state``（**取码之前**）⇒ 该地址在服务端已存在。
#:
#: 实测 2026-09-16（批次 25288）：3 个出现该键的 run **全部**是已注册地址
#: （精确率 3/3），但 11 个已注册 run 里只有 3 个出现它（**召回率仅 27%**）
#: ⇒ 只能**单向**使用：命中即止损，未命中不能推出「未注册」。
LOGIN_LANE_KEYS: tuple[str, ...] = ("passwordless_login_magic_link_sent",)


def _keys_of(summary) -> list[str] | None:
    """dump summary 里的事务键列表；不可读时返回 ``None``（而不是空列表）。"""
    if not isinstance(summary, dict):
        return None
    keys = summary.get("client_auth_session_keys")
    if not isinstance(keys, list) or not keys:
        # 非 200 的 dump 返回 ``{"status": ..., "body": ...}``，没有键列表。
        return None
    return [str(key).lower() for key in keys]


def otp_dispatch_verdict(summary) -> str:
    """判定服务端是否**完成**了验证码派发，供 ``wait_email_otp`` 分辨超时根因。

    返回 ``"stuck"`` / ``"dispatched"`` / ``"unknown"``：

    * ``"stuck"`` —— dump 可读且事务键里仍有挂起键：服务端没完成派发。
      这是**派发侧**证据（出口只是可能的原因之一，单账号不足以判定）。
    * ``"dispatched"`` —— dump 可读且没有挂起键：派发已走完，码没到是**邮箱侧**
      的事（与出口无关）。
    * ``"unknown"`` —— dump 不可用（非 200 / 传输失败 / 形状不可读 / 键列表为空）：
      **没有证据**。调用方必须回落到旧行为，不要凭猜测下结论 —— 空键列表尤其
      不能当 ``dispatched``：那说明 body 里压根没有 ``client_auth_session``。
    """
    keys = _keys_of(summary)
    if keys is None:
        return "unknown"
    if any(pending in key for key in keys for pending in DISPATCH_PENDING_KEYS):
        return "stuck"
    return "dispatched"


def signup_lane_verdict(summary) -> str:
    """判定服务端把这次 authorize 当**注册**还是**登录** —— 取码之前的止损判据。

    返回 ``"login"`` / ``"signup"`` / ``"unknown"``：

    * ``"login"`` —— 事务里有 magic-link 键 ⇒ 该地址在服务端**已存在**，
      注册必然以 ``user_already_exists`` 收场。精确率 3/3，**召回率仅 27%**。
    * ``"signup"`` —— dump 可读且没有该键。⚠️ **不能**读成「未注册」：
      实测 10 个这种形状的 run 里 9 个最终仍是 ``user_already_exists``。
      服务端只在 ``create_account`` 给出存在性判决，而那时 OTP 已经花掉了。
    * ``"unknown"`` —— dump 不可用，没有证据。
    """
    keys = _keys_of(summary)
    if keys is None:
        return "unknown"
    if any(marker in key for key in keys for marker in LOGIN_LANE_KEYS):
        return "login"
    return "signup"


def auth_dump_failure_text(body) -> str:
    """The diagnostic tail printed when the dump endpoint is not 200.

    Until 2026-09-14 the status code was the whole line, and that cost exactly
    the diagnosis the dump exists to provide: a dead-end account's relogin
    printed ``client_auth_session_dump[existing_login_after_otp_validate_failed]:
    404`` on both attempts, so an empty body and a Cloudflare block page were
    indistinguishable.  Shape only when the answer *was* JSON -- key names are
    what this branch is read for -- but the text is kept when it was not,
    because then what came back *is* the diagnosis.
    """
    if isinstance(body, dict):
        raw = body.get("_raw")
        if isinstance(raw, str):
            return f" body={raw[:300]!r}" if raw else " body=<empty>"
        return f" body_keys={sorted(str(key) for key in body)[:12]}"
    if body is None:
        return " body=<none>"
    return f" body={str(body)[:300]!r}"


def fetch_client_auth_session_dump(session, auth_base, base_headers, stage=""):
    try:
        response = request_with_retry(
            session,
            "get",
            f"{auth_base}/api/accounts/client_auth_session_dump",
            label=f"client_auth_session_dump {stage or 'default'}",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{auth_base}/email-verification"},
            impersonate=auth_impersonate(),
        )
    except Exception as exc:
        print(f"  client_auth_session_dump[{stage or 'default'}] warning: {exc}")
        return {}
    body = _json_or_raw(response, limit=1200)
    if getattr(response, "status_code", 0) != 200:
        print(
            f"  client_auth_session_dump[{stage or 'default'}]: {response.status_code}"
            f"{auth_dump_failure_text(body)}"
        )
        return {"status": getattr(response, "status_code", 0), "body": body}
    summary = auth_dump_summary(body)
    stage_key = stage or "default"
    # 1200, not 800: the observed maximum summary is 795 characters, so the old
    # cap was five bytes from silently cutting the ``signals`` tail off a line
    # whose whole point is that tail.
    print(f"  client_auth_session_dump[{stage_key}]: {compact_auth_dump_text(stage_key, summary)[:1200]}")
    return summary

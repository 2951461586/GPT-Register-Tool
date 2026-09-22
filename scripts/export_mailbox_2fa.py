"""Export iCloud accounts as ``Email----接码URL----2FA`` lines.

Definition of the export set (same as the 2026-09-20 census):
    domain == icloud.com  AND  优惠状态 == "Free·无优惠"   -> 1684 accounts

Column sources
--------------
* Email   -- ``accounts.email``
* 接码URL -- the mailbox OTP URL.  Preferred source is the live pool file
             ``mailbox_tokens.txt`` (parsed with the project's own
             ``mailbox_parsers.parse_mailbox_pool_line``); the account's own
             ``mailbox_token`` column is the fallback.  Which one won is
             recorded per row in ``--audit`` output.
* 2FA     -- ``accounts.totp_secret`` (the column), falling back to
             ``raw_json.totp_secret``.  🔴 The column is the durable truth:
             ``upsert_account`` rebuilds ``raw_json`` from
             ``AccountSessionModel.safe_snapshot()``, whose whitelist does not
             carry the ``promotion*`` keys -- and the same class of rebuild can
             drop ``totp_secret`` from the JSON while the column keeps it.  Read
             the column first so a later rebuild cannot silently blank the 2FA
             column of the delivered file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "runtime" / "accounts.sqlite3"
POOL_FILE = ROOT / "mailbox_tokens.txt"
OUT_DIR = ROOT / "runtime" / "analysis"
PROMO_FREE = "Free·无优惠"


def _terminal_markers() -> tuple[str, ...]:
    """Reuse the batch runner's terminal-failure list instead of copying it.

    A second copy would drift from the first -- this codebase already has a
    precedent (``http_client.TRANSIENT_MARKERS`` vs the ``failure_registry``
    network class, which have diverged).  The import is cheap: every
    module-level import in ``batch_enable_2fa`` is stdlib.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from batch_enable_2fa import TERMINAL_ERROR_MARKERS
        return tuple(TERMINAL_ERROR_MARKERS)
    except Exception:  # pragma: no cover - classification is best-effort
        return ()


def load_pool() -> dict[str, str]:
    from sms_tool.mailbox_parsers import parse_mailbox_pool_line

    pool: dict[str, str] = {}
    text = POOL_FILE.read_text(encoding="utf-8", errors="replace")
    for index, line in enumerate(text.splitlines(), start=1):
        account = parse_mailbox_pool_line(line, source_path=str(POOL_FILE), line_no=index)
        if account is None:
            continue
        email = str(account.email or "").strip().lower()
        token = str(account.token or "").strip()
        if email and token:
            pool[email] = token
    return pool


def load_rows() -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    for r in conn.execute(
        "SELECT email, mailbox_token, totp_secret, twofa_enrolled_at, raw_json "
        "FROM accounts WHERE email LIKE '%@icloud.com'"
    ):
        try:
            data = json.loads(r["raw_json"] or "{}")
        except Exception:
            data = {}
        if str(data.get("promotion_status") or "") != PROMO_FREE:
            continue
        rows.append({
            "email": str(r["email"] or "").strip().lower(),
            "account_url": str(r["mailbox_token"] or data.get("mailbox_token") or "").strip(),
            # Column first, raw_json only as a fallback -- see module docstring.
            "totp_secret": str(r["totp_secret"] or data.get("totp_secret") or "").strip(),
            "twofa_enrolled_at": int(r["twofa_enrolled_at"] or 0),
            "twofa_enroll_error": str(data.get("twofa_enroll_error") or "").strip(),
        })
    conn.close()
    rows.sort(key=lambda r: r["email"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Email----URL----2FA manifest")
    parser.add_argument("--out", default="")
    parser.add_argument("--only-2fa", action="store_true",
                        help="emit only rows that actually have a TOTP secret")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    pool = load_pool()
    rows = load_rows()

    stats = {
        "total": len(rows),
        "with_2fa": 0,
        "without_2fa": 0,
        "without_2fa_terminal": 0,
        "without_2fa_retryable": 0,
        "url_from_pool": 0,
        "url_from_account": 0,
        "url_conflict": 0,
        "url_missing": 0,
    }
    terminal_markers = _terminal_markers()
    lines: list[str] = []
    audit: list[dict] = []

    for row in rows:
        email = row["email"]
        pool_url = pool.get(email, "")
        account_url = row["account_url"]
        if pool_url and account_url and pool_url != account_url:
            stats["url_conflict"] += 1
        if pool_url:
            url, source = pool_url, "pool"
            stats["url_from_pool"] += 1
        elif account_url:
            url, source = account_url, "account"
            stats["url_from_account"] += 1
        else:
            url, source = "", "missing"
            stats["url_missing"] += 1
        if row["totp_secret"]:
            stats["with_2fa"] += 1
        else:
            # An empty 2FA field is not a neutral outcome -- the row is useless
            # to whoever consumes this file.  Split it so the operator can tell
            # "still retryable" apart from "will never succeed":
            #   terminal  = dead mailbox / deleted account / no password on
            #               record -> more passes will NOT help, the mailbox
            #               source has to be replenished (or the account given
            #               up on)
            #   retryable = transport or session noise -> another pass may fix
            stats["without_2fa"] += 1
            last_error = str(row["twofa_enroll_error"] or "")
            if terminal_markers and any(m in last_error for m in terminal_markers):
                stats["without_2fa_terminal"] += 1
            else:
                stats["without_2fa_retryable"] += 1
        if args.only_2fa and not row["totp_secret"]:
            continue
        lines.append(f"{email}----{url}----{row['totp_secret']}")
        audit.append({
            "email": email, "url": url, "url_source": source,
            "has_2fa": bool(row["totp_secret"]),
            "pool_url_differs": bool(pool_url and account_url and pool_url != account_url),
            "twofa_enroll_error": row["twofa_enroll_error"],
        })

    # The empty-2FA rows are not a neutral outcome -- whoever consumes this file
    # cannot use them.  The main file stays complete (1684 rows, no row dropped,
    # no field shifted), but the unusable rows are also written out on their own
    # so the operator can act on them without diffing the whole manifest.
    no2fa_terminal: list[dict] = []
    no2fa_retryable: list[dict] = []
    for item in audit:
        if item["has_2fa"]:
            continue
        err = item["twofa_enroll_error"]
        if terminal_markers and any(m in err for m in terminal_markers):
            no2fa_terminal.append(item)
        else:
            no2fa_retryable.append(item)

    suffix = "_only2fa" if args.only_2fa else ""
    txt_path = out_dir / f"icloud_no_trial_email_url_2fa{suffix}_{stamp}.txt"
    # newline="\n" is mandatory: write_text defaults to newline=None, which
    # translates "\n" to os.linesep -- on Windows that silently turns the whole
    # manifest CRLF.  These files are consumed by an external tool, and the
    # project convention (see .gitattributes) is LF.  Same defect class as the
    # ratchet baselines; guarded repo-wide by tests/test_line_ending_guard.py.
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    audit_path = out_dir / f"icloud_no_trial_email_url_2fa{suffix}_{stamp}.audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                          encoding="utf-8", newline="\n")

    list_path = None
    if no2fa_terminal or no2fa_retryable:
        list_path = out_dir / f"icloud_no_trial_email_url_2fa{suffix}_{stamp}_no2fa.txt"
        block: list[str] = [
            "# 2FA 字段为空的账号 —— 主文件里这些行不可用（第 3 段为空）。",
            "# 主文件仍保持完整行数、未删行；本文件只是把它们单独标出来。",
            "# 格式: email----接码URL----原因",
            "",
            f"## 终态：再跑轮次无效，需补邮箱源或放弃  ({len(no2fa_terminal)} 行)",
        ]
        block += [f"{i['email']}----{i['url']}----{i['twofa_enroll_error']}"
                  for i in no2fa_terminal]
        block += [
            "",
            f"## 可重试：再跑一轮可能成功  ({len(no2fa_retryable)} 行)",
        ]
        block += [f"{i['email']}----{i['url']}----{i['twofa_enroll_error']}"
                  for i in no2fa_retryable]
        list_path.write_text("\n".join(block) + "\n", encoding="utf-8", newline="\n")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"导出: {txt_path}  ({len(lines)} 行)")
    print(f"审计: {audit_path}")
    if list_path is not None:
        print(f"空 2FA 清单: {list_path}  "
              f"(终态 {len(no2fa_terminal)} / 可重试 {len(no2fa_retryable)})")
    if stats["without_2fa"]:
        print(
            f"\n⚠️  {stats['without_2fa']} 行的 2FA 字段为空 —— 这些行不可用：\n"
            f"    终态（加轮次无效，需补邮箱源）: {stats['without_2fa_terminal']}\n"
            f"    可重试（再跑一轮可能成功）    : {stats['without_2fa_retryable']}\n"
            f"    逐行原因见审计文件的 twofa_enroll_error 字段。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

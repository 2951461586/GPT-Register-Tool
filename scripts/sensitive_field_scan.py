"""Fail CI when source logs a prefix of a credential or report artifacts leak one."""

from __future__ import annotations

import json
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _rel(path: Path) -> str:
    """Repo-relative label when the path is inside the repo, else absolute.

    An ``--artifacts`` directory may live outside the repo (a staging area, a
    release asset directory).  ``Path.relative_to`` raises on that, so calling it
    unguarded made the whole ``--artifacts`` path crash on a relative argument --
    which is why nothing could use it.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


SOURCE_PREFIX = re.compile(
    r"(?i)(?:access_token|refresh_token|totp_secret|ba_token|stripe_pk|card_number|cardNumber|"
    r"(?:self\.)?_(?:pp|ba)_token)\s*\[\s*:\s*\d+"
)
SOURCE_CARD_FRAGMENT = re.compile(r"(?i)(?:card(?:_?number|_?last4)?|pan)[^\n]{0,80}(?:\[\s*-\d+\s*:|substring\s*\()")
# ``<credential key> = <value>`` inside an artifact.  Non-values are rejected up
# front, each with its reason:
#
#   * placeholder sentinels -- ``missing_*`` is the "we could not obtain one" label
#     written into ``runtime/account_relogin_guard.json``'s ``last_shape`` field,
#     and JSON booleans (``true``/``false``) are config flags, not values.
#   * key echoes -- ``totp_secret=totp_secret`` is a keyword argument echoing the
#     variable name inside a quoted source fragment (retired
#     ``runtime/_retired_*/_phases*.json``), and ``s.oauth_refresh_token`` is an
#     attribute chain; both are code, not data.
#   * empty values -- ``""`` / ``''`` are placeholders in every artifact writer
#     here.
#
# The optional closing quote after the key (and the optional opening quote before
# the value) are what make JSON artifacts match at all.  Without them
# ``"access_token": "..."`` was **never** detected: the key alternation required
# ``=``/``:`` immediately after the key, and JSON has a quote in between.  That
# blind spot mattered because ``scan_release_payload.check_artifact_regexes``
# reuses this same authority over the shipped ``.json`` / ``.jsonl`` payload.
#
# False positives matter as much as false negatives here: a gate that cries wolf
# on its own diagnostic output gets ignored, which is how a real leak later slips
# through.  Both classes (``missing_*`` sentinels and source echoes) were the only
# findings this scan produced on a clean tree before the fix.
ARTIFACT_SECRET = re.compile(
    r"(?i)(?P<artifact_key>access[_-]?token|refresh[_-]?token|totp[_-]?secret|ba[_-]?token|"
    r"card[_-]?(?:number|cvv))"
    r'"?\s*[=:]\s*"?'
    r"(?!\[REDACTED\]|\*{3,}|null|None|missing_|true|false|''|\")"
    r"(?!(?P=artifact_key)(?![A-Za-z0-9_]))"
    r"(?!(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z0-9_]*(?P=artifact_key)(?![A-Za-z0-9_]))"
    r"[^\s,}]+"
)
ARTIFACT_CARD_FRAGMENT = re.compile(r"(?i)(?:card|卡片|尾号)[^\n]{0,30}\*{2,}\d{2,}")

# A sensitive name only leaks if it reaches the sink *unredacted*, so a call that
# hands the name to a redactor is fine.  The set is matched by name because that
# is the only thing available at this layer:
#   sanitize / redact / mask -- the convention used at every other call site
#   root_reason              -- ``_root_reason(exc, proxy)``
#                               (sms_tool/sentinel/client.py:85) runs the message
#                               through ``redact_proxy_text(text, proxy)`` at
#                               line 112, so its ``proxy`` argument is consumed by
#                               a redactor even though neither the helper's name
#                               nor the call site says "redact".
SAFE_PRINT_WRAPPER = re.compile(r"(?i)(sanitize|redact|mask|root_reason)")


def _policy_embedding_failures(root: Path | None = None) -> list[str]:
    """The desktop project that compiles the sanitizer must embed the policy.

    ``SensitiveDataSanitizer`` reads ``SmsWorkbench.sensitive_policy.json`` out of
    **its own** assembly manifest -- ``Assembly.GetExecutingAssembly()`` at
    ``SmsWorkbench.Contracts/SensitiveDataSanitizer.cs:94`` -- so the embed has to
    live in the project that compiles that file, and nowhere else.  Resolving the
    pair beats hard-coding a path: commit ``aeaea75`` moved the sanitizer and the
    embed together from ``SmsWorkbench/`` into ``SmsWorkbench.Contracts/``, after
    which the old ``SmsWorkbench/SmsWorkbench.csproj`` assertion failed on a
    correct tree while the policy was still embedded -- a red gate on green code,
    which is how a guard loses its authority.

    Ownership decision (2026-09-30): ``SmsWorkbench.Contracts`` is the sole
    embedder.  Embedding it in ``SmsWorkbench/`` as well would add a second
    resource with the same logical name in a different assembly, which nothing
    reads -- ``GetExecutingAssembly`` cannot reach it.  Documented in
    ``docs/directory-map.md``.
    """
    base = ROOT if root is None else root
    desktop_projects = sorted(
        path for path in base.glob("SmsWorkbench*/*.csproj") if not {"bin", "obj"} & set(path.parts)
    )
    owner = next(
        (path for path in desktop_projects if (path.parent / "SensitiveDataSanitizer.cs").is_file()),
        None,
    )
    if owner is None:
        return ["SensitiveDataSanitizer.cs: not compiled by any desktop project"]
    try:
        project = owner.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [f"{owner.name}: cannot read project: {exc}"]
    if "sensitive_policy.json" not in project or "SmsWorkbench.sensitive_policy.json" not in project:
        return [f"{owner.name}: shared sensitive policy is not embedded"]
    return []


def main(argv: list[str] | None = None) -> int:
    """扫描源码与产物。

    可选参数：--artifacts <dir>（可重复）追加产物目录，供发布流程在打包后调用。

    默认只扫 ``logs/``。``runtime/`` **刻意不在默认范围内**：它是 .gitignore 的
    本地活状态，**按设计持有凭据** —— ``scripts/batch_enable_2fa.py`` 把 TOTP
    密钥写进 ``runtime/analysis/2fa_enroll_report_*.jsonl`` 并注明那是 "the only
    place the secret exists -- treat it as the recovery source of truth"，
    ``docs/directory-map.md`` 也把该目录定义为 "Never commit; summarize redacted
    state only"。用凭据闸门扫一个按设计存凭据的目录只会永远红，而永远红的闸门
    正是本仓反复记录的失效模式（误报 ⇒ 被忽略 ⇒ 真泄漏过闸）。

    要审计本地活状态，显式传入：``--artifacts runtime``。要守发布包，
    ``scripts/scan_release_payload.py`` 复用本模块的 ``ARTIFACT_SECRET``。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_artifacts: list[Path] = []
    while "--artifacts" in argv:
        index = argv.index("--artifacts")
        if index + 1 >= len(argv):
            print("--artifacts requires a directory argument", file=sys.stderr)
            return 2
        extra_artifacts.append(Path(argv[index + 1]).resolve())
        del argv[index : index + 2]
    if argv:
        print(f"unknown arguments: {' '.join(argv)}", file=sys.stderr)
        return 2

    failures: list[str] = []
    policy_path = ROOT / "sensitive_policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
        if policy.get("schema") != "sensitive_policy.v1":
            failures.append("sensitive_policy.json: unsupported schema")
        if not policy.get("sensitive_keys") or not policy.get("text_patterns"):
            failures.append("sensitive_policy.json: keys and text_patterns are required")
        if not policy.get("sensitive_options"):
            failures.append("sensitive_policy.json: sensitive_options is required")
        for index, item in enumerate(policy.get("text_patterns") or []):
            re.compile(str(item["pattern"]))
            if not str(item.get("replacement") or ""):
                failures.append(f"sensitive_policy.json: text_patterns[{index}] replacement is required")
    except (OSError, ValueError, KeyError, re.error) as exc:
        failures.append(f"sensitive_policy.json: invalid policy: {exc}")

    project_failures = _policy_embedding_failures()
    failures.extend(project_failures)

    source_groups = (
        (ROOT / "sms_tool", ("*.py",)),
        (ROOT / "services", ("*.py",)),
        (ROOT / "SmsWorkbench", ("*.cs",)),
    )
    for base, patterns in source_groups:
        paths = (path for pattern in patterns for path in base.rglob(pattern))
        for path in paths:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
            for number, line in enumerate(source.splitlines(), 1):
                if SOURCE_PREFIX.search(line):
                    failures.append(f"{path.relative_to(ROOT)}:{number}: credential prefix logging")
                if SOURCE_CARD_FRAGMENT.search(line) and any(
                    marker in line.lower() for marker in ("print", "log", "appendline")
                ):
                    failures.append(f"{path.relative_to(ROOT)}:{number}: card fragment logging")
            if path.suffix.lower() == ".py":
                try:
                    tree = ast.parse(source, filename=str(path))
                except SyntaxError as exc:
                    failures.append(f"{path.relative_to(ROOT)}:{exc.lineno}: source cannot be parsed")
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "print":
                        continue
                    expression = ast.get_source_segment(source, node) or ""
                    names = {item.id.lower() for item in ast.walk(node) if isinstance(item, ast.Name)}
                    sensitive_names = {
                        "proxy",
                        "us_proxies",
                        "promo_proxies",
                        "access_token",
                        "refresh_token",
                        "ba_token",
                        "totp_secret",
                        "password",
                        "card_number",
                        "cvv",
                        "cvc",
                    }
                    if names & sensitive_names and not SAFE_PRINT_WRAPPER.search(expression):
                        failures.append(f"{path.relative_to(ROOT)}:{node.lineno}: sensitive output bypasses safe_print")
    for base in (ROOT / "logs", *extra_artifacts):
        if not base.is_dir():
            if base in extra_artifacts:
                failures.append(f"artifacts directory does not exist: {base}")
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".log", ".txt"}:
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if ARTIFACT_SECRET.search(line):
                        failures.append(f"{_rel(path)}:{number}: sensitive artifact value")
                    if ARTIFACT_CARD_FRAGMENT.search(line):
                        failures.append(f"{_rel(path)}:{number}: card fragment artifact")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("sensitive field scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

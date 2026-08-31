"""Fail CI when source logs a prefix of a credential or report artifacts leak one."""

from __future__ import annotations

import json
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PREFIX = re.compile(
    r"(?i)(?:access_token|refresh_token|totp_secret|ba_token|stripe_pk|card_number|cardNumber|"
    r"(?:self\.)?_(?:pp|ba)_token)\s*\[\s*:\s*\d+"
)
SOURCE_CARD_FRAGMENT = re.compile(
    r"(?i)(?:card(?:_?number|_?last4)?|pan)[^\n]{0,80}(?:\[\s*-\d+\s*:|substring\s*\()"
)
ARTIFACT_SECRET = re.compile(
    r"(?i)(?:access[_-]?token|refresh[_-]?token|totp[_-]?secret|ba[_-]?token|card[_-]?(?:number|cvv))"
    r"\s*[=:]\s*(?!\[REDACTED\]|\*{3,}|null|\"\"|''|None)[^\s,}]+"
)
ARTIFACT_CARD_FRAGMENT = re.compile(r"(?i)(?:card|卡片|尾号)[^\n]{0,30}\*{2,}\d{2,}")


def main(argv: list[str] | None = None) -> int:
    """扫描源码与产物。

    可选参数：--artifacts <dir>（可重复）追加产物目录，供发布流程在打包后调用。
    默认仍只扫 runtime/ 与 logs/，行为不变。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_artifacts: list[Path] = []
    while "--artifacts" in argv:
        index = argv.index("--artifacts")
        if index + 1 >= len(argv):
            print("--artifacts requires a directory argument", file=sys.stderr)
            return 2
        extra_artifacts.append(Path(argv[index + 1]))
        del argv[index:index + 2]
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

    project = (ROOT / "SmsWorkbench" / "SmsWorkbench.csproj").read_text(encoding="utf-8-sig")
    if "sensitive_policy.json" not in project or "SmsWorkbench.sensitive_policy.json" not in project:
        failures.append("SmsWorkbench.csproj: shared sensitive policy is not embedded")

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
                if SOURCE_CARD_FRAGMENT.search(line) and any(marker in line.lower() for marker in ("print", "log", "appendline")):
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
                        "proxy", "us_proxies", "promo_proxies", "access_token", "refresh_token",
                        "ba_token", "totp_secret", "password", "card_number", "cvv", "cvc",
                    }
                    if names & sensitive_names and not re.search(r"(?i)(sanitize|redact|mask)", expression):
                        failures.append(f"{path.relative_to(ROOT)}:{node.lineno}: sensitive output bypasses safe_print")
    for base in (ROOT / "runtime", ROOT / "logs", *extra_artifacts):
        if not base.is_dir():
            if base in extra_artifacts:
                failures.append(f"artifacts directory does not exist: {base}")
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".log", ".txt"}:
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if ARTIFACT_SECRET.search(line):
                        failures.append(f"{path.relative_to(ROOT)}:{number}: sensitive artifact value")
                    if ARTIFACT_CARD_FRAGMENT.search(line):
                        failures.append(f"{path.relative_to(ROOT)}:{number}: card fragment artifact")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("sensitive field scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

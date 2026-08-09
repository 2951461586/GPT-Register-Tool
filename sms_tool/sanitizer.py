"""Single redaction boundary for operator-visible data.

This module is intentionally dependency-free so it can be used by logging,
JSONL reports, desktop IPC, and subprocess adapters without import cycles.
Sensitive values are replaced in full; no token or secret prefix is retained.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "sensitive_policy.v1"


@lru_cache(maxsize=1)
def load_sensitive_policy(path: str | Path | None = None) -> Mapping[str, Any]:
    source = Path(path).resolve() if path else Path(__file__).resolve().parent.parent / "sensitive_policy.json"
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"unsupported sensitive policy schema: {value.get('schema') if isinstance(value, dict) else ''}")
    if not isinstance(value.get("sensitive_keys"), list) or not isinstance(value.get("text_patterns"), list):
        raise ValueError("sensitive policy keys and text_patterns must be arrays")
    return value


SENSITIVE_POLICY = load_sensitive_policy()
REDACTED_VALUE = str(SENSITIVE_POLICY.get("redacted_value") or "[REDACTED]")
SENSITIVE_KEYS = frozenset(str(item) for item in SENSITIVE_POLICY["sensitive_keys"])
_SENSITIVE_KEYS_LOWER = frozenset(item.lower() for item in SENSITIVE_KEYS)
_SAFE_KEY_SUFFIXES = tuple(str(item) for item in SENSITIVE_POLICY.get("safe_key_suffixes") or ())
_SENSITIVE_KEY_FRAGMENTS = tuple(str(item) for item in SENSITIVE_POLICY.get("sensitive_key_fragments") or ())
SENSITIVE_OPTIONS = frozenset(str(item).lower() for item in SENSITIVE_POLICY.get("sensitive_options") or ())


def _python_replacement(value: str) -> str:
    return re.sub(r"\$(\d+)", r"\\g<\1>", value)


_TEXT_PATTERNS = tuple(
    (re.compile(str(item["pattern"])), _python_replacement(str(item.get("replacement") or REDACTED_VALUE)))
    for item in SENSITIVE_POLICY["text_patterns"]
)


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize(value: Any, *, key: str = "") -> Any:
    """Recursively redact mappings/lists and free-form strings."""
    lowered = key.lower()
    key_is_sensitive = (
        lowered in _SENSITIVE_KEYS_LOWER
        or (
            not lowered.endswith(_SAFE_KEY_SUFFIXES)
            and any(part in lowered for part in _SENSITIVE_KEY_FRAGMENTS)
        )
    )
    if key_is_sensitive:
        return REDACTED_VALUE if value not in (None, "") else value
    if isinstance(value, Mapping):
        return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, BaseException):
        return sanitize_text(value)
    return value


def sanitize_json(value: Any, **kwargs: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, default=str, **kwargs)


def sanitize_command_args(args: Any) -> list[str]:
    """Redact option values using the canonical cross-language policy."""
    values = [str(item or "") for item in (args or ())]
    output: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        option, separator, _ = value.partition("=")
        if option.lower() in SENSITIVE_OPTIONS:
            output.append(f"{option}={REDACTED_VALUE}" if separator else option)
            if not separator and index + 1 < len(values):
                output.append(REDACTED_VALUE)
                index += 1
        else:
            output.append(sanitize_text(value))
        index += 1
    return output


# Compatibility names used by adapters and tests.
redact_sensitive_text = sanitize_text
redact_sensitive_values = sanitize
sanitize_exception = sanitize_text

"""Load optional project ``.env`` values without adding a runtime dependency.

The process environment remains authoritative: values already exported by the
launcher are never replaced by the project file unless ``override=True`` is
requested explicitly.  This keeps deployment secrets outside persisted JSON
while making local driver configuration convenient.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
_PROJECT_ROOT = PROJECT_ROOT
_ENV_PATH = ENV_PATH
_DEFAULT_ENV_PATH = ENV_PATH
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOADED = False


def _default_path() -> Path:
    # Keep both public and reference-compatible private names patchable in
    # tests and integrations without making one mutable global authoritative.
    if _ENV_PATH != _DEFAULT_ENV_PATH:
        return Path(_ENV_PATH)
    return Path(ENV_PATH)


def _decode_value(raw: str) -> str:
    """Decode the small quoted-value subset commonly used in ``.env`` files."""
    value = str(raw).strip()
    if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
        return value
    value = value[1:-1]
    if raw.strip().startswith("'"):
        return value.replace("\\'", "'")
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        '"': '"',
        "\\": "\\",
    }
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            next_char = value[index + 1]
            out.append(escapes.get(next_char, next_char))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and (len(line) == 6 or line[6].isspace()):
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue
        values[key] = _decode_value(raw_value)
    return values


def read_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Read a dotenv file into a mapping without modifying ``os.environ``."""
    target = Path(path) if path is not None else _default_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    return _parse_lines(text.splitlines())


def load_env(*, path: str | Path | None = None, override: bool = False) -> Path:
    """Load the project dotenv file and return its path.

    Missing files and malformed lines are intentionally non-fatal.  This
    function is safe to call during module import and does not log values.
    """
    global _LOADED
    target = Path(path) if path is not None else _default_path()
    for key, value in read_env_file(target).items():
        if override or key not in os.environ:
            os.environ[key] = value
    _LOADED = True
    return target


def ensure_loaded() -> None:
    """Load the project file once, preserving inherited environment values."""
    if not _LOADED:
        load_env()


def env_str(key: str, default: str = "") -> str:
    """Return a trimmed environment value after ensuring dotenv is loaded."""
    ensure_loaded()
    value = os.getenv(str(key))
    return default if value is None or not str(value).strip() else str(value).strip()


__all__ = ["ENV_PATH", "PROJECT_ROOT", "ensure_loaded", "env_str", "load_env", "read_env_file"]

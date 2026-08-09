"""Mandatory operator-output seam backed by the shared sensitive policy."""

from __future__ import annotations

import sys
from typing import Any, TextIO

from .sanitizer import sanitize_command_args, sanitize_text


def safe_print(
    *values: Any,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    target = file or sys.stdout
    text = sep.join(str(value) for value in values)
    print(sanitize_text(text), end=end, file=target, flush=flush)


def safe_exception(value: BaseException | Any) -> str:
    return sanitize_text(value)


def safe_command_display(args: Any) -> str:
    return " ".join(sanitize_command_args(args))


class SanitizingTextIO:
    """Text stream proxy that guarantees policy enforcement at process output."""

    def __init__(self, wrapped: TextIO) -> None:
        self._wrapped = wrapped

    def write(self, value: str) -> int:
        return self._wrapped.write(sanitize_text(value))

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def install_safe_stdio() -> None:
    if not isinstance(sys.stdout, SanitizingTextIO):
        sys.stdout = SanitizingTextIO(sys.stdout)
    if not isinstance(sys.stderr, SanitizingTextIO):
        sys.stderr = SanitizingTextIO(sys.stderr)

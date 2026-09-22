"""File and credential loading shared by the protocol-payment extractors.

Why this module exists
----------------------
Phase 1 of `docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`
extracts the provably provider-free helpers the extractors grew by copy-paste.
This file is batch 3 (file loading).  Batch 1 is ``proxy_bookkeeping.py``.

What "provider-free" means here
-------------------------------
Nothing in this module may hard-code something that differs between the
extractors.  Two knobs had to become parameters, and both were found by reading
bytes rather than by trusting the AST comparison -- which blanks provider tokens
and therefore hides this exact class of difference:

    ``load_token`` -- the env-var name list differs and is NOT a copy:
      ideal  ``("PP_TOKEN", "IDEAL_TOKEN")``
      twint  ``("PP_TOKEN", "TWINT_TOKEN")``
      blik   ``("PP_TOKEN", "IDEAL_TOKEN")``   <- ideal's name, looks like a
                                                copy-paste slip, but it is the
                                                shipped behaviour and unifying it
                                                would change twint's lookup order
    So ``env_names`` is injected, not defaulted to a "canonical" list.

    ``token_file`` -- each extractor reads its own ``SCRIPT_DIR / "token.txt"``,
    and ``SCRIPT_DIR`` is per-extractor.  Injected as a path.

Design note: bodies are copied, not improved
--------------------------------------------
Differential verification can only prove "unchanged" if nothing changed.  So the
bodies below are the extractors' own, character for character, including their
idioms (the ``for/else`` decode ladder, the ``input()`` fallback).  An
"improvement" made during extraction is precisely what would invalidate the
proof.

Rule 10 compliance
------------------
This package must not import `sms_tool`.  Nothing here does; it is pure stdlib.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Callable

__all__ = [
    "load_proxy_file",
    "load_token",
]

# The env var every extractor reads for an explicit session cookie.  Unlike the
# token names above, this one is genuinely shared: `PP_SESSION_TOKEN` appears
# verbatim in ideal, twint and blik.  Kept as a named constant so the callers
# pass one thing rather than repeating a string literal.
SESSION_TOKEN_ENV = "PP_SESSION_TOKEN"


def _environ_get(name: str) -> str:
    """Production default for ``load_token(env_get=...)``.

    Deliberately a module-level function rather than a nested ``def`` inside
    ``load_token``: a nested ``def env_get`` would *shadow the parameter of the
    same name*, so its body could only ever call itself (infinite recursion).
    That mistake passes both AST comparison and ``compileall``, so it is not
    hypothetical -- keep this out of the function body.
    """
    return os.environ.get(name, "")


def _read_stdin(prompt_text: str) -> str:
    """Production default for ``load_token(prompt=...)``.

    A function, not the bare ``input`` builtin, and resolved at call time.  Two
    reasons, both learned the hard way:

    * Writing ``prompt: Callable[[str], str] = input`` captures the builtin as a
      *default argument*, i.e. at ``def`` time.  ``unittest.mock.patch`` on
      ``builtins.input`` then cannot intercept it, so every caller that patches
      ``input`` -- test harnesses, interactive fixtures -- silently falls
      through to the real stdin.
    * Passing the builtin directly also means a caller cannot swap it without
      passing a value, which defeats the point of having the parameter.

    Resolving ``input`` here keeps a single patchable seam.
    """
    return input(prompt_text)


def _shuffle_in_place(items: list[str]) -> None:
    """Production default for ``load_proxy_file(shuffle=...)``.

    ``random.shuffle`` is a *bound method of the module-level ``Random``
    instance*.  Written as a default argument it is captured at def time, and
    because it is already bound, patching ``random.shuffle`` afterwards -- in
    this namespace or the caller's -- has no effect on it.  That made the
    ``shuffle`` parameter nominally injectable but practically inert.

    Calling through the attribute here keeps one late-bound, patchable seam.
    """
    random.shuffle(items)


def load_proxy_file(
    path: Path,
    *,
    register_for_redaction: Callable[[str], None],
    normalize: Callable[[str], str],
    shuffle: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Read a proxy list file, registering each line before normalising it.

    ``register_for_redaction`` and ``normalize`` are injected because both are
    per-extractor: registration writes into that extractor's own redaction
    registry, and the normalisers disagree on the four-field convention (see
    ``proxy_short`` in ``proxy_bookkeeping``).

    ``shuffle`` defaults to ``None`` and is resolved to ``random.shuffle``
    *inside* the body.  Do not write ``shuffle=random.shuffle`` as a default:
    that captures a bound method of the hidden module-level ``Random`` instance
    at def time, after which no amount of patching ``random.shuffle`` -- in this
    namespace or any other -- can intercept the call.  See ``_shuffle_in_place``.
    """
    if shuffle is None:
        shuffle = _shuffle_in_place

    proxies: list[str] = []
    if not path.exists():
        return proxies
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            register_for_redaction(line)
            proxy = normalize(line)
            if proxy:
                proxies.append(proxy)
    shuffle(proxies)
    return proxies


def load_token(
    *,
    env_names: tuple[str, ...],
    token_file: Path,
    normalize_token: Callable[[str], tuple[str, str]],
    log: Callable[[str], None],
    prompt: Callable[[str], str] | None = None,
    env_get: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """Resolve ``(access_token, session_token)`` from env, then file, then prompt.

    ``env_names`` is injected and deliberately has **no default**: ideal and
    twint differ (``IDEAL_TOKEN`` vs ``TWINT_TOKEN``) and blik reuses ideal's
    name, so any default would silently pick one provider's behaviour for all
    three.  The order of the tuple is part of the contract -- the first hit wins.

    ``normalize_token`` splits one blob into ``(token, session_token)``.
    ``prompt`` and ``env_get`` exist for testability; both default to ``None``
    and are resolved inside the body (see ``_read_stdin`` / ``_environ_get``
    for why the defaults are not written inline).
    """
    if env_get is None:
        env_get = _environ_get
    if prompt is None:
        prompt = _read_stdin

    for env_name in env_names:
        value = env_get(env_name).strip()
        if value:
            log(f"使用环境变量 {env_name}")
            token, session_token = normalize_token(value)
            env_session = env_get(SESSION_TOKEN_ENV).strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return (token, env_session or session_token)

    candidates = [token_file]
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "utf-8", "ascii"):
            try:
                text = raw.decode(enc).strip()
                break
            except UnicodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            log("使用 token 文件")
            token, session_token = normalize_token(text)
            env_session = env_get(SESSION_TOKEN_ENV).strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return (token, env_session or session_token)

    token = prompt("请输入 access_token: ").strip()
    session_token = env_get(SESSION_TOKEN_ENV).strip()
    token, parsed_session = normalize_token(token)
    return (token, session_token or parsed_session)

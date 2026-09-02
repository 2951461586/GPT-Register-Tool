"""Guard against HTTP calls that can hang forever.

Why this exists
---------------
An audit flagged "`account_2fa.py` has 9 HTTP calls with no timeout". That turned
out to be a FALSE NEGATIVE of the grep: the calls go through curl_cffi, whose
``Session`` defaults to ``timeout=30`` (verified against a local hanging server:
``Session(timeout=2.0).get(url, impersonate=...)`` raised after 2.01 s). Grep for
a missing ``timeout=`` keyword cannot see a default applied inside the library.

What actually matters is the CLIENT FAMILY, not the call site:

    curl_cffi.requests.Session   default timeout = 30 s   (safe)
    httpx                        default timeout =  5 s   (safe)
    requests.Session / requests  NO default               (hangs forever)
    urllib.request.urlopen       NO default               (hangs forever)

So a missing ``timeout=`` is only dangerous on the last two families, or when a
curl_cffi session is explicitly built with ``timeout=None`` (which switches the
30 s default off and is the one way to make curl_cffi hang forever).

This test fails on exactly those cases, and additionally ratchets the number of
``**kwargs``-mediated call sites (statically opaque - the timeout may or may not
be in the dict) so a newly added one gets a human look instead of slipping by.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"dist", "runtime", ".venv", ".git", "__pycache__", "tests",
             ".dotnet", "node_modules", "logs", "sessions"}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "request", "urlopen"}

# module -> (family, has a library-level default timeout?)
FAMILY_BY_MODULE = {
    "curl_requests": ("curl_cffi", True),
    "curl_cffi": ("curl_cffi", True),
    "CurlSession": ("curl_cffi", True),
    "_CffiSession": ("curl_cffi", True),
    "requests": ("requests", False),
    "_requests": ("requests", False),
    "http_requests": ("requests", False),
    "httpx": ("httpx", True),
    "urllib.request": ("urllib", False),
}

# Statically opaque sites on the no-default families (requests / urllib): the
# timeout may or may not be inside the splatted dict. Count was 5 on
# 2026-09-01 (3x momo_qr_extract, 2x mailbox_remail) and every one of them does
# carry a timeout. Keep the ceiling tight so a new one forces a human look.
MAX_SPLATTED_SITES = 6


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _family_map(tree: ast.AST):
    """Map local alias -> (family, has_default) from the import statements."""
    aliases: dict[str, tuple[str, bool]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for module, family in FAMILY_BY_MODULE.items():
                    if alias.name == module or alias.name.startswith(module + "."):
                        local = alias.asname or alias.name.split(".")[0]
                        aliases[local] = family
        elif isinstance(node, ast.ImportFrom) and node.module:
            for module, family in FAMILY_BY_MODULE.items():
                if node.module == module or node.module.startswith(module + "."):
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = family
    return aliases


def _call_sites():
    """Yield (relative path, line number, description) for every HTTP call."""
    for path in _python_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        relative = str(path.relative_to(ROOT))
        aliases = _family_map(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "Session":
                owner = node.func.value
                owner_name = owner.id if isinstance(owner, ast.Name) else ""
                family = aliases.get(owner_name)
                # curl_cffi's 30 s safety net is disabled by timeout=None.
                if family == ("curl_cffi", True):
                    for keyword in node.keywords:
                        if keyword.arg == "timeout" and isinstance(keyword.value, ast.Constant) \
                                and keyword.value.value is None:
                            yield (relative, node.lineno,
                                   "curl_cffi Session(timeout=None) disables the 30 s default")

            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in HTTP_METHODS:
                continue
            owner = node.func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else ""
            family = aliases.get(owner_name)
            if family is None:
                continue
            name, has_default = family
            if has_default:
                continue

            keywords = node.keywords
            explicit = any(kw.arg == "timeout" for kw in keywords)
            splatted = any(kw.arg is None for kw in keywords)
            if explicit:
                continue
            if splatted:
                yield (relative, node.lineno, f"{owner_name}.{node.func.attr} via **kwargs")
            else:
                yield (relative, node.lineno,
                       f"{owner_name}.{node.func.attr} has NO timeout and {name} has no default")


def test_no_http_call_can_hang_forever():
    offenders = [(path, line, note) for path, line, note in _call_sites()
                 if not note.endswith("via **kwargs")]
    assert not offenders, (
        "HTTP calls with no timeout on a client that has no library default "
        "(requests / urllib hang forever; curl_cffi is safe at 30 s):\n"
        + "\n".join(f"  {path}:{line}  {note}" for path, line, note in offenders)
    )


def test_kwargs_mediated_timeouts_are_audited():
    splatted = [(path, line) for path, line, note in _call_sites()
                if note.endswith("via **kwargs")]
    assert len(splatted) <= MAX_SPLATTED_SITES, (
        f"{len(splatted)} HTTP call sites pass their arguments through **kwargs, "
        f"above the audited ceiling of {MAX_SPLATTED_SITES}. A static check cannot "
        "see whether 'timeout' is in those dicts - inspect each new site, confirm "
        "it passes a timeout, then raise MAX_SPLATTED_SITES. Sites:\n"
        + "\n".join(f"  {path}:{line}" for path, line in splatted)
    )

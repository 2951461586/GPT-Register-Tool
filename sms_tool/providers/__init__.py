"""Mailbox provider implementations.

Provider implementations live in this package. The top-level ``mailbox_*``
modules are compatibility facades for existing callers.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Pyright resolves `__all__` names statically, so it needs to see the symbols;
    # a module `__getattr__` alone does not satisfy `reportUnsupportedDunderAll`.
    # This block never runs -- the same idiom `registration/__init__.py` uses.
    from . import (  # noqa: F401
        mailbox_cfworker,
        mailbox_gmail,
        mailbox_graph,
        mailbox_icloud_url,
        mailbox_remail,
        mailbox_smailr,
        outlook_imap_client,
    )

__all__ = [
    "mailbox_cfworker",
    "mailbox_gmail",
    "mailbox_graph",
    "mailbox_icloud_url",
    "mailbox_remail",
    "mailbox_smailr",
    "outlook_imap_client",
]

#: Immutable snapshot of the public surface, taken once at import. ``__getattr__``
#: checks this and not ``__all__``, so ``p.__all__.append(...)`` -- which any caller
#: can do to the list -- cannot widen what reaches ``import_module``. Written as a
#: literal above because pyright resolves ``__all__`` statically and reports a
#: computed assignment (``reportUnsupportedDunderAll``).
_PROVIDER_MODULES = tuple(__all__)


def __getattr__(name: str) -> Any:
    """Resolve a provider submodule on first access (PEP 562).

    ``__all__`` names submodules, not symbols, so the package has to answer for
    them. Without this the declaration was decorative: ``from sms_tool.providers
    import mailbox_gmail`` only worked because the import system falls back to a
    submodule lookup, so an ``__all__`` entry could name a module that no longer
    existed and nothing noticed -- pyright reported all seven as missing
    attributes (``reportUnsupportedDunderAll``) while ``import *`` silently
    exported nothing.

    Resolving here keeps the declaration truthful without paying for it: the
    package still imports zero provider modules, so a caller that only wants
    ``ports`` does not pull the mailbox stack in.
    """
    if name in _PROVIDER_MODULES:
        # `name` comes from the `_PROVIDER_MODULES` tuple above, never from caller input.
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

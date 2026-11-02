"""The one two-sided boolean parse behind every ``registration.*`` A/B toggle.

Why this module exists
----------------------
Two copies of this parse used to exist: ``registration/flags.py`` and, verbatim,
``auth_flow/steps.py::_registration_flag``. The second was a deliberate copy --
the package could not import ``sms_tool.registration`` (or that module) without
growing an edge ``scripts/import_layer_ratchet.py`` forbids -- and the two
docstrings said so ("kept textually identical so their semantics cannot
diverge"). A leaf module in ``kernel/`` removes the reason for the copy: both
sides import the same four lines, so they cannot drift.

Why the parse is two-sided
--------------------------
The sharded production config freezes every section into a ``MappingProxyType``
(``_freeze`` in ``sms_tool/config.py``), which is a ``Mapping`` but **not** a
``dict``: a ``dict`` guard reads as "section missing" in production while plain
``dict`` test fixtures stay green. That is the 2026-10-07 P1-D class, where three
toggles silently returned their default in every real run.

``default`` answers both a **missing** key and an **unrecognised** value. A
default-on toggle therefore stays truthy for an unreadable value and a
default-off toggle stays falsy, which is what the hand-written bodies did before
they were consolidated and what a naive ``bool(value)`` rewrite breaks. The
2026-10-10 scan also found ``sentinel_password_bundle`` failing **open** (an
unrecognised value turned a default-off switch on); returning ``default`` is what
fixed it.

Owner
-----
Leaf: imports nothing from ``sms_tool``, so any layer may use it. Callers keep
their own names (``registration.flags.registration_flag``,
``auth_flow.steps._registration_flag``) so patch targets and doc references stay
valid.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Values that mean False / True when a config leaf arrives as a string. Shared
#: so a JSON/YAML string never changes meaning per call site.
FALSY_FLAG_VALUES = (False, 0, "0", "false", "False", "no", "No", "off")
TRUTHY_FLAG_VALUES = (True, 1, "1", "true", "True", "yes", "Yes", "on")


def coerce_flag(value: Any, default: bool) -> bool:
    """Parse one config leaf as a boolean; ``default`` answers anything unknown."""
    if value in FALSY_FLAG_VALUES:
        return False
    if value in TRUTHY_FLAG_VALUES:
        return True
    return default


def section_flag(config: Any, section: str, key: str, default: bool) -> bool:
    """Read ``config[section][key]`` as a boolean, ``Mapping``-safe.

    ``config`` and the section may be any ``Mapping`` (including the frozen
    ``mappingproxy`` the runtime config uses); anything else reads as ``default``.
    """
    section_data = config.get(section) if isinstance(config, Mapping) else None
    if not isinstance(section_data, Mapping):
        return default
    return coerce_flag(section_data.get(key, default), default)


__all__ = ["FALSY_FLAG_VALUES", "TRUTHY_FLAG_VALUES", "coerce_flag", "section_flag"]

"""Single owner for reading ``registration.*`` boolean toggles from a config.

Why this module exists
----------------------
Every ``registration.*`` A/B switch has to read a boolean out of the **frozen**
config.  The sharded production config turns every section into a
``MappingProxyType`` (``_freeze`` in ``sms_tool/config.py``), which is a
``Mapping`` but **not** a ``dict``; a ``dict`` guard reads as "section missing"
in production while plain-``dict`` test fixtures stay green.  That is the
2026-10-07 P1-D class: three registration toggles silently read their default in
every production run.

The parser is deliberately **two-sided**: ``default`` answers both a missing key
and an unrecognised value.  A default-**on** toggle therefore stays truthy for an
unreadable value and a default-**off** toggle stays falsy -- what the hand-written
bodies did before consolidation, and what a naive ``bool(value)`` rewrite breaks.

Owner
-----
This module is a leaf: it imports nothing from ``sms_tool`` (only ``typing``), so
every registration module may import it without adding a cross-directory edge to
``scripts/import_layer_ratchet.py``.  It lives at the top level rather than inside
``auth_flow.steps`` because the callers below sit directly in ``sms_tool/`` and
must not depend on the ``auth_flow`` package.
``auth_flow.steps._registration_flag`` keeps its own copy on purpose: importing
this module from there would add an ``sms_tool/auth_flow -> sms_tool`` module-level
edge, and the ratchet forbids that growth.  The two bodies are kept textually
identical so their semantics cannot diverge.

Consumers (2026-10-10 scan P1-C′ consolidation)
-----------------------------------------------
``registration_protocol_helpers`` (re-export, for existing callers),
``registration_otp_stages``, ``registration_sentinel_stages``,
``registration_edge_challenge`` and ``registration_handlers``.  Before this
module the four non-``auth_flow`` A/B toggles each hand-rolled the parse with
three different unknown-value semantics; ``sentinel_password_bundle`` even
failed **open** (an unrecognised value turned a default-off switch on).
"""

from __future__ import annotations

from typing import Any, Mapping

#: Values that mean False / True when a config leaf arrives as a string.  Shared
#: by the registration flag readers so a YAML/JSON string never changes meaning
#: per call site.
_FALSY_FLAG_VALUES = (False, 0, "0", "false", "False", "no", "No", "off")
_TRUTHY_FLAG_VALUES = (True, 1, "1", "true", "True", "yes", "Yes", "on")


def registration_flag(config: Any, key: str, default: bool) -> bool:
    """Read one ``registration.<key>`` boolean toggle from a frozen config.

    Pure, and ``Mapping``-safe: the sharded production config freezes every
    section into a ``mappingproxy`` (``_freeze`` in ``sms_tool/config.py``),
    which is **not** a ``dict``.  A ``dict`` check made three of these toggles
    read their default in every production run while the tests, which pass plain
    dicts, stayed green (2026-10-07 scan P1-D).

    ``default`` answers both a missing key and an unrecognised value, which is
    the two-sided contract ``auth_flow.steps._registration_flag`` states: a
    default-**on** toggle stays truthy for an unreadable value, a default-**off**
    toggle stays falsy.  Returning ``default`` here reproduces that exactly.
    """
    section = config.get("registration") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return default
    value = section.get(key, default)
    if value in _FALSY_FLAG_VALUES:
        return False
    if value in _TRUTHY_FLAG_VALUES:
        return True
    return default


__all__ = ["registration_flag"]

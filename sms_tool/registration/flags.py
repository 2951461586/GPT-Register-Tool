"""``registration.<key>`` boolean toggles: the name callers know, one parse.

The parse itself lives in :mod:`sms_tool.kernel.flags`. It used to live here
**and** in ``auth_flow.steps``, as two textually identical copies, because the
``auth_flow`` package could not import this module without growing an edge
``scripts/import_layer_ratchet.py`` forbids. Moving the four shared lines to the
``kernel`` leaf removed the reason for the copy: both names now delegate to one
implementation.

This module keeps its name and signature so existing importers
(``registration_otp_stages``, ``registration_sentinel_stages``,
``registration_edge_challenge``, ``registration_handlers``,
``registration_protocol_helpers``) and patch targets keep working.
"""

from __future__ import annotations

from typing import Any

from ..kernel.flags import section_flag


def registration_flag(config: Any, key: str, default: bool) -> bool:
    """Read one ``registration.<key>`` boolean toggle from a frozen config.

    ``Mapping``-safe and two-sided: ``default`` answers both a missing key and an
    unrecognised value, so a default-on toggle stays truthy and a default-off
    toggle stays falsy for anything unreadable. See
    :func:`sms_tool.kernel.flags.coerce_flag` for why that matters (the frozen
    ``mappingproxy`` and the 2026-10-10 fail-open fix).

    Every key read through here is pinned to ``scripts/experiment_ledger.json``
    by ``scripts/experiment_ratchet.py``: a flag nobody registered is a decision
    nobody recorded.
    """
    return section_flag(config, "registration", key, default)


__all__ = ["registration_flag"]

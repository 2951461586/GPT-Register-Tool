"""Single operator-output seam: one call feeds both channels from one source.

Why this exists
---------------
The protocol lane emits operator-facing lines through bare ``print()`` while
diagnostic events go through ``logging``.  The two channels are *not* fed from
the same source: ``registration_handlers`` alone mixes 40 ``print`` calls with
``_LOGGER.info`` calls, so a run''s evidence is split across ``sms_tool.log``
(logging records only) and ``backend_stdout.jsonl`` (the stdout mirror) with no
guarantee that a fact printed on one channel exists on the other.

``SanitizingTextIO`` + ``StdoutMirror`` already guarantee that a ``print`` is
sanitised and mirrored to disk -- so this module does NOT reroute output.  What
it adds is the missing half: the same call *also* emits a logging record, so
the fact survives on the structured channel with its ``extra`` fields intact.

Use :func:`emit` for new operator-facing lines.  Existing ``print`` calls are
deliberately left in place -- they work, they are mirrored, and rewriting 500+
call sites would only add risk.  The companion ratchet
(``scripts/bare_print_ratchet.py``) freezes the current count so the split
stops growing while new code goes through this seam.
"""

from __future__ import annotations

import logging
from typing import Any

from .diagnostics import safe_print


def emit(logger: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    """Emit one operator-visible line to stdout AND one logging record.

    ``safe_print`` sanitises and mirrors the line to the desktop IPC channel;
    ``logger.info`` writes the same text (formatted with ``args``) to the log
    file with any ``extra={...}`` fields preserved.  Because both sides are fed
    from the same ``message`` here, the two channels can no longer drift apart
    the way a hand-written ``print`` + separate ``logger`` call could.
    """
    safe_print(message % args if args else message)
    logger.info(message, *args, **kwargs)

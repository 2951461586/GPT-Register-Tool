"""Compatibility shim: the durable operation journal lives in ``operation_journal``.

Why this file still exists
--------------------------
The implementation moved to :mod:`sms_tool.operation_journal` on 2026-10-18
because the registration persistence path uses the same journal as the payment
path, and importing a payment-named module from ``commands/registration`` was
the last direct edge between the two lanes.  Five ``pay_link`` modules plus
their tests import these names, so the file stays as a re-export rather than
forcing an unrelated rename through the payment lane.

Why the names are not renamed here
----------------------------------
``PaymentOperation*`` are the historical API.  Renaming them is a mechanical
follow-up that touches the payment lane only; doing it in the same commit as
the boundary fix would mix an architecture change with an API rename.  See
``operation_journal``'s docstring for the same reasoning and the ``payment_wire``
precedent.
"""

from __future__ import annotations

from .operation_journal import (
    LEDGER_DIR,
    SCHEMA_VERSION,
    PaymentOperation,
    PaymentOperationConflict,
    PaymentOperationStore,
    conflict_result,
)

__all__ = [
    "LEDGER_DIR",
    "SCHEMA_VERSION",
    "PaymentOperation",
    "PaymentOperationConflict",
    "PaymentOperationStore",
    "conflict_result",
]

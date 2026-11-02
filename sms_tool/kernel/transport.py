"""What a transport call can raise, in one place.

Narrowing ``except Exception`` to ``except TRANSPORT_ERRORS`` is the P6-PR-B
recipe. A logic error raised inside the same ``try`` -- a ``KeyError`` from a
re-shaped response, a ``TypeError`` from a header built wrong -- must not be
reclassified as a network failure: that hides the defect and lands in the
funnel's ``unknown`` bucket while the batch keeps spending mailboxes.

Why a separate module instead of a constant in ``kernel.error_classification``:
that module is imported by ``accounts/account_scan`` and ``batch_runner``, which
never touch the wire. Putting the curl import there would charge every liveness
and batch start-up for it. A leaf that only the narrowing sites import pays for
itself, and the kernel boundary explicitly allows third-party imports.
"""

from __future__ import annotations

from curl_cffi.requests.errors import RequestsError

#: ``OSError``/``TimeoutError`` cover socket-level failures curl_cffi lets through
#: unwrapped (a refused connection, a read timeout). Anything else is a defect.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (RequestsError, OSError, TimeoutError)

__all__ = ["TRANSPORT_ERRORS"]

"""Record / replay harness for the UPI protocol stages (stage-3 harness).

Why this exists
---------------
``sms_tool/upi_link/stages.py`` is now 13 ``_stage_*(ops, state)`` functions split
out of the 1138-line ``run_upi_qr_link_once`` (see
``docs/audits/plan-2026-10-03-upi-pipeline-stage-split.md`` §11).  The split was
proven equivalent by AST comparison, but *nothing* executed a single stage body:
the flow was only ever patched out wholesale, so the body carried ~55% statement
coverage and zero stage-level test net.

This module is that test net's transport, deliberately the same shape as
``tests/payment_replay.py`` (which serves ``services/protocol-payment``):

* it **reuses** ``payment_replay``'s ``ReplayCursor`` / ``ReplaySession`` /
  ``RecordingSession`` / ``freeze_determinism`` -- no second transport;
* it installs only the UPI-specific **seam**: the two session factories the
  stage bodies resolve through the ops bundle (``pipeline._new_session`` and
  ``pipeline._upi_new_chatgpt_session``), plus the ambient network edges a
  replay must not hit (geo lookup, edge probe, proxy rotation, the egress gate,
  the browser rail), plus the two non-HTTP side-channels
  (``m.stripe.com`` fingerprint registration, Playwright hCaptcha) via env.

Because ``pipeline._build_upi_operations()`` reads ``pipeline``'s globals **at
call time**, patching the factories on ``pipeline`` is enough: build the ops
bundle *after* entering :func:`upi_replay` and the stage bodies replay.

What it does **not** do: it never rewrites a ``_upi_*`` helper.  A stage that
calls ``ops._upi_stripe_init`` still runs the real function; only its transport
is served from the recording.  That is what makes a slice test evidence about the
stage, not about a mock.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from payment_replay import (  # noqa: E402  (re-exported transport surface)
    RecordedExchange,
    RecordingSession,
    ReplayCursor,
    ReplayExhausted,
    ReplayMismatch,
    ReplayResponse,
    ReplaySession,
    canonical_body,
    canonical_url,
    freeze_determinism,
    load_recording,
    request_fingerprint,
    save_recording,
)

__all__ = [
    # transport (re-exported so a UPI test imports one module)
    "RecordedExchange",
    "RecordingSession",
    "ReplayCursor",
    "ReplayExhausted",
    "ReplayMismatch",
    "ReplayResponse",
    "ReplaySession",
    "canonical_body",
    "canonical_url",
    "freeze_determinism",
    "load_recording",
    "request_fingerprint",
    "save_recording",
    # UPI seam
    "UPI_STUB_ENV",
    "UPIReplay",
    "build_state",
    "cursor_from",
    "install_upi_recording",
    "install_upi_replay",
    "run_stage",
    "synthetic_exchange",
    "upi_replay",
]

#: Environment that switches off the side-channels a pure-HTTP replay cannot
#: serve.  ``UPI_STRIPE_FINGERPRINT=0`` drops the ``m.stripe.com/6`` registration
#: (it is *in addition to* the Stripe init POST, so leaving it on makes every
#: slice two requests); ``UPI_HCAPTCHA_DISABLE=1`` stops the Playwright solver.
UPI_STUB_ENV: dict[str, str] = {
    "UPI_STRIPE_FINGERPRINT": "0",
    "UPI_HCAPTCHA_DISABLE": "1",
    "UPI_DUMP": "0",
    "UPI_INDIA_EXIT_PROBE": "0",
    "UPI_ROUNDS": "1",
}


def _pipeline():
    from sms_tool.upi_link import pipeline

    return pipeline


def _stage_module():
    from sms_tool.upi_link import stages

    return stages


# --------------------------------------------------------------------------
# synthetic exchanges
# --------------------------------------------------------------------------


def synthetic_exchange(
    method: str,
    url: str,
    *,
    status: int = 200,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    response_url: str | None = None,
) -> dict[str, Any]:
    """One ``RecordedExchange``-shaped dict for a hand-authored fixture.

    ``body`` given as a mapping/list is JSON-encoded, matching what the real
    ``save_recording`` stores (a response body is always a string there).
    """
    if isinstance(body, (dict, list)):
        body_text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    else:
        body_text = str(body or "")
    return {
        "request": {"method": method.upper(), "url": url},
        "response": {
            "status_code": int(status),
            "headers": dict(headers or {"content-type": "application/json"}),
            "body": body_text,
            "url": response_url or url,
        },
    }


def cursor_from(exchanges: Iterable[Any], *, strict: bool = True) -> ReplayCursor:
    """Build a cursor from dicts or ``RecordedExchange`` objects."""
    items = [e if isinstance(e, RecordedExchange) else RecordedExchange.from_dict(e) for e in exchanges]
    return ReplayCursor(items, strict=strict)


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------


@contextlib.contextmanager
def install_upi_replay(
    cursor: ReplayCursor,
    *,
    geo_country: str = "IN",
    edge_status: int = 200,
    edge_blocked: bool = False,
    egress_failure: Mapping[str, Any] | None = None,
):
    """Patch ``pipeline``'s ambient edges so the ops bundle replays.

    Every patch target is a name ``pipeline._build_upi_operations()`` copies into
    the bundle, so the patched object is what the stage bodies actually call.
    """
    pipeline = _pipeline()

    def _stripe_session(*_args: Any, **_kwargs: Any) -> ReplaySession:
        return ReplaySession(cursor)

    def _chatgpt_session(*_args: Any, **_kwargs: Any) -> ReplaySession:
        return ReplaySession(cursor)

    def _geo(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(country=geo_country)

    def _edge(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(http_status=edge_status, blocked_by_cloudflare=edge_blocked)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(pipeline, "_new_session", _stripe_session))
        stack.enter_context(patch.object(pipeline, "_upi_new_chatgpt_session", _chatgpt_session))
        stack.enter_context(patch.object(pipeline, "resolve_proxy_geo", _geo))
        stack.enter_context(patch.object(pipeline, "probe_openai_edge", _edge))
        stack.enter_context(patch.object(pipeline, "rotate_session", lambda proxy, country: proxy))
        stack.enter_context(patch.object(pipeline, "_upi_assert_egress_contract", lambda _rt: egress_failure))
        yield pipeline


@contextlib.contextmanager
def install_upi_recording(sink: list[dict[str, Any]]):
    """Wrap the two UPI session factories so they append full exchanges."""
    pipeline = _pipeline()
    real_stripe = pipeline._new_session
    real_chatgpt = pipeline._upi_new_chatgpt_session

    def _stripe(*args: Any, **kwargs: Any) -> RecordingSession:
        return RecordingSession(real_stripe(*args, **kwargs), sink)

    def _chatgpt(*args: Any, **kwargs: Any) -> RecordingSession:
        return RecordingSession(real_chatgpt(*args, **kwargs), sink)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(pipeline, "_new_session", _stripe))
        stack.enter_context(patch.object(pipeline, "_upi_new_chatgpt_session", _chatgpt))
        yield pipeline


@dataclass
class UPIReplay:
    """Handle yielded by :func:`upi_replay` (already patched and frozen)."""

    pipeline: Any
    stages: Any
    cursor: ReplayCursor
    ops: Any
    events: list[tuple[str, str]] = field(default_factory=list)

    def build_state(self, **overrides: Any):
        return build_state(emit=self._record, events=self.events, **overrides)

    def _record(self, stage: str, message: str) -> None:
        self.events.append((stage, message))

    def run_stage(self, name: str, state: Any) -> Any:
        return run_stage(name, self.ops, state, stages=self.stages)


@contextlib.contextmanager
def upi_replay(cursor: ReplayCursor, *, env: Mapping[str, str] | None = None, **seam: Any) -> Iterator[UPIReplay]:
    """Enter a frozen, patched UPI replay session.

    ``ops`` is built **inside** the patched context, which is the whole point:
    the bundle snapshots the patched factories, so a stage body built from it
    replays instead of dialing out.
    """
    pipeline = _pipeline()
    stages = _stage_module()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {**UPI_STUB_ENV, **dict(env or {})}, clear=False))
        stack.enter_context(freeze_determinism())
        stack.enter_context(install_upi_replay(cursor, **seam))
        ops = pipeline._build_upi_operations()
        yield UPIReplay(pipeline=pipeline, stages=stages, cursor=cursor, ops=ops)


def run_stage(name: str, ops: Any, state: Any, *, stages: Any = None) -> Any:
    """Run one ``_stage_*`` function; ``name`` may be short (``stripe_init``)."""
    stages = stages or _stage_module()
    func = getattr(stages, name if name.startswith("_stage_") else f"_stage_{name}")
    return func(ops, state)


# --------------------------------------------------------------------------
# state construction
# --------------------------------------------------------------------------


def build_state(
    *,
    emit: Callable[[str, str], None] | None = None,
    events: list[tuple[str, str]] | None = None,
    **overrides: Any,
):
    """A minimally-populated ``UpiStageContext`` for one stage slice.

    Only the fields a slice actually reads are pre-set; anything else left unset
    raises ``AttributeError`` on read, which is the intended signal that the slice
    reached for state the harness did not declare.
    """
    from sms_tool.upi_link.context import UpiStageContext

    sink = events if events is not None else []
    state = UpiStageContext()
    state.emit = emit or (lambda stage, message: sink.append((stage, message)))
    # identity / transport
    state.access_token = "at_replay"
    state.proxy = ""
    state.checkout_proxy = "http://checkout.example:8080"
    state.provider_proxy = "http://provider.example:8080"
    state.approve_proxy = "http://approve.example:8080"
    state.device_id = "did_replay"
    state.session_token = ""
    state.fingerprint = {
        "name": "replay",
        "user_agent": "UA/1.0",
        "accept_language": "en-US,en;q=0.9",
        "sec_ch_ua": '"Chromium";v="146"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "locale": "en-US",
        "elements_locale": "en",
    }
    state.risk = SimpleNamespace(stripe_ids={})
    # checkout handle
    state.cs_id = "cs_replay_1"
    state.cs = "cs_replay"
    state.stripe_pk = "pk_test_replay"
    state.checkout_country = "IN"
    state.payment_country = "IN"
    state.target_country = "IN"
    state.currency = "inr"
    state.payment_currency = "INR"
    state.require_zero = True
    state.runtime_config = {}
    state.qr_path = ""
    state.proxy_state = None
    # flow switches (match the production defaults)
    state.is_oaics = False
    state.require_server_upi_mandate = False
    state.update_tax_region = False
    state.update_customer_data = False
    state.wait_paid = False
    state.paid_timeout = 0.0
    state.local_mandate_enabled = False
    state.browser_rail = False
    # runtime bundle + resolved config the pre-exit / later stages read
    state.upi_cfg = {}
    state._rc = SimpleNamespace(
        checkout_proxy=state.checkout_proxy,
        provider_proxy=state.provider_proxy,
        approve_proxy=state.approve_proxy,
    )
    # artifacts a downstream slice may read (init from Stage 2, qr/redirect from 7)
    state.init = {}
    state.qr_data = {}
    state.qr_data_str = ""
    state.redirect_url = ""
    state.hosted_url = ""
    state.upi_uri = ""
    state.written_qr_path = ""
    state.expires_at = ""
    state.link_type = "upi_link"
    state.processor_entity = "openai_ie"
    state.amount = 0
    state.pm_types = []
    state.ft_status = {
        "due": 0,
        "has_free_trial": True,
        "has_upi": True,
        "coupon_name": "plus-1-month-free",
        "percent_off": 100,
        "payment_method_types": [],
    }
    state.approval_ok = False
    state.approval_blocked = False
    state.paid_state = {"paid": False, "payment_status": "not_requested"}
    state.checkout_ui_mode = "custom"
    for key, value in overrides.items():
        setattr(state, key, value)
    return state, sink

#!/usr/bin/env python3
"""Segmented gRPC wrapper for the GoPay payment flow."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import threading
import time
import uuid
from concurrent import futures
from dataclasses import dataclass
from typing import Any

import grpc
import requests

import payment_pb2
import payment_pb2_grpc
from gopay import (
    DEFAULT_STRIPE_PK,
    GoPayCharger,
    GoPayError,
    OTPCancelled,
    _build_chatgpt_session,
    _load_cfg,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _billing_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    billing = cfg.get("billing") or {}
    if billing:
        return dict(billing)
    cards = cfg.get("cards") or []
    if cards and isinstance(cards[0], dict):
        card0 = cards[0]
        out = dict(card0.get("address") or {})
        for key in ("name", "email"):
            if card0.get(key):
                out[key] = card0[key]
        return out
    return {}


def _normalize_listen(value: str) -> str:
    value = (value or ":50051").strip()
    if value.startswith(":"):
        return "[::]" + value
    return value


def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _unlink_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    gopay_cfg = cfg.get("gopay") or {}
    if not isinstance(gopay_cfg, dict):
        return {}
    raw = gopay_cfg.get("unlink") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _configured_unlink_url(cfg: dict[str, Any], request_url: str = "") -> str:
    if request_url:
        return request_url.strip()
    gopay_cfg = cfg.get("gopay") or {}
    if not isinstance(gopay_cfg, dict):
        gopay_cfg = {}
    unlink_cfg = _unlink_cfg(cfg)
    otp_cfg = gopay_cfg.get("otp") or {}
    if not isinstance(otp_cfg, dict):
        otp_cfg = {}
    return str(
        unlink_cfg.get("url")
        or unlink_cfg.get("adb_url")
        or unlink_cfg.get("termux_url")
        or otp_cfg.get("adb_url")
        or otp_cfg.get("termux_url")
        or os.getenv("GOPAY_UNLINK_URL", "").strip()
        or os.getenv("GOPAY_ADB_URL", "").strip()
        or os.getenv("GOPAY_TERMUX_URL", "").strip()
        or ""
    ).strip()


def _unlink_after_success_enabled(cfg: dict[str, Any]) -> bool:
    unlink_cfg = _unlink_cfg(cfg)
    return _as_bool(unlink_cfg.get("enabled")) or _as_bool(unlink_cfg.get("run_after_success"))


def _trigger_gopay_unlink(cfg: dict[str, Any], *, request_url: str = "", timeout_seconds: int = 0) -> dict[str, Any]:
    url = _configured_unlink_url(cfg, request_url=request_url)
    if not url:
        raise GoPayError("gopay unlink requires gopay.unlink.url, gopay.unlink.adb_url, or GOPAY_UNLINK_URL")
    unlink_cfg = _unlink_cfg(cfg)
    timeout = int(timeout_seconds or unlink_cfg.get("timeout_seconds") or unlink_cfg.get("timeout") or 120)
    url = url.rstrip("/")
    logger.info("[payment] triggering GoPay unlink via %s", url)
    resp = requests.post(
        f"{url}/gopay/unlink",
        timeout=max(5, timeout),
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        data = {"status": "unknown", "raw": resp.text[:1000]}
    if not isinstance(data, dict):
        data = {"status": "unknown", "raw": data}
    return data


@dataclass
class PendingFlow:
    charger: GoPayCharger
    state: dict[str, Any]
    expires_at: float

    def close(self) -> None:
        self.charger.close()


class FlowStore:
    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.Lock()
        self._flows: dict[str, PendingFlow] = {}
        self._closed = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="payment-flow-reaper", daemon=True)
        self._reaper.start()

    def put(self, charger: GoPayCharger, state: dict[str, Any]) -> tuple[str, int]:
        flow_id = uuid.uuid4().hex
        expires_at = time.time() + self._ttl_seconds
        with self._lock:
            self._flows[flow_id] = PendingFlow(charger=charger, state=state, expires_at=expires_at)
        return flow_id, int(expires_at)

    def pop(self, flow_id: str) -> PendingFlow | None:
        with self._lock:
            return self._flows.pop(flow_id, None)

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            flows = list(self._flows.values())
            self._flows.clear()
        for flow in flows:
            flow.close()

    def _reap_loop(self) -> None:
        while not self._closed.wait(30):
            now = time.time()
            expired: list[PendingFlow] = []
            with self._lock:
                for flow_id, flow in list(self._flows.items()):
                    if flow.expires_at <= now:
                        expired.append(self._flows.pop(flow_id))
            for flow in expired:
                logger.info("[payment] closing expired flow")
                flow.close()


class PaymentService(payment_pb2_grpc.PaymentServiceServicer):
    def __init__(self, cfg: dict[str, Any], flow_ttl_seconds: int):
        self._cfg = cfg
        self._flows = FlowStore(flow_ttl_seconds)

    def close(self) -> None:
        self._flows.close()

    def StartGoPay(self, request, context):
        credential = getattr(request, "credential", None)
        session_token = str(getattr(credential, "session_token", "") or "").strip()
        access_token = str(getattr(credential, "access_token", "") or "").strip()
        if not session_token:
            return payment_pb2.StartGoPayResponse(
                success=False,
                error_message="session_token is required",
            )

        charger = None
        cs_session = None
        try:
            cfg = copy.deepcopy(self._cfg)
            fresh_checkout = cfg.get("fresh_checkout") or {}
            auth_cfg = dict(fresh_checkout.get("auth") or {})
            auth_cfg["session_token"] = session_token
            if access_token:
                auth_cfg["access_token"] = access_token
            else:
                auth_cfg.pop("access_token", None)
            auth_cfg.pop("cookie_header", None)

            cs_session = _build_chatgpt_session(auth_cfg)

            gopay_cfg = dict(cfg.get("gopay") or {})
            if request.gopay_country_code:
                gopay_cfg["country_code"] = request.gopay_country_code
            if request.gopay_phone:
                gopay_cfg["phone_number"] = request.gopay_phone
            if request.tokenization:
                gopay_cfg["tokenization"] = request.tokenization

            stripe_pk = (
                (cfg.get("stripe") or {}).get("publishable_key")
                or auth_cfg.get("stripe_pk")
                or DEFAULT_STRIPE_PK
            )
            runtime_cfg = dict(cfg.get("runtime") or {})
            proxy = request.proxy_url or (cfg.get("proxy") or "").strip() or None
            charger = GoPayCharger(
                cs_session,
                gopay_cfg,
                otp_provider=lambda: (_ for _ in ()).throw(OTPCancelled("external OTP required")),
                proxy=proxy,
                runtime_cfg=runtime_cfg,
                log=logger.info,
            )

            logger.info("[payment] StartGoPay start")
            state = charger.start_until_otp(stripe_pk=stripe_pk, billing=_billing_from_config(cfg))
            flow_id, expires_at = self._flows.put(charger, state)
            charger = None
            cs_session = None
            logger.info("[payment] StartGoPay waiting_otp flow=%s", flow_id[:8])
            return payment_pb2.StartGoPayResponse(
                success=True,
                flow_id=flow_id,
                snap_token=str(state.get("snap_token") or ""),
                issued_after_unix=int(state.get("issued_after_unix") or 0),
                expires_at_unix=expires_at,
                checkout_url=str(state.get("checkout_url") or ""),
                checkout_session_id=str(state.get("cs_id") or state.get("checkout_session_id") or ""),
                otp_required=True,
            )
        except GoPayError as exc:
            logger.error("[payment] StartGoPay failed: %s", exc)
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] StartGoPay crashed")
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            if charger is not None:
                charger.close()
            elif cs_session is not None:
                _close_session(cs_session)

    def CompleteGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.GoPayResponse(success=False, error_message="flow_id is required")
        if not request.otp:
            return payment_pb2.GoPayResponse(success=False, error_message="otp is required")

        flow = self._flows.pop(request.flow_id)
        if flow is None:
            return payment_pb2.GoPayResponse(success=False, error_message="payment flow not found or expired")

        try:
            logger.info("[payment] CompleteGoPay flow=%s", request.flow_id[:8])
            if request.pin:
                flow.charger.pin = request.pin
            result = flow.charger.complete_after_otp(flow.state, request.otp)
            state = str(result.get("state") or "")
            success = state == "succeeded"
            unlink_status = ""
            unlink_error = ""
            if success and _unlink_after_success_enabled(self._cfg):
                try:
                    unlink_result = _trigger_gopay_unlink(self._cfg)
                    unlink_status = str(unlink_result.get("status") or ("ok" if unlink_result.get("ok") else ""))
                except Exception as exc:
                    unlink_error = str(exc)[:500]
                    logger.error("[payment] GoPay unlink after success failed: %s", exc)
            return payment_pb2.GoPayResponse(
                success=success,
                error_message="" if success else f"payment state={state or 'unknown'}",
                charge_ref=str(result.get("charge_ref") or ""),
                snap_token=str(result.get("snap_token") or ""),
                unlink_status=unlink_status,
                unlink_error=unlink_error,
            )
        except GoPayError as exc:
            logger.error("[payment] CompleteGoPay failed: %s", exc)
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] CompleteGoPay crashed")
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            flow.close()

    def CancelGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.CancelGoPayResponse(success=False, error_message="flow_id is required")
        flow = self._flows.pop(request.flow_id)
        if flow is not None:
            flow.close()
        logger.info("[payment] CancelGoPay flow=%s found=%s", request.flow_id[:8], flow is not None)
        return payment_pb2.CancelGoPayResponse(success=True)

    def UnlinkGoPay(self, request, context):
        try:
            result = _trigger_gopay_unlink(
                self._cfg,
                request_url=str(request.url or ""),
                timeout_seconds=int(request.timeout_seconds or 0),
            )
            status = str(result.get("status") or ("ok" if result.get("ok") else ""))
            ok = bool(result.get("ok")) or status == "ok"
            return payment_pb2.UnlinkGoPayResponse(
                success=ok,
                error_message="" if ok else str(result.get("error") or result)[:500],
                status=status,
                raw_json=json.dumps(result, ensure_ascii=False)[:4000],
            )
        except Exception as exc:
            logger.error("[payment] UnlinkGoPay failed: %s", exc)
            return payment_pb2.UnlinkGoPayResponse(
                success=False,
                error_message=str(exc)[:500],
            )


def serve(config_path: str, listen: str, flow_ttl_seconds: int):
    cfg = _load_cfg(config_path)
    service = PaymentService(cfg, flow_ttl_seconds=flow_ttl_seconds)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )
    payment_pb2_grpc.add_PaymentServiceServicer_to_server(service, server)
    listen_addr = _normalize_listen(listen)
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info("[payment] gRPC listening on %s flow_ttl=%ss", listen_addr, flow_ttl_seconds)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("[payment] shutting down")
        server.stop(grace=5)
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(description="GoPay payment gRPC service")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--listen", default=":50051")
    parser.add_argument("--flow-ttl", type=int, default=240)
    args = parser.parse_args()

    serve(
        config_path=args.config,
        listen=args.listen,
        flow_ttl_seconds=args.flow_ttl,
    )


if __name__ == "__main__":
    main()

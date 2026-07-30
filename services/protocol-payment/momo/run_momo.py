#!/usr/bin/env python3
"""Thin runner: MoMo scannable-QR extractor for the unified payment-link manager.

Wraps :func:`momo_qr_extract.probe_token_blob` so the manager can drive the MoMo
flow the same way it drives ``pix/run_pix.py``: hand in one access token plus one
VN exit proxy, receive a single normalized JSON object on stdout. A ``data:image``
QR is decoded to a PNG under ``--qr-out-dir`` so the desktop UI can open it, and the
base64 blob is kept out of stdout to avoid flooding the caller.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

# allow running from this directory (imports momo_qr_extract + ac_paylink_core)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import momo_qr_extract as momo


_DATA_URI_RE = re.compile(
    r"^data:image/(?P<ext>png|jpe?g|gif|webp);base64,(?P<b64>[A-Za-z0-9+/=]+)$"
)


def _decode_qr_to_file(data_uri: str, out_dir: str) -> str:
    match = _DATA_URI_RE.match(str(data_uri or "").strip())
    if not match:
        return ""
    ext = match.group("ext")
    ext = "jpg" if ext == "jpeg" else ext
    try:
        raw = base64.b64decode(match.group("b64"))
    except Exception:
        return ""
    directory = Path(out_dir or (Path(__file__).resolve().parent / "qr"))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"momo_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        path.write_bytes(raw)
    except Exception:
        return ""
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="MoMo scannable-QR extractor (standalone runner)")
    parser.add_argument("--token", default=os.environ.get("MOMO_TOKEN", ""), help="ChatGPT access token or session JSON")
    parser.add_argument("--token-file", default="", help="file containing access token or session JSON")
    parser.add_argument("--proxy", default=os.environ.get("MOMO_PROXY", ""), help="VN exit proxy seed (empty = direct)")
    parser.add_argument("--checkout-proxy", default=os.environ.get("MOMO_CHECKOUT_PROXY", ""), help="ChatGPT checkout proxy")
    parser.add_argument("--promotion-proxy", default=os.environ.get("MOMO_PROMOTION_PROXY", ""), help="ChatGPT checkout/update proxy")
    parser.add_argument("--provider-proxy", default=os.environ.get("MOMO_PROVIDER_PROXY", ""), help="Stripe init/payment-method proxy")
    parser.add_argument("--approve-proxy", default=os.environ.get("MOMO_APPROVE_PROXY", ""), help="ChatGPT approve proxy")
    parser.add_argument("--redirect-proxy", default=os.environ.get("MOMO_REDIRECT_PROXY", ""), help="MoMo/Nicepay redirect proxy")
    parser.add_argument("--strategy", default="custom_promo", choices=["custom_promo", "hosted_promo", "custom_trial"])
    parser.add_argument("--probe-only", action="store_true", help="Stop after checkout eligibility/payment-method probing")
    parser.add_argument("--pre-proxy", default=os.environ.get("MOMO_PRE_PROXY", "off"), help="upstream SOCKS/HTTP proxy; off = disabled")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--max-proxies", type=int, default=1)
    parser.add_argument("--trial-days", type=int, default=30)
    parser.add_argument("--qr-out-dir", default="", help="directory for decoded QR PNG files")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    token = (args.token or "").strip()
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print(json.dumps({"ok": False, "error": "missing access token", "payment_method": "momo"}, ensure_ascii=False))
        return 2

    proxy = (args.proxy or args.checkout_proxy or "").strip()
    stage_proxies = {
        "checkout": (args.checkout_proxy or proxy).strip(),
        "promotion": (args.promotion_proxy or proxy).strip(),
        "provider": (args.provider_proxy or proxy).strip(),
        "approve": (args.approve_proxy or proxy).strip(),
        "redirect": (args.redirect_proxy or proxy).strip(),
    }
    result = momo.probe_token_blob(
        token,
        proxies=[proxy] if proxy else None,
        direct=not proxy,
        trial_days=max(1, args.trial_days),
        max_proxies=max(1, args.max_proxies),
        timeout=max(8, args.timeout),
        emit_qr=not args.probe_only,
        pre_proxy=args.pre_proxy,
        label="A",
        stage_proxies=stage_proxies,
        strategy=args.strategy,
        max_attempts=1,
    )

    decision = str(result.get("decision") or "")
    has_qr = bool(result.get("has_qr"))
    hosted = str(result.get("hosted_instructions_url") or "")
    qr_image = str(result.get("qr_image_url") or result.get("qr_png_url") or "")
    qr_data = str(result.get("qr_data") or "")

    qr_path = _decode_qr_to_file(qr_image, args.qr_out_dir) if qr_image.startswith("data:image") else ""

    # Prefer a scannable gateway URL for display; never echo the base64 data URI.
    display_url = hosted or (qr_data if qr_data.startswith(("http://", "https://")) else "")
    display_qr = "" if qr_data.startswith("data:image") else qr_data

    summary = {
        "ok": bool(
            (has_qr and (hosted or qr_path))
            or (args.probe_only and result.get("credential_valid") is not False and result.get("checkout_status") == "created")
        ),
        "payment_method": "momo",
        "url": display_url,
        "hosted_instructions_url": hosted,
        "qr_data": display_qr or hosted,
        "qr_path": qr_path,
        "has_qr": has_qr,
        "decision": decision,
        "decision_text": str(result.get("decision_text") or ""),
        "amount_due": result.get("amount_due"),
        "currency": result.get("currency") or "VND",
        "methods": result.get("methods"),
        "has_momo": result.get("has_momo"),
        "qr_status": result.get("qr_status"),
        "qr_error": result.get("qr_error"),
        "approve_status": result.get("approve_status"),
        "confirm_status": result.get("confirm_status"),
        "checkout_strategy": result.get("checkout_strategy"),
        "stage_status": result.get("stage_status"),
        "link_type": "momo_protocol_qr",
    }
    if not summary["ok"]:
        summary["error"] = result.get("qr_error") or result.get("decision_text") or decision or "momo QR extraction failed"
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "payment_method": "momo"}, ensure_ascii=False))
        raise SystemExit(1)

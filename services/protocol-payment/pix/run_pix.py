#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# allow running from this directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pix_extract import generate_opll_pix_long_link, proxy_for_region


def main() -> int:
    p = argparse.ArgumentParser(description="PIX long-link extractor (standalone)")
    p.add_argument("--token", default=os.environ.get("OPENAI_ACCESS_TOKEN", ""), help="ChatGPT access token")
    p.add_argument("--token-file", default="", help="file containing access token")
    p.add_argument("--br-proxy", default=os.environ.get("PIX_BR_PROXY", ""), help="BR provider proxy")
    p.add_argument("--vn-proxy", default=os.environ.get("PIX_VN_PROXY", ""), help="VN promotion proxy")
    p.add_argument("--proxy", default=os.environ.get("PIX_PROXY", ""), help="seed proxy (region rewritten to BR/VN)")
    p.add_argument("--out", default="", help="write full JSON result to file")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    token = (args.token or "").strip()
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("missing access token: pass --token / --token-file / OPENAI_ACCESS_TOKEN", file=sys.stderr)
        return 2

    br = (args.br_proxy or "").strip()
    vn = (args.vn_proxy or "").strip()
    seed = (args.proxy or "").strip()
    if seed:
        if "://" not in seed and "@" in seed:
            seed = "http://" + seed
        br = br or proxy_for_region(seed, "BR")
        vn = vn or proxy_for_region(seed, "VN")
    if br and "://" not in br and "@" in br:
        br = "http://" + br
    if vn and "://" not in vn and "@" in vn:
        vn = "http://" + vn

    def logcb(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    result = generate_opll_pix_long_link(
        token,
        country="BR",
        currency="BRL",
        proxy_url=br,
        promotion_proxy_url=vn,
        log_cb=logcb,
    )
    summary = {
        "long_url": result.get("long_url"),
        "pix_hosted_instructions_url": result.get("pix_hosted_instructions_url"),
        "provider_redirect_url": result.get("provider_redirect_url"),
        "pix_qr_code": result.get("pix_qr_code"),
        "pix_qr_image_url_png": result.get("pix_qr_image_url_png"),
        "pix_qr_image_url_svg": result.get("pix_qr_image_url_svg"),
        "stripe_amount": result.get("stripe_amount"),
        "cs_id": result.get("cs_id"),
        "payment_method_id": result.get("payment_method_id"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

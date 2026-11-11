#!/usr/bin/env python3
"""Measure and verify the per-account TLS ClientHello (JA3) override.

The override lives in :mod:`sms_tool.tls_fingerprint`; this script is the
*evidence* half.  It answers three questions the module cannot answer by itself:

1. **What do the presets actually send?**  ``--capture`` prints the live
   extension order and JA3/Akamai hashes per impersonation profile.  Without a
   capture nobody can tell whether ``VERIFIED_EXTENSION_ORDERS`` still matches
   the installed curl_cffi.
2. **Do the accounts differ?**  ``--seeds`` builds the override for several
   seeds and prints the resulting orders — offline, no network.
3. **Does the permuted ClientHello still work?**  ``--live`` performs one real
   request per profile through the configured exit and prints the status.

``--live`` and ``--capture`` need network; everything else is offline and safe
to run in CI.

    python scripts/tls_fingerprint_probe.py
    python scripts/tls_fingerprint_probe.py --seeds 5
    python scripts/tls_fingerprint_probe.py --capture --proxy "$PROXY_URL"
    python scripts/tls_fingerprint_probe.py --live --proxy "$PROXY_URL"

Exit codes: ``0`` all checks passed, ``1`` a check failed, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.tls_fingerprint import (  # noqa: E402
    VERIFIED_EXTENSION_ORDERS,
    build,
    permute_extension_order,
)

DEFAULT_TARGET = "https://auth.openai.com/robots.txt"
PEET = "https://tls.peet.ws/api/all"


def profiles() -> list[str]:
    from sms_tool.auth_headers import AUTH_FINGERPRINT_PROFILES

    return list(AUTH_FINGERPRINT_PROFILES)


def _session(impersonate: str, proxy: str):
    from curl_cffi import requests as curl_requests

    # Explicit branches rather than a ``**kwargs: dict[str, object]``: the typed
    # constructor rejects an ``object``-valued mapping, and the two cases are
    # exactly one line each.
    if proxy:
        return curl_requests.Session(impersonate=impersonate, timeout=25, proxies={"http": proxy, "https": proxy})
    return curl_requests.Session(impersonate=impersonate, timeout=25)


def _auth_edge_headers(impersonate: str) -> dict[str, str]:
    """The header set the registration flow actually sends.

    A bare ``Session(impersonate=...)`` sends only curl-impersonate's default
    headers, and Cloudflare answers those with a managed challenge for the
    whole Firefox family (measured 2026-11-11: 403 "Just a moment..." for
    firefox144/147/135/133 both direct and through the IN exit).  That is a
    property of the *headers*, not of the ClientHello: with this project's
    ``openai_auth_headers`` the same profiles answer 200.  Probing without them
    would therefore report a false failure and, worse, blame the TLS override.
    """
    from sms_tool.auth_headers import openai_auth_headers, set_auth_fingerprint

    set_auth_fingerprint(impersonate)
    return openai_auth_headers(
        referer="https://chatgpt.com/",
        origin="https://auth.openai.com",
        accept="text/html,application/xhtml+xml",
        include_trace=True,
        extra={
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
            "Upgrade-Insecure-Requests": "1",
        },
    )


def capture(proxy: str, only: str = "") -> int:
    """Print the live extension order + hashes per profile."""
    print(f"target: {PEET}{' via proxy' if proxy else ' direct'}")
    for name in profiles():
        if only and name != only:
            continue
        try:
            session = _session(name, proxy)
            data = session.get(PEET).json()
            session.close()
        except Exception as exc:  # broad-except: report and keep going
            print(f"{name:16s} FAILED {type(exc).__name__}: {str(exc)[:70]}")
            continue
        tls = data.get("tls") or {}
        h2 = data.get("http2") or {}
        ja3 = str(tls.get("ja3") or "")
        extensions = ja3.split(",")[2] if ja3.count(",") >= 2 else ""
        print(
            f"{name:16s} ja3={tls.get('ja3_hash')} akamai={h2.get('akamai_fingerprint_hash')}\n"
            f"                 ext={extensions}"
        )
    return 0


def verify_structure(seeds: int) -> int:
    """Offline: the override must be per-account, complete and order-only."""
    failures = 0
    names = profiles()
    print(f"profiles: {len(names)}  verified families: {sorted(VERIFIED_EXTENSION_ORDERS)}")
    for family, base in sorted(VERIFIED_EXTENSION_ORDERS.items()):
        base_set = set(base.split("-"))
        orders = set()
        for index in range(max(2, seeds)):
            order = permute_extension_order(base, f"probe-seed-{index}")
            if not order:
                print(f"  {family}: FAILED to permute")
                failures += 1
                break
            parts = order.split("-")
            if set(parts) != base_set:
                print(f"  {family}: FAILED set changed")
                failures += 1
                break
            if parts[0] != base.split("-")[0]:
                print(f"  {family}: FAILED first extension moved")
                failures += 1
                break
            if "21" in base_set and parts[-1] != "21":
                print(f"  {family}: FAILED padding is not last")
                failures += 1
                break
            orders.add(order)
        else:
            print(f"  {family}: {len(orders)} distinct order(s) over {max(2, seeds)} seeds")
            if len(orders) < min(2, max(2, seeds)):
                print(f"  {family}: FAILED does not vary per account")
                failures += 1

    for name in names:
        first = build(profile=name, seed="probe-1")
        second = build(profile=name, seed="probe-2")
        family = first.family
        if family in VERIFIED_EXTENSION_ORDERS:
            if not first.active or first.extension_order == second.extension_order:
                print(f"  {name}: FAILED override missing or not per-account")
                failures += 1
        elif first.active:
            print(f"  {name}: FAILED override built for an unverified family")
            failures += 1
    if not failures:
        print("structure: OK")
    return 0 if not failures else 1


def verify_live(proxy: str, target: str, seeds: int) -> int:
    """Real handshake per profile (and per seed for the verified families)."""
    failures = 0
    for name in profiles():
        attempts = max(1, seeds) if build(profile=name, seed="x").family in VERIFIED_EXTENSION_ORDERS else 1
        statuses = []
        for index in range(attempts):
            seed = f"probe-live-{index}"
            override = build(profile=name, seed=seed)
            try:
                session = _session(name, proxy)
                if override.active:
                    from curl_cffi.const import CurlOpt

                    session.curl_options = {CurlOpt.TLS_EXTENSION_ORDER: override.extension_order}
                response = session.get(target, headers=_auth_edge_headers(name))
                statuses.append(int(response.status_code))
                session.close()
            except Exception as exc:  # broad-except: report and keep going
                statuses.append(f"{type(exc).__name__}:{str(exc)[:40]}")
        ok = all(isinstance(status, int) and status < 400 for status in statuses)
        print(f"{name:16s} {'OK  ' if ok else 'FAIL'} {statuses}")
        failures += 0 if ok else 1
    print(f"live: {len(profiles()) - failures}/{len(profiles())} profiles reached {target}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proxy", default="", help="proxy URL to exit through")
    parser.add_argument("--capture", action="store_true", help="print the live preset extension order (network)")
    parser.add_argument("--live", action="store_true", help="perform real requests per profile (network)")
    parser.add_argument("--only", default="", help="restrict --capture/--live to one profile name")
    parser.add_argument(
        "--target", default=DEFAULT_TARGET, help=f"request target for --live (default {DEFAULT_TARGET})"
    )
    parser.add_argument("--seeds", type=int, default=3, help="seeds to compare (default 3)")
    parser.add_argument("--json", action="store_true", help="emit the built overrides as JSON and exit")
    args = parser.parse_args(argv)

    if args.json:
        payload = {
            name: build(profile=name, seed=f"probe-{index}").to_dict() for name in profiles() for index in range(1)
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.capture:
        return capture(args.proxy, args.only)
    if args.live:
        return verify_live(args.proxy, args.target, args.seeds)
    return verify_structure(args.seeds)


if __name__ == "__main__":
    raise SystemExit(main())

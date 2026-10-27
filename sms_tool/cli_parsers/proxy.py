"""Proxy-pool administration flags for ``cli.build_parser``.

Three read-only commands that answer three different questions about a pool:

* ``--proxy-list``   what is configured (masked), plus cached geo/health
* ``--proxy-test``   can it reach the ChatGPT edge, and where does it exit
* ``--proxy-screen`` is it a stable residential line (dual-pass + ASN ranking)

Each writes one JSON document; with ``--desktop-ipc`` it is emitted as the
terminal IPC result frame instead of printed, which is how the desktop
ProxyPool window reads it.
"""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--proxy-list",
        action="store_true",
        help="List the configured proxy pool (credentials masked) with cached geo/health",
    )
    parser.add_argument(
        "--proxy-test",
        action="store_true",
        help="Probe each pool entry against the ChatGPT edge and measure its exit country",
    )
    parser.add_argument(
        "--proxy-screen",
        action="store_true",
        help="Dual-pass residential screening (stability/country/ASN) with an ASN ranking",
    )
    parser.add_argument(
        "--proxy-read-lane",
        default="protocol_registration",
        help="Config lane to list/probe when --proxy-pool is not given",
    )
    parser.add_argument("--proxy-country", default="", help="Target ISO country for --proxy-test/--proxy-screen")
    parser.add_argument(
        "--proxy-quality-min",
        type=int,
        default=0,
        help="Minimum residential quality score (0-100) for --proxy-screen",
    )
    parser.add_argument(
        "--proxy-workers", type=int, default=8, help="Concurrent probes for --proxy-test/--proxy-screen"
    )
    parser.add_argument("--proxy-timeout", type=float, default=6.0, help="Per-probe timeout seconds")

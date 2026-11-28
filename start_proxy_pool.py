#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in ``scripts/start_proxy_pool.py``."""

from scripts.start_proxy_pool import _load_upstreams_from_str, _parse_args, _upstreams_from_proxy_cfg, main

__all__ = ["_load_upstreams_from_str", "_parse_args", "_upstreams_from_proxy_cfg", "main"]

if __name__ == "__main__":
    main()

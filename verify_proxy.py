#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in ``scripts/verify_proxy.py``."""

from scripts.verify_proxy import _chat_base, _load_config, _report, main, test_proxy

__all__ = ["_chat_base", "_load_config", "_report", "main", "test_proxy"]

if __name__ == "__main__":
    raise SystemExit(main())

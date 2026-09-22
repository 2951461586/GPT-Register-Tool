"""Ratchet for config keys in the two highest-churn sections.

Why this exists
---------------
``config_unread_keys`` in the doctor report lists keys that are set but never
read -- measured at 61 on 2026-09-19, and the count only grows because nothing
fails when a new one appears.  Worse, ``validate_config`` has **no unknown-key
check at all**: a typo (``at_stability_probe_conut``) is silently ignored, the
default is used, and the operator only finds out when a batch behaves wrong.

This ratchet freezes the accepted key set for ``registration`` and
``email_registration`` -- the two sections every registration-tuning change
touches.  The accepted set is the union of:

  * keys the active config actually sets (runtime.json),
  * keys ``validate_config`` validates (they are legal by definition),
  * keys documented in ``config.example.json`` for those sections,

frozen into ``config_key_baseline.json``.  A key outside that set in any of the
three config files fails the check.  Adding a legitimate new key means adding
it to the baseline in the same commit -- which is exactly the review moment a
silent typo never gets.

    python scripts/config_key_ratchet.py                  # check
    python scripts/config_key_ratchet.py --detail         # per-section sets
    python scripts/config_key_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "config_key_baseline.json"
SECTIONS = ("registration", "email_registration")
CONFIG_FILES = ("runtime.json", "config.example.json")


def _section_keys(path: Path) -> dict[str, set]:
    if not path.exists():
        return {s: set() for s in SECTIONS}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {s: set((data.get(s) or {}).keys()) if isinstance(data.get(s), dict) else set() for s in SECTIONS}


def _validator_keys() -> dict[str, set]:
    """Keys validate_config references under each watched section."""
    src = (ROOT / "sms_tool" / "config.py").read_text(encoding="utf-8")
    i = src.find("def validate_config")
    j = src.find("\ndef ", i + 5)
    body = src[i:j]
    out = {s: set() for s in SECTIONS}
    for sec in SECTIONS:
        # Word-boundary the section name: ``email_registration.x`` contains the
        # substring ``registration.x``, and without the boundary an email key
        # leaks into the registration section (measured on ``use_as_username``,
        # 2026-09-19).
        name = r"(?<![a-z_])" + re.escape(sec)
        for m in re.finditer(name + r"\.get\(\s*\"([a-z_0-9]+)\"", body):
            out[sec].add(m.group(1))
        for m in re.finditer(name + r",\s*\(([^)]*)\)", body):
            for k in re.findall(r"\"([a-z_0-9]+)\"", m.group(1)):
                out[sec].add(k)
        for m in re.finditer(name + r"\.([a-z_0-9]+)", body):
            out[sec].add(m.group(1))
        out[sec].discard("get")
    return out


def accepted() -> dict[str, set]:
    out = _validator_keys()
    for name in CONFIG_FILES:
        for sec, keys in _section_keys(ROOT / name).items():
            out[sec] |= keys
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    current = accepted()
    if args.update_baseline:
        # 🔴 ``newline="\n"`` is mandatory -- see the identical note in
        # ``bare_print_ratchet.py``: the ``newline=None`` default turns ``\n``
        # into CRLF on Windows, which silently desyncs the worktree copy from
        # its LF index entry (``git diff`` cannot see it).
        BASELINE.write_text(
            json.dumps({k: sorted(v) for k, v in current.items()}, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print("baseline updated: " + ", ".join(f"{s}={len(v)}" for s, v in current.items()))
        return 0

    baseline = {k: set(v) for k, v in json.loads(BASELINE.read_text(encoding="utf-8")).items()} if BASELINE.exists() else {}
    failures = []
    for sec in SECTIONS:
        extra = current[sec] - baseline.get(sec, set())
        if extra:
            failures.append(f"  {sec}: new keys not in baseline: {sorted(extra)}")
    if args.detail:
        for sec in SECTIONS:
            print(f"[{sec}] accepted {len(current[sec])} keys")
            for k in sorted(current[sec]):
                print(f"    {k}")
    if failures:
        print("config-key ratchet FAILED (a key outside the accepted set appeared):")
        print("\n".join(failures))
        print("Add the key to the baseline only if it is intentional: --update-baseline")
        return 1
    print("config-key ratchet OK (" + ", ".join(f"{s}={len(current[s])}" for s in SECTIONS) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())

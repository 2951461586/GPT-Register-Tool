"""Ratchet: the registration lane and the payment lane stay separated.

Why this exists
---------------
The import-layer ratchet measures **directory** pairs, so it cannot see two
modules that live side by side at the package root.  On 2026-10-18 the
registration lane imported two such modules:

* ``registration_handlers`` / ``registration_finalize`` /
  ``registration_drivers.browser_flow.orchestrator`` imported
  ``paypal_proxy.infer_proxy_country`` -- exit-country inference is a proxy/geo
  concern that happened to be *defined* in a PayPal-named module;
* ``commands/registration.py`` imported ``payment_operation`` -- the durable
  idempotency journal the payment lane also uses.

Both were real coupling that no gate could see.  The fix moved the definition
sites (``proxy_entry.infer_proxy_country``, ``operation_journal``) and left
delegating shells behind, so the *names* survive while the edges are gone.

What this freezes
-----------------
1. **registration -> payment: zero.**  Any direct import -- module scope or
   function body, so a lazy import cannot hide it -- fails.  The baseline is an
   empty list on purpose; this is the one rule with no allowance.
2. **payment -> registration: shared infrastructure only.**  The payment lane
   legitimately borrows the Sentinel anti-bot client, the proxy string
   authority and the auth-header profiles.  A fourth target needs a decision
   here, not a silent import.
3. **Non-payment importers of payment code: a frozen inventory.**  Otherwise
   the edge could be re-opened by adding a neutral facade and importing that,
   which is the same coupling with a better name.
4. **Only ``cli`` may import the adapter layers.**  The ten
   ``sms_tool -> sms_tool/commands`` / ``-> sms_tool/cli_parsers`` edges the
   directory ratchet reports all originate in ``cli.py``, which *is* the CLI
   entrypoint (``docs/architecture.md`` Ownership Matrix) -- physically at the
   root, but not a workflow module.  So the enforceable rule is not "root must
   not import adapters" (false) but "only ``cli`` does".
5. **``registration.py`` fan-out: frozen target set.**  It is the compatibility
   facade for the whole lane; 31 distinct modules today.  Freezing the *set*
   rather than the count means moving an import within the facade cannot mask a
   new dependency.

Usage:
    python scripts/lane_coupling_ratchet.py              # check
    python scripts/lane_coupling_ratchet.py --detail     # print every set
    python scripts/lane_coupling_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "lane_coupling_baseline.json"
PKG = "sms_tool"
EXCLUDED_DIRS = {"__pycache__", "_vendor"}

#: Second path component (``sms_tool.<this>``) -> registration lane.
REGISTRATION_PREFIXES = (
    "registration",
    "auth_",
    "mailbox",
    "sentinel",
    "phone_",
    "otp_strategy",
    "sms_provider",
    "smsbower",
    "nexsms",
    "omakse",
)

#: Second path component -> payment lane.
PAYMENT_PREFIXES = (
    "payment_",
    "pay_link",
    "paypal",
    "paypal_link",
    "upi_link",
    "gcash_",
    "wallet_",
    "pp_link_helpers",
    "gen_pp_link",
    "checkout_contract",
    "promotion_states",
    "regional_payment_adapter",
)

#: Registration-lane modules the payment lane may import.  All three are shared
#: infrastructure, not workflow: the Sentinel client, the proxy string authority
#: and the auth-header/fingerprint profiles.
PAYMENT_MAY_IMPORT = ("sentinel", "phone_proxy", "auth_headers")

#: Adapter-layer subpackages the root package may reach into -- from ``cli`` only.
ADAPTER_LAYERS = ("commands", "cli_parsers")
ADAPTER_OWNER = "sms_tool.cli"

#: The compatibility facade whose fan-out is frozen.
FACADE = "sms_tool.registration"


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in (ROOT / PKG).rglob("*.py"):
        if set(path.parts) & EXCLUDED_DIRS:
            continue
        files[_module_name(path)] = path
    return files


def _resolve(node: ast.AST, module: str, is_package: bool) -> list[str]:
    """Every dotted name one ``import``/``from`` statement can denote."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        base = module.split(".")
        if not is_package:
            base = base[:-1]
        up = node.level - 1
        if up:
            base = base[:-up] if up <= len(base) else []
        target = ".".join(filter(None, [".".join(base), node.module or ""]))
    else:
        target = node.module or ""
    names = [target] if target else []
    names.extend(f"{target}.{alias.name}" for alias in node.names)
    return names


def _normalize(name: str, known: set[str]) -> str | None:
    """Resolve a dotted name to a first-party module (or ``None``)."""
    if name in known:
        return name
    head = ".".join(name.split(".")[:2])
    return head if head in known else None


def edges() -> set[tuple[str, str]]:
    """Every first-party import edge as ``(source_module, target_module)``.

    Both module scope and function bodies are included: a delayed import is the
    documented cycle-break, not an exemption from the lane rule.
    """
    files = _package_files()
    known = set(files)
    found: set[tuple[str, str]] = set()
    for module, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - syntax errors fail elsewhere
            continue
        is_package = path.name == "__init__.py"
        for node in ast.walk(tree):
            for name in _resolve(node, module, is_package):
                target = _normalize(name, known)
                if target and target != module:
                    found.add((module, target))
    return found


def _component(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ""


def lane_of(module: str) -> str:
    component = _component(module)
    if component.startswith(REGISTRATION_PREFIXES):
        return "registration"
    if component.startswith(PAYMENT_PREFIXES):
        return "payment"
    return "neutral"


def analyze() -> dict[str, list]:
    """The five frozen facts, as sorted lists of JSON-friendly values."""
    found = edges()
    registration_to_payment: list[list[str]] = []
    payment_to_registration: list[list[str]] = []
    payment_importers: set[str] = set()
    root_to_adapters: list[list[str]] = []
    facade_fanout: set[str] = set()

    for source, target in found:
        source_lane = lane_of(source)
        target_lane = lane_of(target)
        if source_lane == "registration" and target_lane == "payment":
            registration_to_payment.append([source, target])
        if source_lane == "payment" and target_lane == "registration":
            payment_to_registration.append([source, target])
        if target_lane == "payment" and source_lane != "payment":
            payment_importers.add(source)
        if source.count(".") == 1 and _component(target) in ADAPTER_LAYERS:
            root_to_adapters.append([source, target])
        if source == FACADE:
            facade_fanout.add(target)

    return {
        "registration_to_payment": sorted(registration_to_payment),
        "payment_to_registration": sorted(payment_to_registration),
        "payment_importers_outside_lane": sorted(payment_importers),
        "root_to_adapter_layers": sorted(root_to_adapters),
        "registration_facade_fanout": sorted(facade_fanout),
    }


def load_baseline(path: Path | None = None) -> dict[str, list]:
    target = path or BASELINE
    if not target.is_file():
        return {}
    raw = json.loads(target.read_text(encoding="utf-8"))
    return {key: value for key, value in raw.items() if key != "schema"}


def _pairs(values: list) -> set[tuple[str, str]]:
    return {(str(a), str(b)) for a, b in values}


def violations(current: dict[str, list], baseline: dict[str, list]) -> list[str]:
    """Every way the tree is worse than the frozen baseline."""
    problems: list[str] = []

    unexpected = _pairs(current["registration_to_payment"]) - _pairs(baseline.get("registration_to_payment", []))
    for source, target in sorted(unexpected):
        problems.append(f"registration lane imports the payment lane: {source} -> {target}")

    for source, target in current["payment_to_registration"]:
        if _component(target).startswith(PAYMENT_MAY_IMPORT):
            continue
        problems.append(
            f"payment lane imports registration workflow (only {', '.join(PAYMENT_MAY_IMPORT)} "
            f"are shared infrastructure): {source} -> {target}"
        )

    new_importers = set(current["payment_importers_outside_lane"]) - set(
        baseline.get("payment_importers_outside_lane", [])
    )
    for source in sorted(new_importers):
        problems.append(f"new non-payment consumer of payment code: {source}")

    for source, target in sorted(
        _pairs(current["root_to_adapter_layers"]) - _pairs(baseline.get("root_to_adapter_layers", []))
    ):
        owner = "" if source == ADAPTER_OWNER else f" (only {ADAPTER_OWNER} may)"
        problems.append(f"root module imports an adapter layer{owner}: {source} -> {target}")

    new_targets = set(current["registration_facade_fanout"]) - set(baseline.get("registration_facade_fanout", []))
    for target in sorted(new_targets):
        problems.append(f"registration facade gained a dependency: {FACADE} -> {target}")

    return problems


def _write_baseline(current: dict[str, list], path: Path) -> None:
    payload = {"schema": "lane_coupling.v1", **current}
    # ``newline="\n"`` on purpose: ``.gitattributes`` declares ``*.json text eol=lf``
    # and every other baseline here is LF.  The default text mode writes CRLF on
    # Windows, so the committed bytes and the working file disagree --
    # ``git ls-files --eol`` reported ``i/lf w/crlf`` for the first version.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--detail", action="store_true", help="print every frozen set")
    parser.add_argument("--update-baseline", action="store_true", help="rewrite the baseline from the tree")
    args = parser.parse_args(argv)

    current = analyze()
    if args.update_baseline:
        _write_baseline(current, BASELINE)
        print(f"lane-coupling baseline written: {BASELINE.relative_to(ROOT)}")
        return 0

    if args.detail:
        for key, value in current.items():
            print(f"=== {key} ({len(value)})")
            for item in value:
                print("   ", item)

    baseline = load_baseline()
    if not baseline:
        print("lane-coupling ratchet: baseline missing; run --update-baseline", file=sys.stderr)
        return 1

    problems = violations(current, baseline)
    if problems:
        print("lane-coupling ratchet FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        "lane-coupling ratchet OK "
        f"(registration->payment 0, payment->registration {len(current['payment_to_registration'])}, "
        f"outside importers {len(current['payment_importers_outside_lane'])}, "
        f"facade fan-out {len(current['registration_facade_fanout'])})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

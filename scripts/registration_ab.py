"""Registration A/B harness for the three live comparisons that are still owed.

``docs/current/protocol-registration.md`` ("Validation limits") requires a
controlled live comparison before trusting three landed-but-unvalidated changes:

* **P0-1** — preflight login entry: ``browser`` (``chatgpt.com/auth/login``)
  vs ``legacy`` (``auth.openai.com/log-in``).
* **P0-2** — Cloudflare-challenge discrimination on the preflight failure path.
  No toggle: the classification is always on, so this arm is *observe-only*.
* **P1-3** — Sentinel password-page bundle: ``sentinel_password_bundle`` off
  (default) vs on.

This script **never performs live traffic**. Only the operator runs the real
batch, because it needs paid mailboxes, a live exit pool, and a controlled
environment (offline tests cannot establish a registration success rate). The
harness does three things:

``plan``     print the pre-registered design: arms, exact config overrides,
             held-constant variables, metrics and decision rule.
``collect``  normalise one operator-run arm's artifacts (run log + batch report
             + config snapshot) into a single record under
             ``runtime/registration_ab/``. The config snapshot is the
             *manipulation check*: without it the arm is recorded unverified.
``compare``  join the arm records, compute the per-metric deltas and apply the
             pre-registered decision rule. It refuses a verdict for an arm whose
             toggle was not verified, and it never reports a success *rate* as
             established by this offline tooling.

Parsing and comparison are pure functions so they can be unit-tested without
running anything (``tests/test_registration_ab.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "runtime" / "registration_ab"

#: Minimum attempted registrations (and preflight probes) per arm before a
#: comparison is allowed to return anything but ``underpowered``. Pre-registered
#: so it cannot be moved after seeing the data.
MIN_ARM_ATTEMPTED = 30
#: Absolute difference in a rate that counts as a real move, pre-registered.
RATE_DELTA = 0.05

EXPERIMENTS: dict[str, dict[str, Any]] = {
    "p0-1-preflight-endpoint": {
        "hypothesis": (
            "Probing chatgpt.com/auth/login (browser entry) draws fewer "
            "Cloudflare challenges than probing auth.openai.com/log-in, which a "
            "real browser never requests standalone."
        ),
        "toggle": "registration.preflight_login_page",
        "arms": {"browser": "browser", "legacy": "legacy"},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "preflight.cloudflare_rate (challenges / probes)",
            "preflight.no_healthy_route",
            "preflight.probes_ok",
            "funnel.registered_per_attempted",
        ),
        "decision": (
            "favor the arm with the lower cloudflare_rate when the gap exceeds "
            f"{RATE_DELTA:.2f} and its no_healthy_route count is not higher; "
            "otherwise inconclusive."
        ),
    },
    "p0-2-cloudflare-observation": {
        "hypothesis": (
            "The Cloudflare discrimination path (CLOUDFLARE_CHALLENGE_MARKER) "
            "actually classifies exit-level refusals instead of generic 4xx."
        ),
        "toggle": None,
        "arms": {"observe": None},
        "hold_constant": ("same exit pool and mailbox batch as the observation window",),
        "metrics": (
            "preflight.cloudflare_hosts (per-exit challenge counts)",
            "preflight.skipped_hosts",
            "funnel.registration_failures_by_class",
        ),
        "decision": (
            "observation only: report the challenge rate per exit. A change to "
            "in-flow rotation needs its own toggle and its own A/B before landing."
        ),
    },
    "p1-3-sentinel-password-bundle": {
        "hypothesis": (
            "Priming the password page's Sentinel flows from one shared proof "
            "does not reduce registration success and does not introduce "
            "sentinel-specific failures."
        ),
        "toggle": "registration.sentinel_password_bundle",
        "arms": {"default": False, "bundle": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool",
            "same registration lane (password lane only) and driver",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (sentinel_* codes)",
            "sentinel.bundle_primed (manipulation check)",
        ),
        "decision": (
            "favor bundle only when it is not worse by more than "
            f"{RATE_DELTA:.2f} AND no sentinel-specific failure class grows; a "
            "regression of any size is a reason to keep the default off."
        ),
    },
}

# Preflight progress lines emitted by ``commands/registration`` (verbatim
# markers; the success line uses full-width parens, the failure lines ASCII
# parens -- match on the marker, not the punctuation).
_PREFLIGHT_PROGRESS = re.compile(r"注册预检\s+(\d+)/(\d+)\s+(.*?)\s+(可用|被 Cloudflare 挑战|失败)")
_PREFLIGHT_COMPLETED = re.compile(r"注册预检完成：(\d+)/(\d+)\s*条实测可用")
_PREFLIGHT_SKIPPED = re.compile(r"剩余候选已跳过：(.+)")
_NO_HEALTHY_ROUTE = re.compile(r"registration_preflight_failed:no_healthy_route:(\w+)")
_HOST_COUNT = re.compile(r"([^,×]+?)\s*×\s*(\d+)")


def parse_preflight_log(text: str) -> dict[str, Any]:
    """Extract preflight counters from a registration run log.

    Pure: the returned shape is what ``collect`` persists and ``compare`` joins.
    """
    probes_ok = 0
    probes_cloudflare = 0
    probes_failed = 0
    cloudflare_hosts: dict[str, int] = {}
    failed_hosts: dict[str, int] = {}
    completed: tuple[int, int] | None = None
    skipped_hosts: dict[str, int] = {}
    no_healthy_route = ""
    for line in str(text or "").splitlines():
        progress = _PREFLIGHT_PROGRESS.search(line)
        if progress:
            label, outcome = progress.group(3), progress.group(4)
            if outcome == "可用":
                probes_ok += 1
            elif outcome == "被 Cloudflare 挑战":
                probes_cloudflare += 1
                cloudflare_hosts[label] = cloudflare_hosts.get(label, 0) + 1
            else:
                probes_failed += 1
                failed_hosts[label] = failed_hosts.get(label, 0) + 1
            continue
        done = _PREFLIGHT_COMPLETED.search(line)
        if done:
            completed = (int(done.group(1)), int(done.group(2)))
            continue
        skipped = _PREFLIGHT_SKIPPED.search(line)
        if skipped:
            for label, count in _HOST_COUNT.findall(skipped.group(1)):
                skipped_hosts[label.strip()] = int(count)
            continue
        route = _NO_HEALTHY_ROUTE.search(line)
        if route:
            no_healthy_route = route.group(1)
    probes = probes_ok + probes_cloudflare + probes_failed
    return {
        "probes": probes,
        "probes_ok": probes_ok,
        "probes_cloudflare": probes_cloudflare,
        "probes_failed": probes_failed,
        "cloudflare_rate": round(probes_cloudflare / probes, 4) if probes else None,
        "cloudflare_hosts": dict(sorted(cloudflare_hosts.items())),
        "failed_hosts": dict(sorted(failed_hosts.items())),
        "skipped_hosts": dict(sorted(skipped_hosts.items())),
        "completed_ok": completed[0] if completed else None,
        "completed_attempted": completed[1] if completed else None,
        "no_healthy_route": no_healthy_route,
    }


def load_funnel(path: Path | None) -> dict[str, Any] | None:
    """Read the safe funnel block from a batch report.

    Accepts either the whole report (``{"funnel": {...}}``) or the funnel dict
    itself. Returns ``None`` when there is no usable funnel.
    """
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return None
    funnel = payload.get("funnel")
    if isinstance(funnel, Mapping):
        return dict(funnel)
    if "attempted" in payload or "registered" in payload:
        return dict(payload)
    return None


def read_toggles(config_path: Path | None) -> dict[str, Any]:
    """Read the A/B toggles from a config snapshot (the manipulation check)."""
    if config_path is None:
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    registration = payload.get("registration") if isinstance(payload, Mapping) else None
    registration = registration if isinstance(registration, Mapping) else {}
    out: dict[str, Any] = {}
    if "preflight_login_page" in registration:
        out["registration.preflight_login_page"] = registration["preflight_login_page"]
    if "sentinel_password_bundle" in registration:
        out["registration.sentinel_password_bundle"] = registration["sentinel_password_bundle"]
    return out


def build_record(
    *,
    experiment: str,
    arm: str,
    log_text: str,
    funnel: Mapping[str, Any] | None,
    toggles: Mapping[str, Any],
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one arm's normalised record."""
    design = EXPERIMENTS.get(experiment)
    if design is None:
        raise ValueError(f"unknown experiment: {experiment}")
    arm_expected = design["arms"].get(arm, "<missing>")
    toggle_name = design["toggle"]
    expected = arm_expected
    actual = toggles.get(toggle_name) if toggle_name else None
    # ``None`` expected means the arm does not own a toggle (observe-only).
    verified = bool(toggle_name) and str(actual) == str(expected)
    return {
        "experiment": experiment,
        "arm": arm,
        "collected_at": int(time.time()),
        "toggle": toggle_name,
        "toggle_expected": expected,
        "toggle_actual": actual,
        "toggle_verified": verified,
        "preflight": parse_preflight_log(log_text),
        "funnel": dict(funnel) if isinstance(funnel, Mapping) else None,
        "log_path": str(log_path) if log_path else "",
        "log_sha256": hashlib.sha256(str(log_text or "").encode("utf-8")).hexdigest()[:16],
    }


def _rate(funnel: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(funnel, Mapping):
        return None
    value = funnel.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compare_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Join arm records and apply the pre-registered decision rule.

    Pure. Returns ``{"experiment", "arms", "verdict", "reason", "powered"}``.
    A verdict of ``unverified`` means at least one arm's config snapshot did not
    match the arm it claims to be -- the comparison is not interpretable.
    """
    rows = [dict(row) for row in records]
    if not rows:
        return {"experiment": "", "arms": [], "verdict": "no_data", "reason": "no arm records", "powered": False}
    experiments = {str(row.get("experiment") or "") for row in rows}
    if len(experiments) != 1:
        return {
            "experiment": "",
            "arms": [row.get("arm") for row in rows],
            "verdict": "mixed_experiments",
            "reason": f"records span {sorted(experiments)}; compare one experiment at a time",
            "powered": False,
        }
    experiment = experiments.pop()
    design = EXPERIMENTS.get(experiment, {})
    unverified = [str(row.get("arm")) for row in rows if not row.get("toggle_verified")]
    if design.get("toggle") and unverified:
        return {
            "experiment": experiment,
            "arms": [row.get("arm") for row in rows],
            "verdict": "unverified",
            "reason": (
                "config snapshot missing or does not match the arm for: "
                + ", ".join(unverified)
                + " -- pass --config so the toggle is verified before comparing"
            ),
            "powered": False,
        }

    metrics = {}
    for row in rows:
        arm = str(row.get("arm"))
        preflight = row.get("preflight") or {}
        funnel = row.get("funnel")
        metrics[arm] = {
            "attempted": funnel.get("attempted") if isinstance(funnel, Mapping) else None,
            "registered_per_attempted": _rate(funnel, "registered_per_attempted"),
            "probes": preflight.get("probes"),
            "probes_ok": preflight.get("probes_ok"),
            "probes_cloudflare": preflight.get("probes_cloudflare"),
            "cloudflare_rate": preflight.get("cloudflare_rate"),
            "no_healthy_route": str(preflight.get("no_healthy_route") or ""),
            "failure_classes": (funnel or {}).get("registration_failures_by_class")
            if isinstance(funnel, Mapping)
            else None,
        }

    powered = all(
        isinstance(item.get("attempted"), int) and item["attempted"] >= MIN_ARM_ATTEMPTED for item in metrics.values()
    )
    verdict, reason = _rule(experiment, metrics, powered)
    return {
        "experiment": experiment,
        "arms": [str(row.get("arm")) for row in rows],
        "metrics": metrics,
        "powered": powered,
        "min_arm_attempted": MIN_ARM_ATTEMPTED,
        "rate_delta": RATE_DELTA,
        "verdict": verdict,
        "reason": reason,
    }


def _rule(experiment: str, metrics: Mapping[str, Mapping[str, Any]], powered: bool) -> tuple[str, str]:
    if not powered:
        return "underpowered", f"each arm needs >= {MIN_ARM_ATTEMPTED} attempted registrations"

    if experiment == "p0-1-preflight-endpoint":
        browser = metrics.get("browser") or {}
        legacy = metrics.get("legacy") or {}
        b_rate, l_rate = browser.get("cloudflare_rate"), legacy.get("cloudflare_rate")
        if b_rate is None or l_rate is None:
            return "inconclusive", "cloudflare_rate unavailable (no preflight lines in the log)"
        if b_rate < l_rate - RATE_DELTA and not browser.get("no_healthy_route"):
            return "favor_browser", f"browser challenge rate {b_rate} < legacy {l_rate} - {RATE_DELTA}"
        if l_rate < b_rate - RATE_DELTA and not legacy.get("no_healthy_route"):
            return "favor_legacy", f"legacy challenge rate {l_rate} < browser {b_rate} - {RATE_DELTA}"
        return "inconclusive", f"challenge rates within {RATE_DELTA}: browser={b_rate} legacy={l_rate}"

    if experiment == "p1-3-sentinel-password-bundle":
        default = metrics.get("default") or {}
        bundle = metrics.get("bundle") or {}
        d_rate, b_rate = default.get("registered_per_attempted"), bundle.get("registered_per_attempted")
        if d_rate is None or b_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        sentinel_growth = _sentinel_failure_growth(default.get("failure_classes"), bundle.get("failure_classes"))
        if sentinel_growth:
            return "keep_default_off", f"sentinel-specific failures grew: {sentinel_growth}"
        if b_rate > d_rate + RATE_DELTA:
            return "favor_bundle", f"bundle {b_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > b_rate + RATE_DELTA:
            return "keep_default_off", f"bundle {b_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} bundle={b_rate}"

    if experiment == "p0-2-cloudflare-observation":
        observe = metrics.get("observe") or {}
        return (
            "observation_only",
            "no toggle: report per-exit challenge counts; in-flow rotation needs its own A/B before landing "
            f"(challenges={observe.get('probes_cloudflare')}, rate={observe.get('cloudflare_rate')})",
        )

    return "inconclusive", "no decision rule registered for this experiment"


def _sentinel_failure_growth(
    default: Mapping[str, Any] | None, bundle: Mapping[str, Any] | None
) -> dict[str, tuple[int, int]]:
    """Failure classes whose count grew in the bundle arm. Pure."""
    left = default if isinstance(default, Mapping) else {}
    right = bundle if isinstance(bundle, Mapping) else {}
    grown: dict[str, tuple[int, int]] = {}
    for key in set(left) | set(right):
        if "sentinel" not in str(key).lower():
            continue
        before = int(left.get(key) or 0)
        after = int(right.get(key) or 0)
        if after > before:
            grown[str(key)] = (before, after)
    return grown


def _cmd_plan(_args: argparse.Namespace) -> int:
    print(json.dumps(EXPERIMENTS, ensure_ascii=False, indent=2))
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    log_path = Path(args.log)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    funnel = load_funnel(Path(args.funnel)) if args.funnel else None
    toggles = read_toggles(Path(args.config)) if args.config else {}
    record = build_record(
        experiment=args.experiment,
        arm=args.arm,
        log_text=log_text,
        funnel=funnel,
        toggles=toggles,
        log_path=log_path,
    )
    if args.note:
        record["note"] = args.note
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.experiment}__{args.arm}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    if not record["toggle_verified"]:
        print(
            "WARNING: toggle not verified "
            f"(expected {record['toggle_expected']!r}, saw {record['toggle_actual']!r}); "
            "pass --config pointing at the config used for this run",
            file=sys.stderr,
        )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    records = []
    for spec in args.arm:
        if "=" not in spec:
            raise SystemExit(f"--arm must be NAME=FILE, got {spec!r}")
        _name, path = spec.split("=", 1)
        records.append(json.loads(Path(path).read_text(encoding="utf-8")))
    report = compare_records(records)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(
        "\nNote: this is a decision aid for a controlled live run, not a "
        "success-rate claim. Offline tests cannot establish a live registration "
        "rate (see docs/current/protocol-registration.md, Validation limits).",
        file=sys.stderr,
    )
    return 0 if report["verdict"] not in {"unverified", "mixed_experiments", "no_data"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the pre-registered A/B design")
    plan.set_defaults(func=_cmd_plan)

    collect = sub.add_parser("collect", help="normalise one operator-run arm into a record")
    collect.add_argument("--experiment", required=True, choices=sorted(EXPERIMENTS))
    collect.add_argument("--arm", required=True)
    collect.add_argument("--log", required=True, help="the registration run log to parse")
    collect.add_argument("--funnel", help="batch report JSON containing the funnel")
    collect.add_argument("--config", help="config snapshot used for the run (manipulation check)")
    collect.add_argument("--note", help="free-form operator note stored with the record")
    collect.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    collect.set_defaults(func=_cmd_collect)

    compare = sub.add_parser("compare", help="join arm records and apply the decision rule")
    compare.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="NAME=FILE",
        help="an arm record written by collect; repeat per arm",
    )
    compare.set_defaults(func=_cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

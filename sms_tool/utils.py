import random
import secrets
import threading
import time

from .config import CFG

_tls = threading.local()

# ==========================================
# Timing
# ==========================================
def _tl():
    if not hasattr(_tls, "timings"): _tls.timings = []
    return _tls.timings
def _tick(name):
    _tl().append((name, time.time()))
    print(f"[{name}]", flush=True)
def _tock():
    t = _tl(); t[-1] = (t[-1][0], time.time() - t[-1][1])
def _print_timings():
    t = _tl(); total = sum(e for _, e in t)
    print("\n" + "=" * 50)
    print(f"{'Step':<40} {'Time (s)':>10}")
    print("-" * 50)
    for name, elapsed in t: print(f"{name:<40} {elapsed:>10.2f}")
    print("-" * 50)
    print(f"{'TOTAL':<40} {total:>10.2f}")
    print("=" * 50)

def _timing_summary():
    t = _tl()
    return {
        "steps": [{"name": name, "seconds": round(elapsed, 2)} for name, elapsed in t],
        "total_seconds": round(sum(elapsed for _, elapsed in t), 2),
    }


def _safe_tock():
    timings = _tl()
    if timings and timings[-1][1] > 1_000_000:
        _tock()


# ==========================================
# Random Generators
# ==========================================
def _random_name():
    first = ["James", "John", "Robert", "Michael", "David", "William", "Mary", "Linda", "Barbara", "Jennifer"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Anderson"]
    return random.choice(first), random.choice(last)

def _random_birthdate():
    y, m, d = random.randint(1985, 2004), random.randint(1, 12), random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"

def _generate_password():
    reg = CFG.get("registration", {})
    length = reg.get("password_random_length", 12)
    suffix = reg.get("password_suffix", "!A1")
    charset = reg.get("password_charset", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    base_len = max(1, length - len(suffix))
    # Use cryptographic RNG to eliminate PRNG-based predictability markers
    return "".join(secrets.choice(charset) for _ in range(base_len)) + suffix


# Default per-stage humanized dwell ranges in milliseconds (min, max). Modeled on
# real browser pacing (page settle, reading, OTP hand-off) so the protocol flow
# does not complete a multi-stage OAuth in a fixed sub-second cadence. Overridable
# via ``registration.humanize_delays``.
_THINK_STAGE_RANGES_MS = {
    "post_sentinel": (600, 1800),
    "post_create_account": (900, 2600),
    "pre_at_probe": (700, 2200),
    "access_token_stability_wait": (0, 0),
    "default": (500, 1600),
}


def think_stage(stage_label: str = "", cfg: dict | None = None):
    """Insert a randomized human-like dwell between registration stages.

    A fixed dwell is itself a detectable cadence, so the delay is jittered:

    - ``registration.humanize_delays`` (``{stage: [min_ms, max_ms]}``) — when set,
      the stage (or ``default``) range is sampled uniformly.
    - ``registration.think_time_ms`` (legacy fixed value) — still honored, but now
      sampled in a ±35% band around it instead of a constant.
    - Otherwise the built-in per-stage ranges apply only when
      ``registration.humanize`` is enabled; disabled by default for parity.

    ``registration.humanize_factor`` scales all dwell times (e.g. 0.5 for faster
    batches). This defeats OpenAI's bot-timing detection that flags accounts
    completing multi-stage OAuth flows in near-constant time.
    """
    cfg = cfg or CFG
    registration_cfg = cfg.get("registration", {}) if isinstance(cfg, dict) else {}
    if not stage_label:
        return
    try:
        factor = float(registration_cfg.get("humanize_factor", 1.0) or 1.0)
    except (TypeError, ValueError):
        factor = 1.0

    configured = registration_cfg.get("humanize_delays")
    configured = configured if isinstance(configured, dict) else {}
    rng = configured.get(stage_label) or configured.get("default")

    seconds = 0.0
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            lo, hi = float(rng[0]), float(rng[1])
            seconds = random.uniform(min(lo, hi), max(lo, hi)) / 1000.0
        except (TypeError, ValueError):
            seconds = 0.0
    else:
        try:
            ms = int(registration_cfg.get("think_time_ms", 0))
        except (TypeError, ValueError):
            ms = 0
        if ms > 0:
            seconds = random.uniform(ms * 0.65, ms * 1.35) / 1000.0
        elif registration_cfg.get("humanize"):
            lo, hi = _THINK_STAGE_RANGES_MS.get(stage_label, _THINK_STAGE_RANGES_MS["default"])
            if hi > 0:
                seconds = random.uniform(lo, hi) / 1000.0

    seconds = max(0.0, seconds * (factor if factor > 0 else 1.0))
    if seconds > 0:
        time.sleep(min(seconds, 30.0))

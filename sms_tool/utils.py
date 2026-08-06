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


def think_stage(stage_label: str = "", cfg: dict | None = None):
    """Insert human-like dwell time between registration stages.

    Configured via ``registration.think_time_ms`` in config.json.
    Disabled (0) by default. Typical values: 800–3000 ms.

    This defeats OpenAI's bot-timing detection, which flags accounts that
    complete multi-stage OAuth flows in sub-second total time.
    """
    cfg = cfg or CFG
    registration_cfg = cfg.get("registration", {}) if isinstance(cfg, dict) else {}
    ms = 0
    try:
        ms = int(registration_cfg.get("think_time_ms", 0))
    except (TypeError, ValueError):
        ms = 0
    if ms > 0 and stage_label:
        time.sleep(ms / 1000.0)

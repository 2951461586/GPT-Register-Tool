import random
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
    return "".join(random.choices(charset, k=base_len)) + suffix

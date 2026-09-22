"""Structural parity report across protocol-payment extractors + duplication ratchet.

Why this exists
---------------
`services/protocol-payment/` contains seven provider extractors that were clearly
grown by copy-paste. The 2026-09-17 audit measured the damage:

    ideal_qr_extract.py  3188 lines / 134 top-level functions
    twint_extract.py     3174 lines / 134
    blik_qr_extract.py   3655 lines / 154
    -> 108 of ideal's 134 functions (81%) are AST-identical to twint's
       once provider-name tokens are normalised away.
    -> momo vs kakao: only 2.4% line overlap. So this is a *cluster*
       property, not something inherent to `services/`.

Line-level similarity is a bad metric here: it cannot tell "the same function
with a renamed variable" from "a function with one comparison flipped". This
tool answers the sharper question -- **after erasing provider naming, is it
structurally the same code?** -- and then treats the *amount* of remaining
duplication as a ratchet.

Pairing is by NORMALISED NAME, not exact name
---------------------------------------------
This matters more than it looks. `stripe_create_ideal_pm` and
`stripe_create_twint_pm` are the same function with the provider substituted --
but a naive exact-name join buckets them as "present in one file only" and
**hides them from the duplicate count entirely**. In ideal/twint, all 134
functions pair up once names are normalised; an exact-name join would have
reported 9 sibling functions as file-specific and quietly understated the
duplication.

So both the join key and the body are normalised: provider tokens are erased
from the function *name* (to find the counterpart) and from the body (to compare
it). Function names are otherwise preserved, so two different functions are
never conflated.

What it measures
----------------
For a pair of files it reports:

* ``identical``  -- paired functions whose normalised AST matches
* ``different``  -- paired functions whose bodies genuinely differ
* ``only_a`` / ``only_b`` -- no counterpart even after name normalisation

The headline number for the ratchet is ``len(identical)``, i.e. **duplicated
units**. That count may only go DOWN. Adding a new near-copy of an existing
function fails the gate.

⚠️ What this is NOT
-------------------
It is **not** a correctness oracle and **not** a licence to merge code. Two
functions being AST-identical proves they are redundant, not that collapsing
them is safe -- the caller's context (which proxy file it reads, which country
table it consults) may differ in ways the function body cannot show. Phase 1 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`
requires differential testing before any extraction, and this tool supplies the
*input list* for that work, nothing more.

Standard library only: it runs as a pytest test and as a git hook.

Usage:
    python scripts/extractor_parity_report.py                    # ratchet check
    python scripts/extractor_parity_report.py --detail           # per-pair table
    python scripts/extractor_parity_report.py --list --pair ideal:twint
    python scripts/extractor_parity_report.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "extractor_parity_baseline.json"

# Extractor files, keyed by the short name used on the command line. Only files
# under `services/protocol-payment/` are eligible: this tool exists to police
# that cluster specifically, and `sms_tool/` has its own guards.
SERVICES_ROOT = "services/protocol-payment"

PAIRS: tuple[tuple[str, str], ...] = (
    ("ideal", "twint"),
    ("ideal", "blik"),
    ("twint", "blik"),
    ("momo", "kakao"),
)

# The momo/kakao pair is a deliberate NEGATIVE CONTROL: it measured 2.4% line
# overlap and 50 shared lines, i.e. these two are independent implementations.
# A parity tool that reports large agreement here has a broken predicate. The
# test suite asserts this pair stays near-zero, which is what makes the
# ideal/twint number meaningful rather than an artefact of over-normalisation.
CONTROL_PAIR = ("momo", "kakao")
CONTROL_MAX_DUPLICATES = 5

_SKIP_DIRS = {"__pycache__"}

# Provider-identifying tokens. Erased from identifiers and string constants so
# that `stripe_create_ideal_pm` and `stripe_create_twint_pm` compare equal.
PROVIDER_TOKENS: tuple[str, ...] = (
    "ideal", "IDEAL", "Ideal",
    "twint", "TWINT", "Twint",
    "blik", "BLIK", "Blik",
    "momo", "MOMO", "Momo",
    "kakao", "KAKAO", "Kakao",
    "pix", "PIX", "Pix",
)

NORMALISED = "PROVIDER"

# Fixture table, run on every invocation before any scan. A parity predicate is
# a single point of failure: if it silently over-normalises, it will report that
# two unrelated files are identical and the ratchet becomes meaningless. These
# cases pin both directions. See `predicates_are_trustworthy`.
FIXTURES: tuple[tuple[str, str, bool], ...] = (
    # (source_a, source_b, expected_identical?)
    #
    # --- provider-suffixed siblings must pair up and compare equal ---
    # This is the case that a naive exact-name join gets WRONG: it buckets both
    # as file-specific and hides them from the duplication count.
    ("def f_ideal(x):\n    return x + 1\n",
     "def f_twint(x):\n    return x + 1\n", True),
    ("def stripe_create_ideal_pm(x):\n    return x + 1\n",
     "def stripe_create_twint_pm(x):\n    return x + 1\n", True),
    # same pairing, but the body genuinely differs
    ("def f_ideal(x):\n    return x + 1\n",
     "def f_twint(x):\n    return x - 1\n", False),
    ("def f_ideal(x):\n    return x > 1\n",
     "def f_twint(x):\n    return x >= 1\n", False),
    # provider token in a callee name only -> still the same shape
    ("def f_ideal(x):\n    return ideal_helper(x)\n",
     "def f_twint(x):\n    return twint_helper(x)\n", True),
    # differing statement count must not normalise away
    ("def f_ideal(x):\n    return x\n",
     "def f_ideal(x):\n    return x\n    return x\n", False),
    # A provider token inside a STRING constant must normalise too, otherwise
    # every URL/log-message difference shows up as a false "different".
    ('def f_ideal():\n    return "https://ideal.example/x"\n',
     'def f_twint():\n    return "https://twint.example/x"\n', True),
    # ...but a genuinely different URL must NOT normalise away to a match.
    ('def f_ideal():\n    return "https://ideal.example/a"\n',
     'def f_twint():\n    return "https://twint.example/b"\n', False),
    # --- differently-named, unrelated functions must NOT pair ---
    ("def alpha(x):\n    return x + 1\n",
     "def beta(x):\n    return x + 1\n", False),
    # --- whitespace / docstring differences are not real differences ---
    ("def f_ideal(x):\n    return x + 1\n",
     "def f_twint(x):\n    # a comment\n    return x + 1\n", True),
    # Docstrings are prose, not behaviour.  This fixture is the regression guard
    # for the bug fixed on 2026-09-17: `ast.unparse` round-trips docstrings
    # faithfully, so before `_drop_docstrings` existed the two bodies below
    # compared as *different* and the report understated duplication.
    #
    # 🔴 The docstrings here differ by a NON-provider word ("alpha" vs "beta").
    # An earlier version of this fixture used "ideal ledger" vs "twint ledger",
    # which `_sub` erases on its own -- so the fixture passed even with
    # `_drop_docstrings` fully disabled, and mutation testing could not see the
    # predicate at all.  A guard that a broken implementation still satisfies is
    # not a guard.  Keep the non-provider word.
    ('def f_ideal(x):\n    """Read the alpha ledger."""\n    return x + 1\n',
     'def f_twint(x):\n    """Read the beta ledger."""\n    return x + 1\n', True),
    # ...but a docstring-only node is still a node: dropping prose must not turn
    # a body with a real statement difference into a match.
    ('def f_ideal(x):\n    """Read the ledger."""\n    return x + 1\n',
     'def f_twint(x):\n    """Read the ledger."""\n    return x + 2\n', False),
    # --- delegating stubs must NOT count as duplication ---
    # Extraction creates these on purpose; counting them inverts the metric's
    # direction (see `is_delegating_stub`).  Two identical forwarders => the
    # function pair must be invisible, so "identical" is empty here.
    ("def proxy_short_ideal(x):\n    return shared_proxy_short(x, normalize)\n",
     "def proxy_short_twint(x):\n    return shared_proxy_short(x, normalize)\n", False),
    ('def proxy_key_ideal(x):\n    """Delegate to common/proxy_bookkeeping.py."""\n'
     "    return shared_proxy_key(x, normalize)\n",
     'def proxy_key_twint(x):\n    """Delegate to common/proxy_bookkeeping.py."""\n'
     "    return shared_proxy_key(x, normalize)\n", False),
    # A stub must be a *pure* forwarder.  A body with real work plus a shared_
    # call is still real logic and must stay counted -- otherwise wrapping a
    # duplicated body in a shared_ call would launder it out of the metric.
    ("def f_ideal(x):\n    y = x + 1\n    return shared_helper(y)\n",
     "def f_twint(x):\n    y = x + 1\n    return shared_helper(y)\n", True),
    # Same, for a body that computes something before delegating.
    ("def f_ideal(x):\n    return shared_helper(x) + 1\n",
     "def f_twint(x):\n    return shared_helper(x) + 1\n", True),
)


def _drop_docstrings(node: ast.AST) -> None:
    """Remove every docstring in ``node``, in place.

    A docstring is ``Expr(Constant(str))`` as the first statement of a
    ``Module`` / ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef``.  Nothing
    else is touched -- in particular a bare string expression that is *not* in
    first position is left alone, because there it is a real (if pointless)
    statement and dropping it would hide a difference rather than reveal one.
    """
    for child in ast.walk(node):
        body = getattr(child, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)


def _normalise(node: ast.AST, *, drop_name: str | None = None) -> str:
    """AST dump with provider-identifying tokens erased.

    Normalises ``Name.id``, ``arg.arg``, ``Attribute.attr``, ``arg`` keywords and
    string constants. Deliberately does NOT normalise numbers, comparison
    operators, or structure -- those are what "genuinely different" means here.

    ``drop_name`` replaces the function's own ``name`` field with a constant. It
    is required when comparing provider-suffixed siblings: their bodies can be
    byte-identical while the ``FunctionDef.name`` differs (``f_ideal`` vs
    ``f_twint``), and leaving the name in makes every sibling look "different".

    The round-trip through ``ast.unparse`` before re-parsing is intentional: it
    discards original formatting and comments, so two functions that differ only
    in whitespace compare equal.

    Docstrings are dropped explicitly (:func:`_drop_docstrings`).  ``ast.unparse``
    does **not** discard them -- it cannot, since a docstring is a real statement
    that round-trips faithfully -- so without that step two bodies that differ
    only in their prose would be reported as "different".  That was a live bug
    until 2026-09-17: the parity report's ``identical`` counts were measured with
    docstrings counted as content, which understated duplication.  Fixing it can
    only move functions from ``different`` to ``identical``, never the reverse.
    """
    try:
        fresh = ast.parse(ast.unparse(node))
    except (SyntaxError, RecursionError, ValueError):
        # ast.unparse can fail on exotic nodes; fall back to the node as given
        # rather than silently treating the function as absent.
        fresh = node

    _drop_docstrings(fresh)

    for child in ast.walk(fresh):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child.name = NORMALISED

    class Erase(ast.NodeTransformer):
        """Substitutes every provider token, leaving the rest of each identifier
        or string intact. Wholesale replacement would collapse distinct names --
        ``ideal_url`` and ``twint_url`` both becoming ``PROVIDER`` is fine, but
        ``fetch_ideal`` and ``parse_ideal`` must stay distinguishable."""

        @staticmethod
        def _sub(value: str) -> str:
            for token in PROVIDER_TOKENS:
                value = value.replace(token, NORMALISED)
            return value

        def visit_Name(self, n: ast.Name) -> ast.AST:
            n.id = self._sub(n.id)
            return n

        def visit_arg(self, n: ast.arg) -> ast.AST:
            n.arg = self._sub(n.arg)
            return n

        def visit_Attribute(self, n: ast.Attribute) -> ast.AST:
            self.generic_visit(n)
            n.attr = self._sub(n.attr)
            return n

        def visit_keyword(self, n: ast.keyword) -> ast.AST:
            self.generic_visit(n)
            if n.arg:
                n.arg = self._sub(n.arg)
            return n

        def visit_Constant(self, n: ast.Constant) -> ast.AST:
            # Substituting in place rather than blanking the value: replacing the
            # whole string would erase the rest of it too, so "https://x/a" and
            # "https://x/b" would compare equal -- a false "identical" that
            # inflates the duplicate count.
            if isinstance(n.value, str):
                n.value = self._sub(n.value)
            return n

    return ast.dump(Erase().visit(fresh))


def normalise_name(name: str) -> str:
    """Erase provider tokens from a function name, for pairing only.

    ``stripe_create_ideal_pm`` -> ``stripe_create_PROVIDER_pm`` so it can be
    matched against its ``twint`` sibling. Case-insensitive because the token
    appears as ``ideal``/``Ideal``/``IDEAL`` across the cluster.
    """
    out = name
    for token in PROVIDER_TOKENS:
        out = out.replace(token, NORMALISED)
    return out


def top_level_functions(source: str) -> dict[str, str]:
    """Map ``normalised name -> normalised AST`` for a module source.

    Only top-level ``def`` / ``async def``. Nested functions and methods are
    part of their parent's fingerprint, which is the right granularity: a helper
    that exists only to serve one function should move with it.

    The key is the *normalised* name (see module docstring). If two functions in
    the same file normalise to the same key -- e.g. ``f_ideal`` and ``f_twint``
    in one file -- the later one is kept under a ``#2`` disambiguator rather than
    silently overwriting its sibling, which would hide a real function.

    Delegating stubs are **excluded**.  See :func:`is_delegating_stub` for why:
    once a body has been extracted to ``common/``, each extractor keeps a
    one-line forwarder, and those forwarders are trivially identical to each
    other.  Counting them as "duplication" makes the ratchet move the wrong way
    the moment you succeed at the extraction, which is a trap this metric fell
    into on 2026-09-17.
    """
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if is_delegating_stub(node):
                continue
            key = normalise_name(node.name)
            if key in out:
                suffix = 2
                while f"{key}#{suffix}" in out:
                    suffix += 1
                key = f"{key}#{suffix}"
            out[key] = _normalise(node)
    return out


def is_delegating_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when ``node`` only forwards to a shared ``common/`` helper.

    Recognised shapes -- the whole body, docstring aside, is a single call to a
    name starting with ``shared_``.  Both forms occur:

        def proxy_key(proxy: str) -> str:
            \"\"\"Delegate to common/proxy_bookkeeping.py.\"\"\"
            return shared_proxy_key(proxy, normalize_proxy_url)

        def dump_http(response, stage, ...) -> None:
            \"\"\"Delegate to common/http_dump.py.\"\"\"
            shared_dump_http(response, stage, ..., dump_env="IDEAL_DUMP")

    The second form (a bare expression-statement call, no ``return``) is what a
    void function delegates with.  It went unrecognised at first, which made
    batch 4 look like a no-op: the real duplicate body was removed (-1) while
    the two new stubs were counted as a new duplicate pair (+1).

    Why this exists
    ---------------
    Extraction *creates* these on purpose.  Six identical forwarders would
    otherwise be counted as six duplicated functions, so performing the
    consolidation the metric is meant to drive would make its number go **up**.
    That is exactly what happened when ``proxy_bookkeeping.py`` landed: the
    ratchet reported +1 / +3 / +3 on a change that deleted ~90 lines of real
    logic from every extractor.

    The predicate is deliberately narrow: a stub must forward to a ``shared_*``
    name *and* have nothing else in its body.  A function with real logic that
    merely happens to also call a ``shared_*`` helper is still counted, because
    its behaviour is still worth de-duplicating.
    """
    body = node.body
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return False
        call = stmt.value
    elif isinstance(stmt, ast.Expr):
        # void delegation: `shared_dump_http(...)` with no `return`
        call = stmt.value
    else:
        return False
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = func.id if isinstance(func, ast.Name) else None
    return bool(name and name.startswith("shared_"))


def compare_sources(a: str, b: str) -> dict[str, list[str]]:
    """Classify top-level functions from two module sources.

    Returns ``{"identical": [...], "different": [...], "only_a": [...],
    "only_b": [...]}``, each list sorted by function name. Names are reported as
    the normalised key, so a matched pair appears under one label.
    """
    fa = top_level_functions(a)
    fb = top_level_functions(b)
    identical = sorted(n for n in fa.keys() & fb.keys() if fa[n] == fb[n])
    different = sorted(n for n in fa.keys() & fb.keys() if fa[n] != fb[n])
    return {
        "identical": identical,
        "different": different,
        "only_a": sorted(fa.keys() - fb.keys()),
        "only_b": sorted(fb.keys() - fa.keys()),
    }


def predicates_are_trustworthy() -> str | None:
    """Run FIXTURES; return ``None`` when healthy, else a failure description.

    Called at the top of ``main`` so a weakened predicate fails LOUDLY instead of
    quietly reporting a clean or catastrophically-duplicated tree.
    """
    for i, (a, b, want_same) in enumerate(FIXTURES):
        try:
            got = compare_sources(a, b)
        except Exception as exc:  # pragma: no cover - defensive
            return f"fixture {i} raised {type(exc).__name__}: {exc}"
        got_same = bool(got["identical"])
        if got_same != want_same:
            return (
                f"fixture {i}: expected identical={want_same}, got {got_same} "
                f"(identical={got['identical']}, different={got['different']})"
            )
    return None


def extractor_path(name: str, root: Path = ROOT) -> Path:
    """Resolve a short name like ``ideal`` to its extractor file.

    Searched as ``<root>/<SERVICES_ROOT>/<name>/*.py`` and the file whose name
    contains ``extract`` wins, so the layout can change without a code edit.
    """
    folder = root / SERVICES_ROOT / name
    if not folder.is_dir():
        raise FileNotFoundError(f"no extractor folder for {name!r}: {folder}")
    candidates = sorted(
        p for p in folder.glob("*.py")
        if p.name != "__init__.py" and not (_SKIP_DIRS & set(p.parts))
    )
    if not candidates:
        raise FileNotFoundError(f"no .py file in {folder}")
    preferred = [p for p in candidates if "extract" in p.name]
    return (preferred or candidates)[0]


def pair_report(a: str, b: str, root: Path = ROOT) -> dict:
    """Compare two named extractors, returning counts plus the name lists."""
    src_a = extractor_path(a, root).read_text(encoding="utf-8")
    src_b = extractor_path(b, root).read_text(encoding="utf-8")
    result = compare_sources(src_a, src_b)
    result["duplicates"] = len(result["identical"])
    result["total_a"] = len(result["identical"]) + len(result["different"]) + len(result["only_a"])
    result["total_b"] = len(result["identical"]) + len(result["different"]) + len(result["only_b"])
    return result


def collect(root: Path = ROOT) -> tuple[dict[str, int], dict[str, dict]]:
    """Return ``({pair_key: duplicates}, {pair_key: report})`` over PAIRS."""
    counts: dict[str, int] = {}
    reports: dict[str, dict] = {}
    for a, b in PAIRS:
        key = f"{a}:{b}"
        report = pair_report(a, b, root)
        counts[key] = int(report["duplicates"])
        reports[key] = report
    return counts, reports


def load_baseline(path: Path | None = None) -> dict:
    return json.loads((path or BASELINE).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--detail", action="store_true", help="print the per-pair table")
    parser.add_argument("--list", action="store_true", help="print function names")
    parser.add_argument(
        "--pair", help="restrict --list/--detail output to one pair, e.g. ideal:twint"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after REMOVING duplication)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    # Self-test first: everything below is meaningless if this fails.
    broken = predicates_are_trustworthy()
    if broken is not None:
        print(f"SELF-TEST FAILED: {broken}", file=sys.stderr)
        print(
            "extractor-parity: refusing to report on a predicate that fails its "
            "own fixtures.",
            file=sys.stderr,
        )
        return 1

    counts, reports = collect(args.root)

    if args.list or args.detail:
        for key in sorted(reports):
            if args.pair and key != args.pair:
                continue
            r = reports[key]
            print(f"\n=== {key} ===")
            if args.detail:
                print(f"  identical : {len(r['identical']):3d}  of {r['total_a']} (a) / {r['total_b']} (b)")
                print(f"  different : {len(r['different']):3d}")
                print(f"  only {key.split(':')[0]:<6}: {len(r['only_a']):3d}")
                print(f"  only {key.split(':')[1]:<6}: {len(r['only_b']):3d}")
            if args.list:
                print(f"  identical ({len(r['identical'])}): {r['identical']}")
                print(f"  different ({len(r['different'])}): {r['different']}")
                print(f"  only_a ({len(r['only_a'])}): {r['only_a']}")
                print(f"  only_b ({len(r['only_b'])}): {r['only_b']}")

    if args.update_baseline:
        previous = load_baseline() if BASELINE.exists() else {"pairs": {}}
        payload = {"pairs": {k: {"duplicates": v} for k, v in sorted(counts.items())}}
        # newline="\n": Path.write_text is TEXT mode on Windows and would turn the
        # terminator into CRLF, making the committed baseline differ across
        # machines by line ending alone. Learned the hard way on this repo.
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {BASELINE.name}")
        for key in sorted(counts):
            before = int(previous.get("pairs", {}).get(key, {}).get("duplicates") or 0)
            after = counts[key]
            delta = after - before
            flag = " " if delta == 0 else ("+" if delta > 0 else "!")
            print(f"  {flag} {key:<14} {before:3d} -> {after:3d} ({delta:+d})")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    baseline = load_baseline()
    grown: list[tuple[str, int, int, list[str]]] = []
    for key in sorted(counts):
        allowed = int(baseline.get("pairs", {}).get(key, {}).get("duplicates") or 0)
        current = counts[key]
        if current <= allowed:
            continue
        # Which functions are newly duplicated? Compare against the recorded
        # name list if we have one, else just report the count change.
        grown.append((key, current, allowed, []))

    if grown:
        for key, current, allowed, _new in grown:
            print(
                f"{key} extractor-parity ratchet: {current} duplicated functions "
                f"> baseline {allowed} (+{current - allowed}). "
                f"Extract the shared function instead of copying it, or justify a "
                f"baseline bump.",
                file=sys.stderr,
            )
        return 1

    # Negative control: momo/kakao are independent implementations. If they ever
    # look alike, the predicate has over-normalised and every other number is junk.
    control_key = f"{CONTROL_PAIR[0]}:{CONTROL_PAIR[1]}"
    control = counts.get(control_key)
    if control is not None and control > CONTROL_MAX_DUPLICATES:
        print(
            f"extractor-parity control FAILED: {control_key} reports {control} "
            f"identical functions, expected <= {CONTROL_MAX_DUPLICATES}. These two "
            f"are independent implementations, so this means the normaliser is "
            f"erasing real differences and the other pairs' numbers are unusable.",
            file=sys.stderr,
        )
        return 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"extractor-parity ratchet OK ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Gate: refuse a commit whose vendored Sentinel assets disagree with the pins.

Why this exists
---------------
``sms_tool/sentinel/bundle.py`` pins the SHA256 of the two vendored runtime
assets (``sdk.js``, ``sentinel-runner.js``) and ``validate_runtime_bundle()``
refuses to run the Node runner when either digest drifts.  That check is
correct -- but until 2026-09-18 nothing verified the pins *before* they reached
a batch.

What that cost, measured
------------------------
On 2026-09-17 21:16 ``sentinel-runner.js`` was rewritten from CRLF to LF by a
routine file rewrite.  The content was byte-identical after normalisation (1438
lines, 57592 -> 56154 bytes -- a difference of exactly the CRLF count), but the
raw digest moved from ``334ceb33...`` to ``b388b2e2...`` while the pin in
``bundle.py`` stayed at the old value.

The result, on the batch that started at 21:18:

* ``validate_runtime_bundle()`` raised ``SentinelBundleError`` on every call;
* ``issue_sentinel_flow()`` swallowed it and fell back to the legacy issuer;
* the legacy issuer also produced no token, so every account reported
  ``sentinel_legacy_incomplete`` -- wording that reads like an upstream channel
  fault;
* **126 of 126 accounts failed.**  42 of them died at ``create_account``.

Neither existing gate could see it.  ``line_ending_guard`` blocks a file that
*mixes* CRLF and LF internally; this file was uniformly LF, so it passed.  And
``git status`` is blind to the change by construction: with
``.gitattributes`` pinning ``*.js text eol=lf`` the index entry is already LF,
so normalising the worktree to LF produces no diff at all.

That is the gap this gate closes.  It compares the *content* of each vendored
asset against the pin the runtime will actually enforce, using the very same
normalised digest the runtime uses -- so the pin and the asset can never
disagree silently again.

Relationship to line_ending_guard
---------------------------------
They are complementary, not redundant:

* ``line_ending_guard``  -- "one file must not mix endings internally";
* ``sentinel_asset_guard`` -- "a pinned asset must match its pin".

A file can satisfy the first and still break the second (uniform CRLF -> LF is
exactly that case).  See ``docs/audits/scan-2026-09-18-sentinel-runner-hash-mismatch.md``.

Standard library only: the hook may be run by any interpreter on PATH.

Usage:
    python scripts/sentinel_asset_guard.py          # check, fail on mismatch
    python scripts/sentinel_asset_guard.py --dry    # report only, never fail
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "sms_tool" / "sentinel" / "bundle.py"

# (asset filename, pin attribute on the bundle module).  Order is the order the
# runtime validates them in, so the first reported offender matches what a live
# run would trip on first.
PINNED_ASSETS: tuple[tuple[str, str], ...] = (
    ("sdk.js", "SDK_SHA256"),
    ("sentinel-runner.js", "RUNNER_SHA256"),
)


def load_bundle_module():
    """Load ``bundle.py`` by path, without importing the ``sms_tool`` package.

    ``bundle.py`` itself is standard-library only, but ``sms_tool/sentinel/__init__.py``
    pulls in ``curl_cffi`` via ``client``.  Loading the file directly keeps this
    gate runnable by whatever interpreter the hook finds on PATH.
    """
    spec = importlib.util.spec_from_file_location("_sentinel_bundle_guard", BUNDLE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {BUNDLE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_is_normalised(bundle) -> str | None:
    """None if ``_digest`` ignores line-ending flavour, else a description.

    Self-test, in the spirit of ``line_ending_guard.classifier_is_trustworthy``.
    This gate delegates normalisation to the runtime's own ``_digest`` so the
    two can never drift -- but that also means a regression *in* ``_digest``
    would silently gut the gate.  Pin the property instead: CRLF and LF content
    must hash alike.
    """
    if not hasattr(bundle, "_digest"):
        return "bundle module has no _digest"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            crlf = Path(tmp) / "crlf"
            lf = Path(tmp) / "lf"
            crlf.write_bytes(b"a\r\nb\r\nc\r\n")
            lf.write_bytes(b"a\nb\nc\n")
            if bundle._digest(crlf) != bundle._digest(lf):
                return "_digest is sensitive to line endings (CRLF != LF)"
            other = Path(tmp) / "other"
            other.write_bytes(b"a\nb\nc\nd\n")
            if bundle._digest(other) == bundle._digest(lf):
                return "_digest ignores real content differences"
    except OSError as exc:
        return f"self-test could not run: {exc}"
    return None


def check_assets(bundle) -> list[tuple[str, str, str]]:
    """[(asset, pinned, actual)] for every asset that disagrees with its pin."""
    runtime_dir = Path(bundle.RUNTIME_DIR)
    offenders: list[tuple[str, str, str]] = []
    for name, pin_name in PINNED_ASSETS:
        path = runtime_dir / name
        pinned = str(getattr(bundle, pin_name, "") or "")
        if not path.is_file():
            offenders.append((name, pinned, "<missing>"))
            continue
        actual = bundle._digest(path)
        if actual != pinned:
            offenders.append((name, pinned, actual))
    return offenders


def main(argv: list[str]) -> int:
    dry = "--dry" in argv

    try:
        bundle = load_bundle_module()
    except Exception as exc:  # noqa: BLE001 - the gate must never crash the hook
        print(
            f"sentinel-asset-guard: could not load {BUNDLE_PATH}: {exc}",
            file=sys.stderr,
        )
        return 1

    broken = digest_is_normalised(bundle)
    if broken is not None:
        print(
            "sentinel-asset-guard: SELF-TEST FAILED -- the digest is not behaving "
            f"as specified: {broken}\n"
            "This gate cannot be trusted until that is fixed.  Do NOT bypass it "
            "with --no-verify; fix sms_tool/sentinel/bundle.py::_digest.",
            file=sys.stderr,
        )
        return 1

    offenders = check_assets(bundle)
    if not offenders:
        print(f"sentinel-asset-guard: {len(PINNED_ASSETS)} pinned asset(s) match their digests")
        return 0

    print(
        f"sentinel-asset-guard: {len(offenders)} vendored asset(s) disagree with "
        "sms_tool/sentinel/bundle.py.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for name, pinned, actual in offenders:
        print(f"  {name}", file=sys.stderr)
        print(f"    pinned: {pinned}", file=sys.stderr)
        print(f"    actual: {actual}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "The runtime validates these pins on every issue call.  A mismatch makes\n"
        "validate_runtime_bundle() raise, which silently degrades Sentinel issuance\n"
        "and can fail an entire batch (126/126 on 2026-09-17).\n"
        "\n"
        "Pick ONE, deliberately:\n"
        "  * asset changed on purpose -> update the pin in bundle.py to the\n"
        "    'actual' digest above, then re-run this gate;\n"
        "  * asset changed by accident -> restore it, e.g.\n"
        "    git checkout -- sms_tool/sentinel/runtime/<asset>\n"
        "\n"
        "Do NOT 'fix' this by rewriting the asset's line endings: the digest is\n"
        "computed over newline-normalised content precisely so that checkout\n"
        "flavour cannot break it (see bundle.py::_digest).",
        file=sys.stderr,
    )

    if dry:
        print("sentinel-asset-guard: --dry set, not failing", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Pin the bundled Python runtime and the staging contract around it.

The installer's self-contained promise rests on one artifact: the CPython
archive named in ``scripts/runtime_manifest.json``. Nothing else in the tree can
verify that the shipped interpreter is the pinned one, so these tests keep the
manifest, the staging script, the payload gate and the build wiring from
drifting apart.

The version check is deliberately duplicated from CI: a manifest that says 3.12
while CI builds 3.13 would produce a runtime whose tests ran under a different
interpreter than the one users get.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "scripts" / "runtime_manifest.json"
STAGING_SCRIPT = ROOT / "scripts" / "stage_python_runtime.ps1"
INSTALLER_SCRIPT = ROOT / "scripts" / "build_installer.ps1"
PAYLOAD_GATE = ROOT / "scripts" / "scan_release_payload.py"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_pins_a_verifiable_windows_artifact() -> None:
    manifest = _manifest()

    assert manifest["schema"] == "python_runtime.v1"
    assert manifest["platform"] == "win-x64"
    assert "python-build-standalone" in manifest["distribution"]
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])
    assert manifest["asset"].endswith("install_only.tar.gz")
    assert manifest["python_full_version"].startswith(manifest["python_version"] + ".")

    # The URL percent-encodes the release tag's '+', so compare the decoded form.
    decoded = urllib.parse.unquote(manifest["url"])
    assert manifest["release"] in decoded
    assert manifest["asset"] in decoded


def test_runtime_version_matches_ci_and_the_lint_target() -> None:
    manifest = _manifest()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'python-version: "{manifest["python_version"]}"' in ci

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lint_target = "py" + manifest["python_version"].replace(".", "")
    assert f'target-version = "{lint_target}"' in pyproject


def test_staging_script_verifies_the_digest_before_extracting() -> None:
    script = STAGING_SCRIPT.read_text(encoding="utf-8")

    assert "runtime_manifest.json" in script
    assert "Get-FileHash" in script
    assert "Refusing to extract" in script
    # A fail-open stage would let a tampered archive land in the package.
    assert "Remove-Item -LiteralPath $archivePath -Force" in script


def test_payload_gate_verifies_the_runtime_stamp_instead_of_trusting_it() -> None:
    gate = PAYLOAD_GATE.read_text(encoding="utf-8")

    assert "PINNED_RUNTIME_STAMP" in gate
    assert "RUNTIME-STAMP.json" in gate
    assert "does not match" in gate


def test_installer_bundles_the_runtime_and_keeps_a_dev_escape_hatch() -> None:
    installer = INSTALLER_SCRIPT.read_text(encoding="utf-8-sig")

    assert "$SkipBundledPython" in installer
    assert "RUNTIME-STAMP.json" in installer
    assert "runtime\\python" in installer


def test_payload_gate_rejects_a_stamp_from_another_release(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    # ``scripts`` is not an importable package; the gate is loaded from its
    # file path at runtime, which pyright cannot resolve statically.
    # ``scripts`` is not an importable package; the gate is loaded from its
    # file path at runtime, which pyright cannot resolve statically.
    import scan_release_payload as gate  # pyright: ignore[reportMissingImports]

    runtime = tmp_path / "runtime" / "python"
    runtime.mkdir(parents=True)
    stamp = runtime / "RUNTIME-STAMP.json"
    stamp.write_text(json.dumps({"sha256": "0" * 64, "release": "other"}), encoding="utf-8")

    failures = gate.check_pinned_runtime(tmp_path)
    assert failures
    assert any("does not match" in item for item in failures)

    manifest = _manifest()
    stamp.write_text(
        json.dumps({"sha256": manifest["sha256"], "release": manifest["release"]}),
        encoding="utf-8",
    )
    assert gate.check_pinned_runtime(tmp_path) == []

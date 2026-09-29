"""Every protocol-payment extractor must bootstrap its own imports.

Why this exists
---------------
The host launches each extractor as ``python <script>`` with ``cwd=<its own
dir>`` (``sms_tool/pay_link/adapters.py``): it sets ``PYTHONUTF8`` and the
extractor's env contract, but **never** ``PYTHONPATH``. So the extractor has to
make ``common/`` importable itself, and the ``momo`` / ``pix`` modules have to
make their own siblings (``ac_paylink_core`` / ``pix_core``) importable.

``test_extractors_contract.py`` inserts both the extractor dir and the protocol
root before importing, so it passes even when an extractor forgot its own
bootstrap -- the exact defect this guard closes. It imports each module in a
subprocess with a stripped ``PYTHONPATH`` and ``cwd=<script dir>`` and asserts
the import succeeds, i.e. the environment the host actually provides.

Why the bootstrap is inline and not a shared module
---------------------------------------------------
A ``services/protocol-payment/_bootstrap.py`` cannot be imported before the
protocol root is on ``sys.path`` -- importing it *is* the thing that needs the
path. The same chicken-and-egg rules out ``from common.bootstrap import ...``.
Factoring it into a package launched with ``python -m`` would change the host's
``cwd`` and every provider's absolute import, which is a larger, riskier change
than the duplication it removes. The guard below is the cheap part that matters:
a new extractor that forgets the prologue fails here instead of at runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "services" / "protocol-payment"

#: Host-launched entry points (``spec.script`` in pay_link/registry) plus the
#: library modules whose *sibling* import is the fragile part (momo / pix use a
#: top-level ``import ac_paylink_core`` / ``import pix_core``).
MODULES: tuple[tuple[str, str], ...] = (
    ("ideal", "ideal/ideal_qr_extract.py"),
    ("twint", "twint/twint_extract.py"),
    ("blik", "blik/blik_qr_extract.py"),
    ("kakao", "kakao/kakao_extract.py"),
    ("direct_card", "direct_card/direct_card_extract.py"),
    ("momo_runner", "momo/run_momo.py"),
    ("pix_runner", "pix/run_pix.py"),
    ("momo_lib", "momo/momo_qr_extract.py"),
    ("ac_paylink_core", "momo/ac_paylink_core.py"),
    ("pix_lib", "pix/pix_extract.py"),
)

_PROBE = (
    "import importlib.util,sys;"
    "p=sys.argv[1];"
    "s=importlib.util.spec_from_file_location('_probe_mod',p);"
    "m=importlib.util.module_from_spec(s);"
    "sys.modules['_probe_mod']=m;"  # dataclasses need the module registered
    "s.loader.exec_module(m);"
    "print('IMPORT_OK')"
)


def _import_clean(script: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _PROBE, str(script)],
        cwd=str(cwd or script.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@pytest.mark.parametrize("name,rel", MODULES, ids=[name for name, _ in MODULES])
def test_extractor_bootstraps_its_imports(name: str, rel: str) -> None:
    script = PROTOCOL_ROOT / rel
    assert script.is_file(), f"{name}: missing {script}"
    result = _import_clean(script)
    assert result.returncode == 0 and "IMPORT_OK" in result.stdout, (
        f"{name} ({rel}) could not be imported with a clean PYTHONPATH and "
        f"cwd=<its dir> -- the environment the host provides. It must bootstrap "
        f"its own sys.path (common/ and, for momo/pix, its sibling module).\n"
        f"stdout: {result.stdout[-400:]}\nstderr: {result.stderr[-800:]}"
    )


#: Library modules that import a *sibling* rather than ``common/``. They used to
#: rely on the runner putting their own directory on ``sys.path``; a direct
#: import from another cwd has to work too.
SIBLING_LIBS: tuple[tuple[str, str], ...] = (
    ("momo_lib", "momo/momo_qr_extract.py"),
    ("ac_paylink_core", "momo/ac_paylink_core.py"),
    ("pix_lib", "pix/pix_extract.py"),
)


@pytest.mark.parametrize("name,rel", SIBLING_LIBS, ids=[name for name, _ in SIBLING_LIBS])
def test_sibling_library_bootstraps_itself_from_a_neutral_cwd(name: str, rel: str) -> None:
    """Importing a library module from a neutral cwd must still find its sibling."""
    script = PROTOCOL_ROOT / rel
    assert script.is_file(), f"{name}: missing {script}"
    result = _import_clean(script, cwd=PROTOCOL_ROOT)
    assert result.returncode == 0 and "IMPORT_OK" in result.stdout, (
        f"{name} ({rel}) could not be imported from cwd={PROTOCOL_ROOT}. It must "
        f"put its own directory on sys.path before importing its sibling, not "
        f"rely on the caller."
        f"\nstdout: {result.stdout[-400:]}\nstderr: {result.stderr[-800:]}"
    )

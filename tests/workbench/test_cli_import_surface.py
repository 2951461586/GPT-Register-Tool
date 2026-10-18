"""Import-surface guard for the CLI entrypoint.

`import sms_tool.cli` used to pull every command adapter, `batch_runner`,
`requests`, `curl_cffi` and the vendored UPI package before argparse ran --
~890 ms of imports for a help screen, paid again by every one-shot backend
spawn. The 2026-10-18 split moved those behind `_LazyModule` proxies and
self-healing resolvers, so the import surface is now a contract:

* the lightweight commands (`--help`, `--doctor`, `--desktop-read`) must not
  drag a browser/HTTP stack into their process;
* the symbols themselves must still resolve on first use (the proxies and
  resolvers are exercised through the normal CLI paths by every other test).

The check runs in a child interpreter: this test session has almost certainly
imported `requests` already, so inspecting `sys.modules` in-process would pass
no matter what the entrypoint does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HEAVY_PACKAGES = ("requests", "curl_cffi", "playwright", "urllib3")
HEAVY_MODULES = (
    "sms_tool.batch_runner",
    "sms_tool.commands.payment_links",
    "sms_tool.commands.registration",
    "sms_tool.gen_pp_link",
    "sms_tool.proxy_routing",
    "sms_tool.upi_link",
)

_PROBE = f"""
import json, sys
import sms_tool.cli
heavy = sorted({{m.split('.')[0] for m in sys.modules if m.split('.')[0] in {HEAVY_PACKAGES!r}}})
modules = [m for m in {HEAVY_MODULES!r} if m in sys.modules]
print(json.dumps({{"heavy": heavy, "modules": modules}}))
"""


def _import_surface() -> dict[str, list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_importing_cli_loads_no_heavy_integration():
    assert _import_surface() == {"heavy": [], "modules": []}


def test_lazy_command_proxy_resolves_on_first_access():
    import sms_tool.cli as cli

    # Attribute access is what triggers the import; the proxy must expose the
    # module's real members (and be replaceable by patch.object, hence no
    # __slots__).
    assert cli.registration_commands.RegistrationCommandContext is not None
    assert cli.payment_commands.PaymentCommandContext is not None

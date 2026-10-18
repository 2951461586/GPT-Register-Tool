# Runtime Packaging

The Windows package is self-contained: it ships a pinned CPython under
`runtime/python` and the WPF host prefers it when no interpreter is configured.
This document records that contract — what is pinned, how it is verified, and
how to fall back when something is off.

## What ships

| Piece | Location | Owner |
| --- | --- | --- |
| Pinned interpreter | `<package>/runtime/python/python.exe` | `scripts/stage_python_runtime.ps1` |
| Pinned artifact record | `scripts/runtime_manifest.json` | Governance test |
| Staging stamp | `<package>/runtime/python/RUNTIME-STAMP.json` | Staging script |
| Installed dependency set | `<package>/runtime/python/PIP-FREEZE.txt` | Staging script |
| Desktop host | `<package>/dist/net10/SmsWorkbench.exe` | `SmsWorkbench/` |

The interpreter is `astral-sh/python-build-standalone` for `win-x64`, matching
the version CI runs Python tests under. The archive is identified by URL **and**
SHA-256 in the manifest; the staging script refuses to extract on a mismatch,
and the release payload gate refuses to ship a runtime whose stamp disagrees
with the manifest.

## Interpreter resolution

`PythonPathResolver` uses this order:

1. a non-default `runtime.python_path` setting (an explicit operator choice,
   including a development `.venv` or a bare `py`);
2. the bundled `runtime/python/python.exe`;
3. `python` from `PATH`.

Only the unconfigured default (`python`, or an empty setting) may be upgraded to
the bundled runtime. A configured value always wins, so pointing the app at a
venv cannot silently switch interpreters.

## Building a package

```powershell
# 1. Stage the pinned interpreter and its dependencies (network required).
.\scripts\stage_python_runtime.ps1

# 2. Build the package. The runtime is copied to runtime\python.
.\scripts\build_installer.ps1

# Dev-only package that relies on a checkout venv instead:
.\scripts\build_installer.ps1 -SkipBundledPython
```

`stage_python_runtime.ps1 -DryRun` validates the manifest and prints the
resolved plan without touching the network; CI and the governance test use it to
keep the script and the manifest honest.

A release build **fails** when the runtime is missing. `-SkipBundledPython`
exists for local packages, not for published assets.

## Still external

- **Node.js 20+** for the Sentinel runner. It is a separately versioned runtime
  with its own SDK bundle; `--doctor` reports it.
- **Browser binaries** for Playwright/Camoufox drivers, obtained by the driver
  itself when that path is used.

## Updating the pin

1. Pick the release asset from `python-build-standalone` (`win-x64`,
   `install_only.tar.gz`, the Python version CI uses).
2. Update `scripts/runtime_manifest.json` with the release, asset, URL and
   SHA-256.
3. Re-run `.\scripts\stage_python_runtime.ps1 -Force` and
   `python -m pytest tests/governance/test_runtime_manifest.py`.
4. Rebuild the package and confirm `--doctor --json` runs through the bundled
   interpreter.

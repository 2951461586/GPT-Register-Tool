# AGENTS.md

Working instructions for anyone (human or agent) changing this repository.
These are conventions the gates cannot enforce; everything else is enforced by
`.githooks/pre-commit` and `.github/workflows/ci.yml`.

## After every push: watch the run

```bash
python scripts/ci_watch.py            # newest ci run for HEAD, waits, exits non-zero if red
python scripts/ci_watch.py --no-wait  # just report the current state
```

A push is not finished when `git push` returns. The 2026-11-19 push passed the
full local suite (7623 passed) and all seven pre-commit gates, then went red in
CI in 1m27s: `scripts/environment_preflight.ps1` rejected a healthy
`curl_cffi 0.17.0` because a third copy of the accepted version range lived in a
PowerShell file no local gate reads.

**Rule: after a push, run `scripts/ci_watch.py` and fix the run before starting
the next task.** When a CI-only gate fails, prefer deleting the duplicated fact
over updating the copy — see `tests/governance/test_preflight_script_authority.py`
for how that one is pinned.

## The local gates are necessary, not sufficient

Before committing, from the repository root (Windows, `.venv` interpreter):

```bash
python -m pytest tests -q -n 4 --dist loadfile     # full suite
python scripts/format_guard.py                     # ruff-format, not just ruff check
python scripts/precommit_guard.py                  # what the hook itself runs
```

CI additionally runs, and nothing local covers them: `dotnet test`, the .NET
warning ratchet, `environment_preflight.ps1`, the secret scans, and
`architecture_scan.py`. If a change touches `.ps1`, `.cs`, `*.yml` or
`Directory.*.props`, assume CI is the first place it will be exercised.

## Facts live in one place

This repository's recurring defect is a duplicated fact drifting in one copy:
the curl_cffi version bracket (three copies), the Sentinel SDK version (code vs
`config.example.json`), the config-key ownership table (Python vs C#). When a
number, range or list already has an owner, import it — do not restate it in a
script, a doc or a template. If it must appear twice, add a test that compares
the two copies (see `tests/governance/test_sentinel_version_pin.py`).

## Secrets and runtime data

`config/proxy.json`, `runtime.json`, `payment.json`, sessions, mailboxes and
logs live outside the checkout (`%LOCALAPPDATA%\GPTRegisterTool\config`,
`runtime/`, `sessions/`) and are git-ignored. Never paste real proxy
credentials, API keys or session tokens into a commit, a test fixture or a doc.

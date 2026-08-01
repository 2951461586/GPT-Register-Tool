# v2026.08.01.1

This patch release contains all `v2026.08.01` account-pool cleanup, retired
module removal, and inbox plain-text rendering changes.

## Build fixes

- Corrected the CloakBrowser dependency range to the published `0.5.x` line so
  a clean Python 3.12 CI runner can install `requirements.txt`.
- Cleared the installer project's `CA1865` analyzer warnings by using the
  character overloads for archive-directory detection.

## Validation

- `python -m pytest -q`: 545 passed, 1 skipped, 7 subtests passed.
- `.dotnet/dotnet.exe test GPTRegisterTool.slnx --no-restore`: 20 passed.
- Clean release worktree installer and portable-package build: passed.

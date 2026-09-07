# v2026.08.01.2

This patch release contains all `v2026.08.01` account-pool cleanup, retired
module removal, and inbox plain-text rendering changes.

## Build fixes

- Corrected the CloakBrowser dependency range to the published `0.5.x` line.
- Added pytest to the single dependency manifest used by clean CI runners.
- Cleared the installer project's `CA1865` analyzer warnings.

## Validation

- `python -m pytest -q`: 545 passed, 1 skipped, 7 subtests passed.
- `.dotnet/dotnet.exe test GPTRegisterTool.slnx --no-restore`: 20 passed.
- Clean release worktree installer and portable-package build: passed.

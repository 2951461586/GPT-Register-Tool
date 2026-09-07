# Historical Audits

Everything in this directory is a historical evidence snapshot: audit rounds,
scan reports, exposure assessments, credential-rotation checklists, and
one-time migration records. Individual files may describe superseded code and
must not be treated as the current architecture contract. Current ownership and
rules live in `docs/architecture.md`, `docs/directory-map.md`, and
`docs/CONTEXT.md`.

Naming conventions for files placed here:

- `audit-YYYY-MM-DD-*.md` — dated audit rounds (cleanup, architecture,
  correctness, performance, quality backlog items).
- `scan_*_YYYY-MM-DD.md` — one-off scan reports (underscore naming predates the
  convention; kept as-is for the original filenames).
- `*-assessment-*.md` — security/behaviour assessments.
- `credential-rotation-*.md` — credential rotation runbooks and evidence.
- `headless-browser-registration-audit.md`,
  `browser-registration-risk-control-gap-*.md` — module-specific audits, kept
  verbatim with their original banners marking superseded conclusions.
- `sentinel-account-health-migration.md` — one-time migration record.

Release notes were archived to `docs/releases/` on 2026-09-06 and are indexed
from `docs/README.md`.

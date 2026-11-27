---
id: spec-18-line-ending-uniformity
type: architecture-design
status: accepted
owner: scripts/line_ending_guard.py
implements:
  - rule-17
depends-on:
  - adr-0013
---

# Tracked text files never mix CRLF and LF internally

Mixed line endings corrupt vendored asset hashes (such as `sentinel-runner.js`) and break cross-platform diffs. `scripts/line_ending_guard.py` enforces uniform endings across every tracked text file.

## Enforced by

- `rule-17` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.

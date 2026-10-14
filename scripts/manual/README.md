# Manual tools

Operator-run helpers that are not part of any gate, build or automated workflow.
Run them from the repository root as modules, for example
`python -m scripts.manual.probe_paypal_regions`.

| Script | Purpose |
|---|---|
| `probe_paypal_regions.py` | Which checkout regions expose PayPal (stages 1-2 of the extractor only, no link generation). |
| `probe_single_account_liveness.py` | Liveness check for one registered account (`--latest` or `--session`). |
| `recover_remail_tokens.py` | Recover `serviceToken` + `orderNo` for already-purchased Remail mailboxes. |

Nothing in the repository imports from this directory. Dated single-use scripts
(profile purge, identity batch rebuild, one-time experiments) were deleted; they remain in git history.
Put reusable tooling in `scripts/` with a test.

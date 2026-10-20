# Rule 17 — A text file must not mix CRLF and LF internally

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 17`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `python scripts/line_ending_guard.py --all` exits 0; `pytest tests/governance/test_line_ending_guard.py` (41 cases)
- **Source section**: Repository Hygiene

## Rule

A text file must not mix CRLF and LF internally. `.gitattributes` pins what git stores (LF); this rule covers what arrives from an editor or a generator, which git cannot normalise retroactively. A single mixed file makes `git diff` report a whole-file change, destroys `git blame` attribution, and breaks the byte-exact reconciliation this repo relies on to attribute a change to a baseline (`git show HEAD:path` + SHA256). Either ending is acceptable **per file**; only mixing is forbidden. **`MIXED_EXEMPT` is now empty**: the whole working tree was measured byte-by-byte on 2026-09-17 and every mixed file was normalised, so any mixed file at all now fails. Scope is decided by **evidence, not declaration**: the suffix whitelist is only a fast path, and an extensionless file is still classified (by a UTF-8/NUL content sniff) — a `LICENSE` holding 21 CRLF + 11 lone LF sat undetected precisely because `Path("LICENSE").suffix == ""` never matched the whitelist and `inspect` never read the file. The guard self-tests its classifier first: a weakened predicate fails loudly instead of silently reporting a clean tree.

# Rule 26 — A kept broad `except` is narrowed or marked, and the mark is not the permission

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 26`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `python scripts/broad_except_ratchet.py` (exits 0; counts every handler by AST, then checks per-file allowances, the global `max_broad`/`max_silent` ceilings, every `policy.lanes` budget, and — for each `whitelist` entry — that its reason is non-empty **and** its declared `broad`/`silent` numbers still equal what the tree measures); `pytest tests/governance/test_broad_except_ratchet.py` (15 cases)
- **Source section**: Registration Protocol Consistency; Rule 1; Rule 3

## Rule

**`except Exception` is the default, not the decision.** A broad handler either gets narrowed or it gets marked — and marking it does not grant permission.

**Narrow first.** A handler that only guards a transport call takes `TRANSPORT_ERRORS` from `sms_tool/kernel/transport.py` (`(RequestsError, OSError, TimeoutError)`); one that guards a single known shape takes that exception (`except KeyError:`, `except (ValueError, TypeError):`). The leaf exists for exactly this and nothing else imports it for another reason.

**Why broad is not free.** On 2026-11-02 the protocol lane measured the failure mode directly: a `KeyError` raised by a re-shaped response was caught as a network failure, so it landed in the funnel's `unknown` bucket while the batch kept spending mailboxes. A narrow tuple keeps a defect a defect — it surfaces as an internal error instead of being reclassified as a transient one and retried.

**When it has to stay broad**, two things are required and they are separate:

1. **The marker.** `except Exception as exc:  # broad-except: compat` — and, where the reason is not self-evident from the next two lines, a short clause saying why the stage must not fail there (e.g. `# broad-except: compat - the stage must not fail here`). This is how a reviewer finds the deliberate cases: `grep -rn '# broad-except:' sms_tool services`. There are 51 today, all of them reviewed.
2. **The permission**, which lives in `scripts/broad_except_baseline.json` and nowhere else: `policy.max_broad` / `policy.max_silent` are the global ceilings, `policy.lanes` scopes a ceiling to a directory, `whitelist` accepts a specific file (with a reason and its measured counts), and `policy.budget_history` records why a ceiling moved.

**The marker is documentation; the gate never reads it.** `broad_except_ratchet.py` counts with the AST — `grep -r broad-except scripts/` matches only a `print` format string. So a comment cannot buy an allowance, and deleting a comment does not lower a count. That asymmetry is deliberate: it keeps the marker honest (it can only help a human) and the gate non-negotiable (it only trusts the tree).

## What the numbers mean, and how to move them

| Field | What it is | How it may change |
| --- | --- | --- |
| `files[name].broad/silent` | Frozen per-file debt | `--write` re-measures it |
| `policy.max_broad/max_silent` | Global ceilings | Only by a deliberate raise, recorded in `policy.budget_history` |
| `policy.lanes[...]` | A budget scoped to a directory tree | A reviewed decision, never a measurement — `--write` must not drop it |
| `whitelist[name]` | Acceptance of one file, with a reason **and** its measured counts | `--write` deliberately does **not** refresh the counts: re-measure and re-review |
| `policy.budget_history[]` | Why a ceiling moved, with a dated review record | Append-only |

`--write` rebuilds the measurement (`files`, the ceilings) and preserves every reviewed decision (`lanes`, `budget_history`, `whitelist`). That split is the whole point: the script may re-measure, it may never re-decide. Both preservation rules were written after the opposite happened — `lanes` was dropped by a re-freeze on 2026-11-01 (the gate went decorative while still printing OK), and `max_silent` reached 158 on 2026-11-02 with no record anywhere, which is why the history field exists.

**The protocol-registration lane carries `max_silent: 0`.** Not as debt to be paid down — as a standing requirement: in that lane a swallowed exception is indistinguishable from a protocol failure. Adding a silent handler under `sms_tool/auth_flow/` or `sms_tool/registration/` needs a lane change first, which is a decision, not an edit.

**A raise needs a review record.** Every `policy.budget_history` entry names a file under `docs/audits/landings/`, and `test_shipped_budget_history_is_well_formed_and_cited` fails if that file is missing. A cap that moved without a reachable reason is exactly the state this rule exists to prevent; see [`landing-2026-11-02-broad-except-budget-and-transport-narrowing.md`](../audits/landings/landing-2026-11-02-broad-except-budget-and-transport-narrowing.md) for the shape of one, including what the audit of 51 markers actually verified (no behaviour change, no narrowing undone, the broad axis going **down** over the same change).

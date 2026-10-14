# One-off scripts

Scripts that were written for a single dated investigation or clean-up and are
not part of any gate, build or documented workflow. They are kept here, apart
from the maintained tooling in `scripts/`, until a final review decides whether
each one is deleted.

Run them from the repository root as modules, for example
`python -m scripts.oneoff.probe_paypal_regions`.

Nothing in the repository imports from this directory. Do not add new scripts
here; put reusable tooling in `scripts/` with a test.

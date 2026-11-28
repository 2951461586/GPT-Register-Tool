## `sentinel-runner-parse-rs` in Rust

![sentinel-runner-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/sentinel/runner.py` (Python) |
| Recorded cases matched | 229 / 229 |
| Speedup, call included | 1.5x (geometric mean) |
| Fallback to the original | 0.0% (0 of 229 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_impersonate_version` | 229 | 684 ns | 456 ns | 1.5x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `registration-quality-rs` in Rust

![registration-quality-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/progress.py` (Python) |
| Recorded cases matched | 437 / 437 |
| Speedup, call included | 1.7x (geometric mean) |
| Fallback to the original | 0.0% (0 of 437 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_aggregate_progress_jsonl` | 437 | 42.7 µs | 24.7 µs | 1.7x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

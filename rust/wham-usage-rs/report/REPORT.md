## `wham-usage-rs` in Rust

![wham-usage-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/wham_usage.py` (Python) |
| Recorded cases matched | 183 / 183 |
| Speedup, call included | 1.5x (geometric mean) |
| Fallback to the original | 0.0% (0 of 183 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `parse_wham_usage` | 183 | 11.0 µs | 7.5 µs | 1.5x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

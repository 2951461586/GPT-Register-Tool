## `cpa-classify-rs` in Rust

![cpa-classify-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/importers/cpa.py` (Python) |
| Recorded cases matched | 23 / 23 |
| Speedup, call included | 1.7x (geometric mean) |
| Fallback to the original | 0.0% (0 of 23 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `classify_cpa_auth_file` | 23 | 3.3 µs | 1.9 µs | 1.7x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

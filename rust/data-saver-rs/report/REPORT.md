## `data-saver-rs` in Rust

![data-saver-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration_drivers/data_saver.py` (Python) |
| Recorded cases matched | 43 / 43 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 43 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `is_cacheable_static_asset` | 43 | 2.6 µs | 1.1 µs | 2.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

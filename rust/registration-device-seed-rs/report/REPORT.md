## `registration-device-seed-rs` in Rust

![registration-device-seed-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/state.py` (Python) |
| Recorded cases matched | 434 / 434 |
| Speedup, call included | 5.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 434 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_derive_seeded_device_ids` | 434 | 7.2 µs | 1.4 µs | 5.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `smsbower-phone-rs` in Rust

![smsbower-phone-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/phone/smsbower.py` (Python) |
| Recorded cases matched | 220 / 220 |
| Speedup, call included | 2.1x (geometric mean) |
| Fallback to the original | 0.0% (0 of 220 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `normalize_phone` | 220 | 1.1 µs | 516 ns | 2.1x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

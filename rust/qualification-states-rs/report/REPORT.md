## `qualification-states-rs` in Rust

![qualification-states-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/payment/qualification_states.py` (Python) |
| Recorded cases matched | 176 / 176 |
| Speedup, call included | 1.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 176 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `qualification_state` | 32 | 1.5 µs | 1.2 µs | 1.3x |
| `qualification_label` | 144 | 1.7 µs | 1.1 µs | 1.5x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

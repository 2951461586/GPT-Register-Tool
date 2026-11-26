## `qualification-parse-rs` in Rust

![qualification-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/payment/qualification.py` (Python) |
| Recorded cases matched | 219 / 219 |
| Speedup, call included | 1.2x (geometric mean) |
| Fallback to the original | 0.0% (0 of 219 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `parse_method_selection` | 219 | 740 ns | 604 ns | 1.2x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

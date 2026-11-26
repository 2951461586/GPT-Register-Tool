## `paypal-dom-rs` in Rust

![paypal-dom-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/paypal/dom_fields.py` (Python) |
| Recorded cases matched | 270 / 270 |
| Speedup, call included | 2.1x (geometric mean) |
| Fallback to the original | 0.0% (0 of 270 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_value_matches` | 270 | 1.5 µs | 708 ns | 2.1x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

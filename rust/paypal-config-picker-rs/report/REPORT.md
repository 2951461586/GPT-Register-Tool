## `paypal-config-picker-rs` in Rust

![paypal-config-picker-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/paypal/config_picker.py` (Python) |
| Recorded cases matched | 228 / 228 |
| Speedup, call included | 2.9x (geometric mean) |
| Fallback to the original | 0.0% (0 of 228 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_safe_paypal_session_stem` | 228 | 1.1 µs | 385 ns | 2.9x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

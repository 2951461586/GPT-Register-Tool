## `sentinel-device-check-rs` in Rust

![sentinel-device-check-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/sentinel_tokens.py` (Python) |
| Recorded cases matched | 575 / 575 |
| Speedup, call included | 1.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 575 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_validate_sentinel_device_payload` | 575 | 1.3 µs | 1.1 µs | 1.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

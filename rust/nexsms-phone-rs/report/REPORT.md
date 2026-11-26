## `nexsms-phone-rs` in Rust

![nexsms-phone-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/phone/nexsms.py` (Python) |
| Recorded cases matched | 221 / 221 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 221 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `api_phone_number` | 221 | 1.0 µs | 439 ns | 2.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

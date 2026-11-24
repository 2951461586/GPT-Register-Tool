## `sms-code-rs` in Rust

![sms-code-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/phone/sms_utils.py` (Python) |
| Recorded cases matched | 58 / 58 |
| Speedup, call included | 2.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 58 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_extract_sms_code` | 58 | 2.2 µs | 900 ns | 2.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

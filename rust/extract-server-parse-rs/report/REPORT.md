## `extract-server-parse-rs` in Rust

![extract-server-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/pay_link/extract_server.py` (Python) |
| Recorded cases matched | 222 / 222 |
| Speedup, call included | 6.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 222 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_validate_api_base` | 222 | 3.3 µs | 517 ns | 6.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

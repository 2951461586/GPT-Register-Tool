## `proxy-screening-rs` in Rust

![proxy-screening-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/proxy/screening.py` (Python) |
| Recorded cases matched | 45 / 45 |
| Speedup, call included | 2.6x (geometric mean) |
| Fallback to the original | 0.0% (0 of 45 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `classify_network_type` | 45 | 6.8 µs | 2.7 µs | 2.6x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

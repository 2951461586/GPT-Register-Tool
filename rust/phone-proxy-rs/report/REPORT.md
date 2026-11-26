## `phone-proxy-rs` in Rust

![phone-proxy-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/phone/phone_proxy.py` (Python) |
| Recorded cases matched | 267 / 267 |
| Speedup, call included | 1.5x (geometric mean) |
| Fallback to the original | 0.0% (0 of 267 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_api_url_for_region` | 267 | 1.2 µs | 828 ns | 1.5x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

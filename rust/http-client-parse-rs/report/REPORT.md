## `http-client-parse-rs` in Rust

![http-client-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/transport/http_client.py` (Python) |
| Recorded cases matched | 234 / 234 |
| Speedup, call included | 2.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 234 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `is_transient_transport_error` | 234 | 1.3 µs | 540 ns | 2.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

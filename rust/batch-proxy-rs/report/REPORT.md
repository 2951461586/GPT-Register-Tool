## `batch-proxy-rs` in Rust

![batch-proxy-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/batch_runner.py` (Python) |
| Recorded cases matched | 208 / 208 |
| Speedup, call included | 2.1x (geometric mean) |
| Fallback to the original | 0.0% (0 of 208 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_interleave_proxy_endpoints` | 208 | 5.8 µs | 2.8 µs | 2.1x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `proxy-edge-probe-rs` in Rust

![proxy-edge-probe-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/proxy/edge_probe.py` (Python) |
| Recorded cases matched | 92 / 92 |
| Speedup, call included | 1.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 92 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `classify_edge_response` | 92 | 4.4 µs | 3.4 µs | 1.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

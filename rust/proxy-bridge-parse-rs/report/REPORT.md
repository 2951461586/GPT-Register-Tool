## `proxy-bridge-parse-rs` in Rust

![proxy-bridge-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/proxy/bridge.py` (Python) |
| Recorded cases matched | 541 / 541 |
| Speedup, call included | 1.9x (geometric mean) |
| Fallback to the original | 0.0% (0 of 541 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_addr_type` | 255 | 791 ns | 267 ns | 3.0x |
| `_host_matches_blocked` | 286 | 680 ns | 573 ns | 1.2x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

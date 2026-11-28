## `token-telemetry-rs` in Rust

![token-telemetry-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/observability/token_telemetry.py` (Python) |
| Recorded cases matched | 220 / 220 |
| Speedup, call included | 3.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 220 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_parse_token_claims_and_hash` | 220 | 2.4 µs | 701 ns | 3.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

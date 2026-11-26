## `codex-jwt-rs` in Rust

![codex-jwt-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/codex/export.py` (Python) |
| Recorded cases matched | 233 / 233 |
| Speedup, call included | 2.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 233 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_jwt_claims` | 233 | 859 ns | 354 ns | 2.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `session-refresh-parse-rs` in Rust

![session-refresh-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/session/refresh.py` (Python) |
| Recorded cases matched | 244 / 244 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 244 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_has_session_cookie` | 244 | 609 ns | 264 ns | 2.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

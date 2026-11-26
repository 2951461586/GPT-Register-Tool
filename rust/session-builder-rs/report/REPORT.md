## `session-builder-rs` in Rust

![session-builder-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/session/builder.py` (Python) |
| Recorded cases matched | 221 / 221 |
| Speedup, call included | 2.8x (geometric mean) |
| Fallback to the original | 0.0% (0 of 221 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_session_token_from_cookie_header` | 221 | 836 ns | 303 ns | 2.8x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `liveness-parse-rs` in Rust

![liveness-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_liveness.py` (Python) |
| Recorded cases matched | 254 / 254 |
| Speedup, call included | 2.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 254 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_is_token_invalid` | 254 | 965 ns | 477 ns | 2.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

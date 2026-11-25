## `sentinel-tokens-rs` in Rust

![sentinel-tokens-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/sentinel_tokens.py` (Python) |
| Recorded cases matched | 98 / 98 |
| Speedup, call included | 6.9x (geometric mean) |
| Fallback to the original | 0.0% (0 of 98 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_solve_pow` | 98 | 14.3 ms | 2.1 ms | 6.9x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

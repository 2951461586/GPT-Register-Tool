## `account-stages-url-rs` in Rust

![account-stages-url-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration/account_stages.py` (Python) |
| Recorded cases matched | 220 / 220 |
| Speedup, call included | 6.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 220 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_strip_prompt_query` | 220 | 3.0 µs | 501 ns | 6.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

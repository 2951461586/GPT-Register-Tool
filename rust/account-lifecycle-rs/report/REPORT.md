## `account-lifecycle-rs` in Rust

![account-lifecycle-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_lifecycle.py` (Python) |
| Recorded cases matched | 217 / 217 |
| Speedup, call included | 2.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 217 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_email_core` | 217 | 1.2 µs | 563 ns | 2.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

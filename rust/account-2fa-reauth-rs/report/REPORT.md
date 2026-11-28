## `account-2fa-reauth-rs` in Rust

![account-2fa-reauth-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_2fa.py` (Python) |
| Recorded cases matched | 436 / 436 |
| Speedup, call included | 2.1x (geometric mean) |
| Fallback to the original | 0.0% (0 of 436 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_needs_reauth` | 436 | 769 ns | 370 ns | 2.1x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

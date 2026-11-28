## `account-terminal-rs` in Rust

![account-terminal-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_terminal.py` (Python) |
| Recorded cases matched | 436 / 436 |
| Speedup, call included | 1.9x (geometric mean) |
| Fallback to the original | 0.0% (0 of 436 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `text_has_account_deactivated` | 436 | 892 ns | 464 ns | 1.9x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

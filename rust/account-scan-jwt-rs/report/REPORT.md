## `account-scan-jwt-rs` in Rust

![account-scan-jwt-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_scan.py` (Python) |
| Recorded cases matched | 223 / 223 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 223 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_jwt_plan_type` | 223 | 909 ns | 397 ns | 2.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

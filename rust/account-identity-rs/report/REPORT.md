## `account-identity-rs` in Rust

![account-identity-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/accounts/account_identity.py` (Python) |
| Recorded cases matched | 19 / 19 |
| Speedup, call included | 2.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 19 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `proxy_egress_key` | 19 | 5.8 µs | 2.4 µs | 2.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

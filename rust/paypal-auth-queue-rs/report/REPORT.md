## `paypal-auth-queue-rs` in Rust

![paypal-auth-queue-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/paypal/authorization_queue.py` (Python) |
| Recorded cases matched | 215 / 215 |
| Speedup, call included | 2.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 215 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_account_ref` | 215 | 866 ns | 359 ns | 2.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

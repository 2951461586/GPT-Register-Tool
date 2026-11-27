## `browser-flow-recovery-rs` in Rust

![browser-flow-recovery-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration_drivers/browser_flow/recovery.py` (Python) |
| Recorded cases matched | 240 / 240 |
| Speedup, call included | 2.6x (geometric mean) |
| Fallback to the original | 0.0% (0 of 240 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `is_navigation_retryable` | 240 | 1.6 µs | 622 ns | 2.6x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `browser-flow-decisions-rs` in Rust

![browser-flow-decisions-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration_drivers/browser_flow/decisions.py` (Python) |
| Recorded cases matched | 241 / 241 |
| Speedup, call included | 1.6x (geometric mean) |
| Fallback to the original | 0.0% (0 of 241 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `needs_chat_base_navigation` | 241 | 606 ns | 376 ns | 1.6x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

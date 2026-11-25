## `playwright-proxy-rs` in Rust

![playwright-proxy-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/registration_drivers/browser_session.py` (Python) |
| Recorded cases matched | 22 / 22 |
| Speedup, call included | 2.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 22 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_playwright_proxy` | 22 | 5.0 µs | 2.4 µs | 2.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

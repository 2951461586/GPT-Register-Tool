## `gmail-html-rs` in Rust

![gmail-html-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/providers/mailbox_gmail.py` (Python) |
| Recorded cases matched | 20 / 20 |
| Speedup, call included | 3.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 20 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_html_to_text` | 20 | 7.7 µs | 2.6 µs | 3.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `icloud-html-rs` in Rust

![icloud-html-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/providers/mailbox_icloud_url.py` (Python) |
| Recorded cases matched | 25 / 25 |
| Speedup, call included | 3.5x (geometric mean) |
| Fallback to the original | 0.0% (0 of 25 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_strip_html_to_visible_text` | 25 | 49.9 µs | 14.1 µs | 3.5x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

## `gmail-html-rs` in Rust

![gmail-html-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/providers/mailbox_gmail.py` (Python) |
| Recorded cases matched | 60 / 60 |
| Speedup, call included | 2.2x (geometric mean) |
| Fallback to the original | 0.0% (0 of 60 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_html_to_text` | 32 | 5.7 µs | 2.2 µs | 2.6x |
| `_parse_proxy_url` | 28 | 3.5 µs | 1.8 µs | 1.9x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

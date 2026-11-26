## `icloud-html-rs` in Rust

![icloud-html-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/providers/mailbox_icloud_url.py` (Python) |
| Recorded cases matched | 1027 / 1027 |
| Speedup, call included | 2.5x (geometric mean) |
| Fallback to the original | 0.0% (0 of 1027 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_strip_html_to_visible_text` | 17 | 3.3 µs | 1.5 µs | 2.2x |
| `_latest_mail_received_at` | 21 | 1.5 µs | 1.0 µs | 1.5x |
| `_decode_js_string` | 228 | 1.4 µs | 480 ns | 2.9x |
| `_with_message_limit` | 234 | 2.5 µs | 1.4 µs | 1.8x |
| `_same_origin` | 263 | 2.6 µs | 680 ns | 3.9x |
| `_extract_url_credentials` | 264 | 3.0 µs | 828 ns | 3.7x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

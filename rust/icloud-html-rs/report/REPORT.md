## `icloud-html-rs` in Rust

![icloud-html-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/providers/mailbox_icloud_url.py` (Python) |
| Recorded cases matched | 1316 / 1316 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 1316 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_strip_html_to_visible_text` | 29 | 2.9 µs | 1.4 µs | 2.1x |
| `_latest_mail_received_at` | 33 | 1.0 µs | 986 ns | 1.0x |
| `_decode_js_string` | 238 | 1.3 µs | 476 ns | 2.8x |
| `_with_message_limit` | 244 | 2.4 µs | 1.3 µs | 1.8x |
| `_same_origin` | 270 | 2.4 µs | 694 ns | 3.4x |
| `_extract_url_credentials` | 272 | 2.6 µs | 789 ns | 3.3x |
| `_yangyang_api_paths` | 230 | 1.0 µs | 367 ns | 2.8x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

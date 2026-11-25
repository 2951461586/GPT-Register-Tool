## `normalize-proxy-url-rs` in Rust

![normalize-proxy-url-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/proxy/entry.py` (Python) |
| Recorded cases matched | 1828 / 1828 |
| Speedup, call included | 3.4x (geometric mean) |
| Fallback to the original | 0.0% (0 of 1828 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `normalize_proxy_url` | 227 | 1.3 µs | 899 ns | 1.4x |
| `redact_proxy_url` | 200 | 3.2 µs | 1.4 µs | 2.3x |
| `strip_session_id` | 223 | 5.7 µs | 1.4 µs | 4.0x |
| `infer_proxy_country` | 223 | 4.6 µs | 1.1 µs | 4.1x |
| `_mask_credentials` | 227 | 7.2 µs | 468 ns | 15x |
| `redact_proxy_text` | 228 | 7.5 µs | 2.0 µs | 3.8x |
| `retarget_region` | 265 | 1.3 µs | 761 ns | 1.7x |
| `infer_region` | 235 | 2.9 µs | 875 ns | 3.4x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

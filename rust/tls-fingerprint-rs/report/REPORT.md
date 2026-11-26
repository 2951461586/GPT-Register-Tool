## `tls-fingerprint-rs` in Rust

![tls-fingerprint-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/transport/tls_fingerprint.py` (Python) |
| Recorded cases matched | 241 / 241 |
| Speedup, call included | 1.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 241 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `permute_extension_order` | 241 | 676 ns | 505 ns | 1.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

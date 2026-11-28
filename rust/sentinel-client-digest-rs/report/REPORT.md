## `sentinel-client-digest-rs` in Rust

![sentinel-client-digest-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/sentinel/client.py` (Python) |
| Recorded cases matched | 215 / 215 |
| Speedup, call included | 2.2x (geometric mean) |
| Fallback to the original | 0.0% (0 of 215 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_secret_digest` | 215 | 870 ns | 401 ns | 2.2x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

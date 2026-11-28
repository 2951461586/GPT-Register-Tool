## `operation-journal-rs` in Rust

![operation-journal-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/operation_journal.py` (Python) |
| Recorded cases matched | 231 / 231 |
| Speedup, call included | 4.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 231 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_hash_public_operation_id` | 231 | 1.7 µs | 431 ns | 4.0x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.

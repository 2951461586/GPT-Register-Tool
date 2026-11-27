## `batch-report-parse-rs` in Rust

![batch-report-parse-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/payment/batch_report.py` (Python) |
| Recorded cases matched | 466 / 466 |
| Speedup, call included | 3.0x (geometric mean) |
| Fallback to the original | 0.0% (0 of 466 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `safe_batch_id` | 235 | 1.6 µs | 503 ns | 3.2x |
| `account_ref` | 231 | 1.9 µs | 706 ns | 2.8x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.
